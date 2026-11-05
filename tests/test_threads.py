import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from social_mcp.accounts import AccountStorageError, save_account
from social_mcp.threads import ThreadsConnection, ThreadsService, register_threads_tools
from social_mcp.threads import client as threads_client
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ThreadsProfile
from tests.support import mock_http

pytestmark = pytest.mark.usefixtures("verified_token")

TOKEN = "synthetic-profile-test-secret"


def save(path, *, expires_at=None):
    save_account(
        path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=1,
        expires_at=expires_at,
    )


def test_profile_flows_through_service_and_mcp_without_token(db_path, monkeypatch):
    save(db_path)
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert request.url.host == "graph.threads.com"
        assert request.url.path == "/v1.0/me"
        return httpx.Response(
            200,
            json={
                "id": "123",
                "username": "example",
                "is_verified": False,
                "access_token": TOKEN,
            },
        )

    mock_http(monkeypatch, respond)
    server = MCPServer("test")
    service = ThreadsService(db_path)
    register_threads_tools(server, service, ThreadsConnection(service))
    result = asyncio.run(server.call_tool("threads_get_profile", {"account_id": "123"}))
    assert isinstance(result, CallToolResult)
    assert len(requests) == 1
    assert "example" in result.model_dump_json()
    assert TOKEN not in result.model_dump_json()


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, 190, "token"),
        (401, None, "token"),
        (400, 4, "rate limit"),
        (429, None, "rate limit"),
        (403, 10, "permissions"),
        (503, 2, "HTTP 503"),
        (200, 190, "token"),
    ],
)
def test_api_errors_do_not_expose_raw_message(monkeypatch, status, code, expected):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            status, json={"error": {"code": code, "message": TOKEN}}
        ),
    )
    with pytest.raises(ThreadsError, match=expected) as caught:
        asyncio.run(threads_client.fetch_profile(TOKEN))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_network_errors_do_not_expose_credentials(monkeypatch, error_type):
    def respond(request):
        raise error_type(TOKEN, request=request)

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(threads_client.fetch_profile(TOKEN))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("payload", [{"id": [TOKEN], "username": "example"}, None])
def test_unexpected_response(monkeypatch, payload):
    mock_http(
        monkeypatch,
        lambda request: (
            httpx.Response(200, json=payload)
            if payload is not None
            else httpx.Response(200, text=TOKEN)
        ),
    )
    with pytest.raises(ThreadsError, match="format") as caught:
        asyncio.run(threads_client.fetch_profile(TOKEN))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("expired", [False, True])
def test_missing_or_expired_account_does_not_call_api(db_path, monkeypatch, expired):
    if expired:
        save(db_path, expires_at=2)
    fetch = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.fetch_profile", fetch)
    with pytest.raises(ThreadsError, match="expired|is connected"):
        asyncio.run(ThreadsService(db_path).get_profile("123"))
    fetch.assert_not_awaited()


def test_profile_must_belong_to_selected_account(db_path, monkeypatch):
    save(db_path)
    monkeypatch.setattr(
        "social_mcp.threads.service.fetch_profile",
        AsyncMock(return_value=ThreadsProfile(id="456", username="other")),
    )
    with pytest.raises(ThreadsError, match="different Threads account"):
        asyncio.run(ThreadsService(db_path).get_profile("123"))


def test_storage_error_is_translated_at_service_boundary(db_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise AccountStorageError(TOKEN)

    monkeypatch.setattr("social_mcp.threads.tokens.get_account", fail)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(ThreadsService(db_path).get_profile("123"))
    assert TOKEN not in str(caught.value)


def test_tool_translates_domain_error(db_path):
    server = MCPServer("test")
    service = ThreadsService(db_path)
    register_threads_tools(server, service, ThreadsConnection(service))
    with pytest.raises(ToolError, match="is connected"):
        asyncio.run(server.call_tool("threads_get_profile", {"account_id": "999"}))
