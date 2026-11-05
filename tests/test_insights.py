import asyncio
import traceback
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import SecretStr

from social_mcp.accounts import Account, save_account
from social_mcp.threads import ThreadsConnection, client, register_threads_tools
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import PostInsights
from social_mcp.threads.service import ThreadsService
from tests.support import mock_http

TOKEN = "synthetic-insights-token"


def insights_server(db_path):
    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    service = ThreadsService(db_path)
    server = MCPServer("test")
    register_threads_tools(server, service, ThreadsConnection(service))
    return server


def test_insights_through_mcp_returns_public_counters(db_path, monkeypatch):
    def respond(request):
        assert request.url.path == "/v1.0/456/insights"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "likes", "values": [{"value": 0}], "secret": TOKEN},
                    {"name": "views", "values": [{"value": 42}]},
                ]
            },
        )

    mock_http(monkeypatch, respond)
    server = insights_server(db_path)
    result = asyncio.run(
        server.call_tool(
            "threads_get_post_insights", {"account_id": "123", "post_id": "456"}
        )
    )
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None

    assert result.is_error is False
    assert result.structured_content == dict(
        post_id="456",
        views=42,
        likes=0,
        replies=None,
        reposts=None,
        quotes=None,
        shares=None,
    )
    assert TOKEN not in result.model_dump_json()


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_insights_mcp_translates_api_errors_without_secrets(
    db_path, monkeypatch, status
):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(status, json={"error": {"message": TOKEN}}),
    )
    server = insights_server(db_path)
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server.call_tool(
                "threads_get_post_insights", {"account_id": "123", "post_id": "456"}
            )
        )
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


def test_service_uses_current_credentials_and_selected_post(db_path, monkeypatch):
    service = ThreadsService(db_path)
    account = Account(
        platform="threads",
        account_id="123",
        username="example",
        access_token=SecretStr("synthetic-renewed-insights-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    get_account = AsyncMock(return_value=account)
    fetch = AsyncMock(return_value=PostInsights(post_id="456", likes=42))
    monkeypatch.setattr(service.tokens, "get_account", get_account)
    monkeypatch.setattr("social_mcp.threads.service.fetch_post_insights", fetch)

    result = asyncio.run(service.get_post_insights("123", "456"))

    get_account.assert_awaited_once_with("123")
    fetch.assert_awaited_once_with("synthetic-renewed-insights-token", "456")
    assert result is fetch.return_value


@pytest.mark.parametrize("expired", [False, True])
def test_insights_service_blocks_missing_or_expired_account(
    db_path, monkeypatch, expired
):
    if expired:
        save_account(
            db_path,
            platform="threads",
            account_id="123",
            username="example",
            access_token=TOKEN,
            obtained_at=1,
            expires_at=2,
        )
    fetch = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.fetch_post_insights", fetch)

    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(ThreadsService(db_path).get_post_insights("123", "456"))

    fetch.assert_not_awaited()


def test_insights_request_and_public_counters(monkeypatch, caplog):
    def respond(request):
        assert request.url.path == "/v1.0/123/insights"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert set(request.url.params["metric"].split(",")) == {
            "views",
            "likes",
            "replies",
            "reposts",
            "quotes",
            "shares",
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "views", "values": [{"value": 100}], "description": TOKEN},
                    {"name": "likes", "values": [{"value": 0}]},
                    {"name": "shares", "values": []},
                    {"name": "future_metric", "new_shape": TOKEN},
                ],
                "access_token": TOKEN,
            },
        )

    mock_http(monkeypatch, respond)
    caplog.set_level(5)
    result = asyncio.run(client.fetch_post_insights(TOKEN, "123"))
    assert result.model_dump() == dict(
        post_id="123",
        views=100,
        likes=0,
        replies=None,
        reposts=None,
        quotes=None,
        shares=None,
    )
    assert TOKEN not in result.model_dump_json()
    assert TOKEN not in caplog.text


def test_empty_insights_are_unavailable_not_zero(monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))
    result = asyncio.run(client.fetch_post_insights(TOKEN, "123"))
    assert all(
        value is None
        for name, value in result.model_dump().items()
        if name != "post_id"
    )


@pytest.mark.parametrize(
    "entries",
    [
        None,
        {},
        [None],
        [{"name": []}],
        [{"name": "likes"}],
        [{"name": "likes", "values": [{"value": -1}]}],
        [{"name": "likes", "values": [{"value": "10"}]}],
        [{"name": "likes", "values": [{"value": True}]}],
        [{"name": "likes", "values": [{}]}],
        [{"name": "likes", "values": [{"value": 1}, {"value": 2}]}],
        [{"name": "likes", "values": [{"value": 1}]}] * 2,
    ],
)
def test_malformed_insights_fail_safely(monkeypatch, entries):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": entries, "secret": TOKEN}),
    )
    with pytest.raises(ThreadsError, match="post insights format") as caught:
        asyncio.run(client.fetch_post_insights(TOKEN, "123"))
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_insights_api_errors_are_safe_and_not_retried(monkeypatch, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": TOKEN}})

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(client.fetch_post_insights(TOKEN, "123"))
    assert len(requests) == 1
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
