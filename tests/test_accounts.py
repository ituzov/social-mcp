import os
import sqlite3
import stat

import pytest
from pydantic import ValidationError

from social_mcp.accounts import (
    Account,
    AccountStorageError,
    AccountSummary,
    get_account,
    list_accounts,
    replace_credentials,
    save_account,
)
from social_mcp.database import initialize_database

TOKEN = "synthetic-account-test-token"


def save(path, *, platform="threads", account_id="123", token=TOKEN, **changes):
    fields = dict(username="example", obtained_at=100, expires_at=None)
    fields.update(changes)
    save_account(
        path, platform=platform, account_id=account_id, access_token=token, **fields
    )


def test_upsert_keeps_other_accounts_and_hides_token(db_path):
    save(db_path)
    save(db_path, platform="instagram")
    save(db_path, account_id="456")
    save(db_path, token="synthetic-new-token", username="renamed", obtained_at=200)
    initialize_database(db_path)
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert account.username == "renamed"
    assert account.access_token.get_secret_value() == "synthetic-new-token"
    assert account.obtained_at == 200
    assert account.expires_at is None
    assert account.access_token.get_secret_value() not in repr(account)
    stored_account = get_account(db_path, platform="instagram", account_id="123")
    assert stored_account is not None
    assert stored_account.username == "example"
    stored_account = get_account(db_path, platform="threads", account_id="456")
    assert stored_account is not None
    assert stored_account.username == "example"
    assert get_account(db_path, platform="threads", account_id="missing") is None


def test_failed_write_is_rolled_back_without_secret_in_error(db_path):
    save(db_path)
    with pytest.raises(AccountStorageError) as caught:
        save(db_path, username="invalid", obtained_at=None)
    assert TOKEN not in str(caught.value)
    stored_account = get_account(db_path, platform="threads", account_id="123")
    assert stored_account is not None
    assert stored_account.username == "example"


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(AccountStorageError):
        get_account(path, platform="threads", account_id="123")
    with pytest.raises(AccountStorageError):
        save(path)
    assert not path.exists()


def test_special_characters_in_database_path(tmp_path):
    path = tmp_path / "data #?%" / "accounts #?%.sqlite3"
    initialize_database(path)
    save(path)
    stored_account = get_account(path, platform="threads", account_id="123")
    assert stored_account is not None
    assert stored_account.access_token.get_secret_value() == TOKEN
    assert get_account(path, platform="threads", account_id="123' OR 1=1 --") is None
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_list_projects_only_public_fields_and_filters_platform(db_path, caplog):
    save(db_path)
    save(db_path, platform="instagram")
    save(db_path, account_id="456", username="second", expires_at=999)
    caplog.set_level("INFO", logger="sqlalchemy.engine.Engine")
    summaries = list_accounts(db_path, platform="threads")
    assert [
        (item.account_id, item.username, item.expires_at) for item in summaries
    ] == [
        ("123", "example", None),
        ("456", "second", 999),
    ]
    assert TOKEN not in repr(summaries)
    assert "accounts.access_token" not in caplog.text
    assert "SELECT" in caplog.text


def test_credentials_compare_and_swap_is_atomic_and_account_scoped(db_path):
    save(db_path)
    save(db_path, platform="instagram")

    def replace(expected, token, expiry):
        return replace_credentials(
            db_path,
            platform="threads",
            account_id="123",
            expected_token=expected,
            access_token=token,
            obtained_at=200,
            expires_at=expiry,
        )

    assert not replace("stale-token", "wrong", 123)
    stored_account = get_account(db_path, platform="threads", account_id="123")
    assert stored_account is not None
    assert stored_account.access_token.get_secret_value() == TOKEN
    assert replace(TOKEN, "new-token", 1000)
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert (account.access_token.get_secret_value(), account.expires_at) == (
        "new-token",
        1000,
    )
    assert not replace(TOKEN, "late-result", 2000)
    with pytest.raises(AccountStorageError):
        replace("new-token", None, 5000)
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert (account.access_token.get_secret_value(), account.expires_at) == (
        "new-token",
        1000,
    )
    stored_account = get_account(db_path, platform="instagram", account_id="123")
    assert stored_account is not None
    assert stored_account.access_token.get_secret_value() == TOKEN


def test_account_secret_is_masked_in_serialization(db_path):
    save(db_path)
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert TOKEN not in account.model_dump_json()
    assert TOKEN not in repr(account)
    assert account.access_token.get_secret_value() == TOKEN


@pytest.mark.parametrize("schema", [Account, AccountSummary])
def test_account_schemas_are_frozen_and_strict(schema):
    fields = dict(
        platform="threads",
        account_id="123",
        username="example",
        obtained_at=1,
        expires_at=None,
    )
    if schema is Account:
        fields["access_token"] = TOKEN
    account = schema(**fields)
    with pytest.raises(ValidationError):
        account.username = "changed"
    with pytest.raises(ValidationError):
        schema(**{**fields, "obtained_at": "1"})
    with pytest.raises(ValidationError) as caught:
        schema(**{**fields, "obtained_at": TOKEN})
    assert TOKEN not in str(caught.value)


def test_public_summary_rejects_secret_field():
    with pytest.raises(ValidationError) as caught:
        AccountSummary(
            platform="threads",
            account_id="123",
            username=None,
            obtained_at=1,
            expires_at=None,
            access_token=TOKEN,
        )
    assert TOKEN not in str(caught.value)


def test_corrupt_database_row_is_safe_storage_error(db_path):
    save(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE accounts SET obtained_at = ?", (TOKEN,))
    for read in (
        lambda: get_account(db_path, platform="threads", account_id="123"),
        lambda: list_accounts(db_path, platform="threads"),
    ):
        with pytest.raises(AccountStorageError) as caught:
            read()
        assert TOKEN not in str(caught.value)
