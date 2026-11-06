import asyncio
import getpass
import warnings

import httpx
import pytest

from social_mcp import app
from social_mcp.accounts import AccountStorageError, get_account, save_account
from social_mcp.threads import ThreadsError, ThreadsService
from tests.support import mock_http

pytestmark = pytest.mark.usefixtures("verified_token")

TOKEN = "synthetic-import-token"
OLD_TOKEN = "synthetic-previous-token"


def mock_profile(monkeypatch, status=200):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(
            status,
            json=(
                {"id": "123", "username": "example"}
                if status == 200
                else {"error": {"code": 190, "message": TOKEN}}
            ),
        )

    mock_http(monkeypatch, respond)
    return requests


def test_command_imports_verified_account_and_profile_can_be_read(
    db_path, monkeypatch, capsys
):
    monkeypatch.setenv("SOCIAL_MCP_DB_PATH", str(db_path))
    monkeypatch.setattr("social_mcp.app.getpass.getpass", lambda prompt: f"  {TOKEN}  ")
    monkeypatch.setattr("social_mcp.threads.tokens.time.time", lambda: 12345)
    requests = mock_profile(monkeypatch)

    app.main(["connect", "threads"])

    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert account.access_token.get_secret_value() == TOKEN
    assert account.username == "example"
    assert account.obtained_at == 12345
    assert account.expires_at == 4_000_000_000
    output = capsys.readouterr()
    assert "@example" in output.out
    assert "123" in output.out
    assert TOKEN not in output.out + output.err
    profile = asyncio.run(ThreadsService(db_path).get_profile("123"))
    assert profile.username == "example"
    assert len(requests) == 2


def test_reconnect_replaces_token_only_after_successful_profile_read(
    db_path, monkeypatch
):
    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="old",
        access_token=OLD_TOKEN,
        obtained_at=1,
        expires_at=2,
    )
    mock_profile(monkeypatch, status=401)
    with pytest.raises(ThreadsError):
        asyncio.run(ThreadsService(db_path).connect(TOKEN))
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert account.access_token.get_secret_value() == OLD_TOKEN
    assert account.expires_at == 2
    assert account.username == "old"

    mock_profile(monkeypatch)
    asyncio.run(ThreadsService(db_path).connect(TOKEN))
    account = get_account(db_path, platform="threads", account_id="123")
    assert account is not None
    assert account.access_token.get_secret_value() == TOKEN
    assert account.expires_at == 4_000_000_000
    assert account.username == "example"


@pytest.mark.parametrize("failure", ["empty", "eof", "interrupt", "visible_input"])
def test_bad_input_cancels_before_database_or_api(monkeypatch, capsys, failure):
    def read(_prompt):
        if failure == "eof":
            raise EOFError
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "visible_input":
            warnings.warn("Echo unavailable", getpass.GetPassWarning, stacklevel=2)
            pytest.fail("Must not fall back to visible input")
        return "   "

    def unexpected(*_args, **_kwargs):
        pytest.fail("Must not initialize database or call API")

    monkeypatch.setattr("social_mcp.app.getpass.getpass", read)
    monkeypatch.setattr("social_mcp.app.initialize_database", unexpected)
    monkeypatch.setattr("social_mcp.threads.service.fetch_profile", unexpected)
    with pytest.raises(SystemExit) as caught:
        app.main(["connect", "threads"])
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.err
    assert not output.out


def test_rejected_token_is_not_saved_or_printed(db_path, monkeypatch, capsys):
    monkeypatch.setenv("SOCIAL_MCP_DB_PATH", str(db_path))
    monkeypatch.setattr("social_mcp.app.getpass.getpass", lambda prompt: TOKEN)
    requests = mock_profile(monkeypatch, status=401)
    with pytest.raises(SystemExit) as caught:
        app.main(["connect", "threads"])
    assert caught.value.code == 1
    assert len(requests) == 1
    assert get_account(db_path, platform="threads", account_id="123") is None
    output = capsys.readouterr()
    assert not output.out
    assert "token" in output.err
    assert TOKEN not in output.err


def test_storage_failure_is_safe_and_not_reported_as_success(
    db_path, monkeypatch, capsys
):
    def fail(*_args, **_kwargs):
        raise AccountStorageError(TOKEN)

    monkeypatch.setenv("SOCIAL_MCP_DB_PATH", str(db_path))
    monkeypatch.setattr("social_mcp.app.getpass.getpass", lambda prompt: TOKEN)
    monkeypatch.setattr("social_mcp.threads.tokens.save_account", fail)
    mock_profile(monkeypatch)
    with pytest.raises(SystemExit) as caught:
        app.main(["connect", "threads"])
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert not output.out
    assert "save" in output.err
    assert TOKEN not in output.err
    assert get_account(db_path, platform="threads", account_id="123") is None


def test_service_rejects_empty_token_without_api_call(db_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Empty token must not reach API")

    monkeypatch.setattr("social_mcp.threads.service.fetch_profile", unexpected)
    with pytest.raises(ThreadsError, match="empty"):
        asyncio.run(ThreadsService(db_path).connect("  "))
