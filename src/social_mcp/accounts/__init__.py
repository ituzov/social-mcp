"""Public interface of the connected accounts module."""

from social_mcp.accounts.errors import AccountStorageError
from social_mcp.accounts.repository import (
    get_account,
    list_accounts,
    replace_credentials,
    save_account,
)
from social_mcp.accounts.schemas import Account, AccountSummary

__all__ = [
    "Account",
    "AccountSummary",
    "AccountStorageError",
    "get_account",
    "list_accounts",
    "replace_credentials",
    "save_account",
]
