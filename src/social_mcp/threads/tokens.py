"""Token validation and refresh with locking and atomic persistence."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path

from filelock import AsyncFileLock, Timeout

from social_mcp.accounts import (
    Account,
    AccountStorageError,
    get_account,
    replace_credentials,
    save_account,
)
from social_mcp.threads.async_utils import finish_before_cancel as _finish_before_cancel
from social_mcp.threads.client import inspect_token, refresh_token
from social_mcp.threads.errors import ThreadsError, ThreadsTransientError
from social_mcp.threads.schemas import ThreadsProfile, TokenInfo

DAY = 86400
REFRESH_WINDOW = 7 * DAY
RETRY_DELAY = 300
logger = logging.getLogger(__name__)


class TokenLifecycle:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._retry_after: dict[str, float] = {}

    @asynccontextmanager
    async def _lock(self, account_id: str):
        key = sha256(account_id.encode()).hexdigest()[:24]
        path = self.db_path.with_name(f"{self.db_path.name}.threads-{key}.lock")
        try:
            async with AsyncFileLock(path, timeout=30, mode=0o600):
                yield
        except Timeout:
            raise ThreadsError(
                "Another request is using this account. Try again later."
            ) from None

    @staticmethod
    async def inspect(token: str, account_id: str | None = None) -> TokenInfo:
        info = await inspect_token(token)
        now = int(time.time())
        if not info.is_valid or info.expires_at <= now:
            raise ThreadsError(
                "The Threads token has expired or been revoked. Reconnect the account."
            )
        if info.data_access_expires_at and info.data_access_expires_at <= now:
            raise ThreadsError(
                "Threads data access has expired. Reconnect the account."
            )
        if info.expires_at <= info.issued_at:
            raise ThreadsError("Threads returned an invalid token expiry.")
        if "threads_basic" not in info.scopes:
            raise ThreadsError("Connecting requires the threads_basic permission.")
        if account_id is not None and info.user_id != account_id:
            raise ThreadsError("The token belongs to a different Threads account.")
        return info

    async def save(self, profile: ThreadsProfile, token: str, info: TokenInfo) -> None:
        try:
            async with self._lock(profile.id):
                await _finish_before_cancel(
                    asyncio.to_thread(
                        save_account,
                        self.db_path,
                        platform="threads",
                        account_id=profile.id,
                        username=profile.username,
                        access_token=token,
                        obtained_at=int(time.time()),
                        expires_at=info.usable_until,
                    )
                )
        except AccountStorageError:
            raise ThreadsError("Could not save the Threads connection.") from None

    async def get_account(self, account_id: str) -> Account:
        try:
            async with self._lock(account_id):
                for _ in range(2):
                    account = await asyncio.to_thread(
                        get_account,
                        self.db_path,
                        platform="threads",
                        account_id=account_id,
                    )
                    if account is None:
                        raise ThreadsError(
                            "No Threads account with this ID is connected."
                        )
                    self._require_unexpired(account)
                    if self._needs_maintenance(account):
                        updated = await _finish_before_cancel(self._maintain(account))
                        if updated is None:
                            continue
                        return updated
                    return account
            raise ThreadsError("The connection changed. Repeat the request.")
        except AccountStorageError:
            raise ThreadsError("Could not read the account storage.") from None

    @staticmethod
    def _require_unexpired(account: Account) -> None:
        if account.expires_at is not None and account.expires_at <= time.time():
            raise ThreadsError("The Threads token has expired. Reconnect the account.")

    def _needs_maintenance(self, account: Account) -> bool:
        if account.expires_at is None:
            return True
        due = account.expires_at <= time.time() + REFRESH_WINDOW
        return due and time.monotonic() >= self._retry_after.get(account.account_id, 0)

    async def _maintain(self, account: Account) -> Account | None:
        try:
            info = await self.inspect(
                account.access_token.get_secret_value(), account.account_id
            )
        except ThreadsTransientError:
            if account.expires_at is None:
                raise
            self._defer_refresh(account.account_id)
            return account
        token = account.access_token.get_secret_value()
        obtained_at = account.obtained_at
        now = int(time.time())
        renewable = info.expires_at - info.issued_at > DAY
        old_enough = now - info.issued_at >= DAY
        if renewable and old_enough and info.usable_until <= now + REFRESH_WINDOW:
            try:
                refreshed = await refresh_token(token)
                new_token = refreshed.access_token.get_secret_value()
                new_info = await self.inspect(new_token, account.account_id)
                if new_info.expires_at <= info.expires_at:
                    raise ThreadsError("The token expiry was not extended.")
                token, info, obtained_at = new_token, new_info, now
            except ThreadsError:
                self._defer_refresh(account.account_id)
        changed = await asyncio.to_thread(
            replace_credentials,
            self.db_path,
            platform="threads",
            account_id=account.account_id,
            expected_token=account.access_token.get_secret_value(),
            access_token=token,
            obtained_at=obtained_at,
            expires_at=info.usable_until,
        )
        if not changed:
            return None
        return Account.model_validate(
            account.model_dump()
            | {
                "access_token": token,
                "obtained_at": obtained_at,
                "expires_at": info.usable_until,
            }
        )

    def _defer_refresh(self, account_id: str) -> None:
        self._retry_after[account_id] = time.monotonic() + RETRY_DELAY
        logger.warning(
            "Threads token refresh is unavailable; using the still-valid token."
        )
