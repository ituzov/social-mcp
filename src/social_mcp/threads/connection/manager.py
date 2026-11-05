"""One active form per MCP process and its result without secrets."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import BaseModel

from social_mcp.threads.connection.page import ConnectionPage
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ThreadsProfile
from social_mcp.threads.service import ThreadsService

ConnectionAction = Literal["start", "restart", "cancel"]


class ConnectionStatus(BaseModel):
    status: Literal["pending", "connected", "closed"]
    url: str | None = None
    profile: ThreadsProfile | None = None
    expires_at: int | None = None


class ThreadsConnection:
    def __init__(self, service: ThreadsService) -> None:
        self._service = service
        self._page: ConnectionPage | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @asynccontextmanager
    async def lifespan(self, _app: object) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await self.close()

    async def manage(self, action: ConnectionAction = "start") -> ConnectionStatus:
        async with self._lock:
            if self._closed:
                raise ThreadsError(
                    "The server is shutting down. Connecting is unavailable."
                )
            if action in ("restart", "cancel"):
                await self._close_page()
            if action == "cancel":
                return ConnectionStatus(status="closed")
            return await self._start_or_status()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._close_page()

    async def _start_or_status(self) -> ConnectionStatus:
        page = self._page
        if page is None:
            try:
                page = ConnectionPage(self._service)
                await page.__aenter__()
            except Exception:
                raise ThreadsError(
                    "Could not open the Threads connection page."
                ) from None
            self._page = page
        if page.finished:
            profile = await page.wait()
            accounts = await self._service.list_accounts()
            expires_at = next(
                (
                    account.expires_at
                    for account in accounts
                    if account.account_id == profile.id
                ),
                None,
            )
            return ConnectionStatus(
                status="connected", profile=profile, expires_at=expires_at
            )
        return ConnectionStatus(status="pending", url=page.url)

    async def _close_page(self) -> None:
        page = self._page
        if page is not None:
            try:
                await page.close()
            except Exception:
                raise ThreadsError(
                    "Could not close the Threads connection page."
                ) from None
            self._page = None
