"""Persist and retrieve accounts using SQLAlchemy."""

from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from social_mcp.accounts.errors import AccountStorageError
from social_mcp.accounts.models import AccountRecord
from social_mcp.accounts.schemas import Account, AccountSummary
from social_mcp.database import open_database


def save_account(
    path: Path,
    *,
    platform: str,
    account_id: str,
    username: str | None,
    access_token: str,
    obtained_at: int,
    expires_at: int | None,
) -> None:
    """Create or update an account in one transaction; timestamps use Unix seconds."""
    statement = insert(AccountRecord).values(
        platform=platform,
        account_id=account_id,
        username=username,
        access_token=access_token,
        obtained_at=obtained_at,
        expires_at=expires_at,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[AccountRecord.platform, AccountRecord.account_id],
        set_={
            "username": statement.excluded.username,
            "access_token": statement.excluded.access_token,
            "obtained_at": statement.excluded.obtained_at,
            "expires_at": statement.excluded.expires_at,
        },
    )
    try:
        with open_database(path) as engine, Session(engine) as session:
            with session.begin():
                session.execute(statement)
    except SQLAlchemyError:
        raise AccountStorageError("Could not save the account.") from None


def get_account(path: Path, *, platform: str, account_id: str) -> Account | None:
    """Find an account by platform and ID; a database failure is not a missing record."""
    try:
        with open_database(path, mode="ro") as engine, Session(engine) as session:
            record = session.get(AccountRecord, (platform, account_id))
            if record is None:
                return None
            return Account.model_validate(
                {
                    "platform": record.platform,
                    "account_id": record.account_id,
                    "username": record.username,
                    "access_token": record.access_token,
                    "obtained_at": record.obtained_at,
                    "expires_at": record.expires_at,
                }
            )
    except SQLAlchemyError, ValidationError:
        raise AccountStorageError("Could not read the account storage.") from None


def list_accounts(path: Path, *, platform: str) -> list[AccountSummary]:
    statement = (
        select(
            AccountRecord.platform,
            AccountRecord.account_id,
            AccountRecord.username,
            AccountRecord.obtained_at,
            AccountRecord.expires_at,
        )
        .where(AccountRecord.platform == platform)
        .order_by(AccountRecord.account_id)
    )
    try:
        with open_database(path, mode="ro") as engine, Session(engine) as session:
            return [
                AccountSummary(**row) for row in session.execute(statement).mappings()
            ]
    except SQLAlchemyError, ValidationError:
        raise AccountStorageError("Could not read the account list.") from None


def replace_credentials(
    path: Path,
    *,
    platform: str,
    account_id: str,
    expected_token: str,
    access_token: str,
    obtained_at: int,
    expires_at: int,
) -> bool:
    """Replace credentials atomically without overwriting a newer reconnection."""
    statement = (
        update(AccountRecord)
        .where(
            AccountRecord.platform == platform,
            AccountRecord.account_id == account_id,
            AccountRecord.access_token == expected_token,
        )
        .values(
            access_token=access_token, obtained_at=obtained_at, expires_at=expires_at
        )
    )
    try:
        with open_database(path) as engine, Session(engine) as session:
            with session.begin():
                return session.connection().execute(statement).rowcount == 1
    except SQLAlchemyError:
        raise AccountStorageError("Could not update the account connection.") from None
