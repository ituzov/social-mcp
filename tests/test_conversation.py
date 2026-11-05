import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import SecretStr

from social_mcp.accounts import Account, save_account
from social_mcp.mcp_server import SocialMCPServer
from social_mcp.threads import (
    ThreadsConnection,
    ThreadsService,
    client,
    register_threads_tools,
)
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import PostsQuery, RepliesPage
from tests.support import mock_http

TOKEN = "synthetic-conversation-token"


def conversation_server(db_path):
    service = ThreadsService(db_path)
    server = SocialMCPServer("test")
    register_threads_tools(server, service, ThreadsConnection(service))
    return server, service


def test_conversation_mcp_pages_preserve_nested_parent_on_other_page(
    db_path, monkeypatch, caplog
):
    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/v1.0/456/conversation"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert request.url.params["limit"] == "1"
        assert request.url.params["reverse"] == "true"
        assert {"root_post", "replied_to", "username"} <= set(
            request.url.params["fields"].split(",")
        )
        if "after" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "999",
                            "text": "Nested reply",
                            "username": "example",
                            "root_post": {"id": "456"},
                            "replied_to": {"id": "789"},
                            "access_token": TOKEN,
                        }
                    ],
                    "paging": {
                        "cursors": {"after": "next"},
                        "next": f"https://graph.threads.com/next?access_token={TOKEN}",
                    },
                },
            )
        assert request.url.params["after"] == "next"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "789",
                        "text": "Direct reply",
                        "root_post": {"id": "456"},
                        "replied_to": {"id": "456"},
                        "has_replies": True,
                    }
                ],
                "paging": {"cursors": {"after": "end"}},
            },
        )

    mock_http(monkeypatch, respond)
    caplog.set_level(5)
    server, _ = conversation_server(db_path)

    async def check():
        first = await server.call_tool(
            "threads_get_conversation",
            {"account_id": "123", "post_id": "456", "limit": 1},
        )
        assert isinstance(first, CallToolResult)
        assert first.structured_content is not None
        assert first.is_error is False
        nested = first.structured_content["items"][0]
        assert nested["replied_to"]["id"] == "789"
        assert nested["root_post"]["id"] == "456"
        assert first.structured_content["next_cursor"] == "next"
        second = await server.call_tool(
            "threads_get_conversation",
            {
                "account_id": "123",
                "post_id": "456",
                "limit": 1,
                "after": first.structured_content["next_cursor"],
            },
        )
        assert isinstance(second, CallToolResult)
        assert second.structured_content is not None
        assert second.is_error is False
        parent = second.structured_content["items"][0]
        assert parent["id"] == nested["replied_to"]["id"]
        assert parent["replied_to"]["id"] == "456"
        assert second.structured_content["next_cursor"] is None
        for result in (first, second):
            assert TOKEN not in result.model_dump_json()
            assert "graph.threads.com" not in result.model_dump_json()

    asyncio.run(check())
    assert len(requests) == 2
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "arguments, hint",
    [
        ({"account_id": TOKEN, "post_id": "456"}, "account_id"),
        ({"account_id": "123", "post_id": TOKEN}, "post_id"),
        ({"account_id": "123"}, "post_id"),
        ({"account_id": "123", "post_id": "456", "limit": True}, "integer"),
        ({"account_id": "123", "post_id": "456", "limit": 0}, "integer"),
        ({"account_id": "123", "post_id": "456", "limit": 101}, "integer"),
        ({"account_id": "123", "post_id": "456", "after": ""}, "next_cursor"),
    ],
)
def test_mcp_conversation_rejects_bad_arguments_before_credentials(
    db_path, monkeypatch, arguments, hint
):
    server, service = conversation_server(db_path)
    get = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get)
    with pytest.raises(ToolError, match=hint) as caught:
        asyncio.run(server.call_tool("threads_get_conversation", arguments))
    assert TOKEN not in str(caught.value)
    get.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"after": ""},
        {"after": "x" * 4097},
    ],
)
def test_service_conversation_validates_page_before_credentials(
    db_path, monkeypatch, arguments
):
    service = ThreadsService(db_path)
    get = AsyncMock()
    fetch = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get)
    monkeypatch.setattr("social_mcp.threads.service.fetch_conversation", fetch)
    with pytest.raises(ThreadsError, match="limit from 1 to 100"):
        asyncio.run(service.get_conversation("123", "456", **arguments))
    get.assert_not_awaited()
    fetch.assert_not_awaited()


@pytest.mark.parametrize("expired", [False, True])
def test_conversation_requires_usable_connection(db_path, monkeypatch, expired):
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
    monkeypatch.setattr("social_mcp.threads.service.fetch_conversation", fetch)
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(ThreadsService(db_path).get_conversation("123", "456"))
    fetch.assert_not_awaited()


def test_conversation_uses_current_lifecycle_token(db_path, monkeypatch):
    service = ThreadsService(db_path)
    account = Account(
        platform="threads",
        account_id="123",
        username="example",
        access_token=SecretStr("synthetic-current-conversation-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    get = AsyncMock(return_value=account)
    page = RepliesPage(items=[])
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(service.tokens, "get_account", get)
    monkeypatch.setattr("social_mcp.threads.service.fetch_conversation", fetch)
    assert (
        asyncio.run(service.get_conversation("123", "456", limit=2, after="next"))
        is page
    )
    get.assert_awaited_once_with("123")
    fetch.assert_awaited_once_with(
        "synthetic-current-conversation-token", "456", PostsQuery(limit=2, after="next")
    )


@pytest.mark.parametrize(
    "payload, cursor",
    [
        ({"data": []}, None),
        ({"data": [{"id": "789", "media_type": "IMAGE"}]}, None),
        (
            {
                "data": [],
                "paging": {"cursors": {"after": "next"}, "next": "https://example.com"},
            },
            "next",
        ),
    ],
)
def test_empty_pages_and_optional_metadata(monkeypatch, payload, cursor):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    page = asyncio.run(client.fetch_conversation(TOKEN, "456", PostsQuery()))
    assert page.next_cursor == cursor
    if page.items:
        assert page.items[0].text is None
        assert page.items[0].replied_to is None


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {}},
        {"data": [{"id": 123}]},
        {"data": [{"id": "789", "replied_to": TOKEN}]},
        {"data": [], "paging": {"next": "https://example.com"}},
        {
            "data": [],
            "paging": {"next": "https://example.com", "cursors": {"after": "same"}},
        },
    ],
)
def test_bad_conversation_data_and_nonadvancing_cursor_are_safe(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(client.fetch_conversation(TOKEN, "456", PostsQuery(after="same")))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "status, code, hint",
    [
        (400, 100, "root post_id"),
        (401, 190, "token"),
        (403, 10, "threads_read_replies"),
        (429, 4, "rate limit"),
        (503, 2, "Temporary"),
    ],
)
def test_conversation_api_errors_through_mcp(db_path, monkeypatch, status, code, hint):
    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            status, json={"error": {"code": code, "message": TOKEN}}
        ),
    )
    server, _ = conversation_server(db_path)
    with pytest.raises(ToolError, match=hint) as caught:
        asyncio.run(
            server.call_tool(
                "threads_get_conversation", {"account_id": "123", "post_id": "456"}
            )
        )
    assert TOKEN not in str(caught.value)
