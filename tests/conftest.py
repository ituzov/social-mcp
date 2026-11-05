from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from social_mcp.accounts import save_account
from social_mcp.database import initialize_database
from social_mcp.mcp_server import SocialMCPServer
from social_mcp.threads import ThreadsConnection, ThreadsService, register_threads_tools
from social_mcp.threads.schemas import TokenInfo
from tests.support import PUBLISHING_TOKEN as TOKEN


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_MCP_DB_PATH", str(tmp_path / "settings.sqlite3"))
    monkeypatch.setenv("SOCIAL_MCP_CHECK_UPDATES", "false")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "accounts.sqlite3"
    initialize_database(path)
    return path


@pytest.fixture
def verified_token(monkeypatch: pytest.MonkeyPatch):
    """Isolate profile and form tests from the token metadata API."""
    monkeypatch.setattr(
        "social_mcp.threads.tokens.inspect_token",
        AsyncMock(
            return_value=TokenInfo(
                user_id="123",
                is_valid=True,
                issued_at=1,
                expires_at=4_000_000_000,
                scopes=["threads_basic"],
            )
        ),
    )


@pytest.fixture
def connected(db_path, monkeypatch):
    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    pause = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.asyncio.sleep", pause)
    service = ThreadsService(db_path)
    server = SocialMCPServer("test")
    register_threads_tools(server, service, ThreadsConnection(service))
    return server, service, pause
