import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.types import CallToolResult
from pydantic import SecretStr

from social_mcp.accounts import Account, save_account
from social_mcp.threads import ThreadsService, client
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import PostsQuery, RepliesPage
from tests.support import mock_http

TOKEN = "synthetic-replies-token"


def test_replies_request_and_safe_page(monkeypatch, caplog):
    def respond(request):
        assert request.url.path == "/v1.0/123/replies"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert request.url.params["after"] == "previous"
        assert request.url.params["limit"] == "2"
        assert request.url.params["reverse"] == "true"
        assert "username" in request.url.params["fields"].split(",")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "456",
                        "username": "reader",
                        "text": "A reply",
                        "has_replies": True,
                        "is_reply_owned_by_me": False,
                        "replied_to": {"id": "123"},
                        "root_post": {"id": "123"},
                        "access_token": TOKEN,
                    }
                ],
                "paging": {
                    "cursors": {"after": "next"},
                    "next": f"https://graph.threads.com/next?access_token={TOKEN}",
                },
            },
        )

    mock_http(monkeypatch, respond)
    caplog.set_level(5)
    page = asyncio.run(
        client.fetch_replies(TOKEN, "123", PostsQuery(limit=2, after="previous"))
    )
    reply = page.items[0]
    assert reply.replied_to is not None
    assert reply.username == "reader"
    assert reply.replied_to.id == "123"
    assert reply.has_replies is True
    assert reply.is_reply_owned_by_me is False
    assert page.next_cursor == "next"
    assert TOKEN not in page.model_dump_json()
    assert "graph.threads.com" not in page.model_dump_json()
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("data", [[], [{"id": "456", "media_type": "IMAGE"}]])
def test_final_reply_page_with_optional_fields_missing(monkeypatch, data):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={"data": data}))
    page = asyncio.run(client.fetch_replies(TOKEN, "123", PostsQuery()))
    assert page.next_cursor is None
    if page.items:
        assert page.items[0].text is None
        assert page.items[0].username is None
        assert page.items[0].has_replies is None


def test_empty_reply_page_can_have_more_results(monkeypatch):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "data": [],
                "paging": {"cursors": {"after": "next"}, "next": "https://example.com"},
            },
        ),
    )
    page = asyncio.run(client.fetch_replies(TOKEN, "123", PostsQuery()))
    assert page.items == []
    assert page.next_cursor == "next"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {}},
        {"data": [{"id": 456}]},
        {"data": [{"id": "456", "has_replies": "false"}]},
        {"data": [{"id": "456", "root_post": TOKEN}]},
        {"data": [], "paging": {"next": "https://example.com"}},
        {
            "data": [],
            "paging": {"next": "https://example.com", "cursors": {"after": "same"}},
        },
    ],
)
def test_malformed_replies_and_nonadvancing_cursor_are_safe(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(client.fetch_replies(TOKEN, "123", PostsQuery(after="same")))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "status, message",
    [
        (400, "post or reply page"),
        (401, "token"),
        (403, "threads_read_replies"),
        (429, "rate limit"),
        (503, "Temporary"),
    ],
)
def test_reply_errors_are_actionable_and_safe(monkeypatch, status, message):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            status,
            json={
                "error": {"code": 100 if status == 400 else 0, "message": TOKEN},
            },
        ),
    )
    with pytest.raises(ThreadsError, match=message) as caught:
        asyncio.run(client.fetch_replies(TOKEN, "123", PostsQuery()))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"after": ""},
        {"after": "x" * 4097},
        {"after": 123},
    ],
)
def test_service_rejects_invalid_page_before_loading_token(
    db_path, monkeypatch, arguments
):

    service = ThreadsService(db_path)
    get_account = AsyncMock()
    fetch = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get_account)
    monkeypatch.setattr("social_mcp.threads.service.fetch_replies", fetch)
    with pytest.raises(ThreadsError, match="limit from 1 to 100"):
        asyncio.run(service.list_replies("123", "456", **arguments))
    get_account.assert_not_awaited()
    fetch.assert_not_awaited()


@pytest.mark.parametrize("expired", [False, True])
def test_service_requires_usable_connected_account(db_path, monkeypatch, expired):

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
    monkeypatch.setattr("social_mcp.threads.service.fetch_replies", fetch)
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(ThreadsService(db_path).list_replies("123", "456"))
    fetch.assert_not_awaited()


def test_service_uses_current_token_and_selected_post(db_path, monkeypatch):

    service = ThreadsService(db_path)
    account = Account(
        platform="threads",
        account_id="123",
        username="example",
        access_token=SecretStr("synthetic-renewed-reply-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    get_account = AsyncMock(return_value=account)
    page = RepliesPage(items=[], next_cursor="next")
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(service.tokens, "get_account", get_account)
    monkeypatch.setattr("social_mcp.threads.service.fetch_replies", fetch)
    result = asyncio.run(service.list_replies("123", "456", limit=2, after="previous"))
    assert result is page
    get_account.assert_awaited_once_with("123")
    fetch.assert_awaited_once_with(
        "synthetic-renewed-reply-token", "456", PostsQuery(limit=2, after="previous")
    )


def reply_server(db_path):
    from social_mcp.mcp_server import SocialMCPServer
    from social_mcp.threads import ThreadsConnection, register_threads_tools

    service = ThreadsService(db_path)
    server = SocialMCPServer("test")
    register_threads_tools(server, service, ThreadsConnection(service))
    return server, service


def test_replies_through_mcp_with_pagination(db_path, monkeypatch):
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
        assert request.url.path == "/v1.0/456/replies"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["limit"] == "2"
        if "after" in request.url.params:
            assert request.url.params["after"] == "next"
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "789",
                        "text": "A reply",
                        "username": "reader",
                        "access_token": TOKEN,
                    }
                ],
                "paging": {
                    "cursors": {"after": "next"},
                    "next": f"https://graph.threads.com/next?access_token={TOKEN}",
                },
            },
        )

    mock_http(monkeypatch, respond)
    server, _ = reply_server(db_path)

    async def check():
        first = await server.call_tool(
            "threads_list_replies", {"account_id": "123", "post_id": "456", "limit": 2}
        )
        assert isinstance(first, CallToolResult)
        assert first.structured_content is not None
        assert first.is_error is False
        assert first.structured_content["items"][0]["text"] == "A reply"
        assert first.structured_content["next_cursor"] == "next"
        assert TOKEN not in first.model_dump_json()
        assert "graph.threads.com" not in first.model_dump_json()
        second = await server.call_tool(
            "threads_list_replies",
            {
                "account_id": "123",
                "post_id": "456",
                "limit": 2,
                "after": first.structured_content["next_cursor"],
            },
        )
        assert isinstance(second, CallToolResult)
        assert second.structured_content is not None
        assert second.structured_content == {"items": [], "next_cursor": None}

    asyncio.run(check())
    assert len(requests) == 2


@pytest.mark.parametrize(
    "arguments, hint",
    [
        ({"account_id": TOKEN, "post_id": "456"}, "account_id"),
        ({"account_id": "123", "post_id": TOKEN}, "post_id"),
        ({"account_id": "123"}, "post_id"),
        ({"account_id": "123", "post_id": "456", "limit": True}, "integer"),
        ({"account_id": "123", "post_id": "456", "limit": 101}, "integer"),
        ({"account_id": "123", "post_id": "456", "after": ""}, "next_cursor"),
    ],
)
def test_mcp_replies_reject_invalid_arguments_before_credentials(
    db_path, monkeypatch, arguments, hint
):
    from mcp.server.mcpserver.exceptions import ToolError

    server, service = reply_server(db_path)
    get_account = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get_account)
    with pytest.raises(ToolError, match=hint) as caught:
        asyncio.run(server.call_tool("threads_list_replies", arguments))
    assert TOKEN not in str(caught.value)
    get_account.assert_not_awaited()


def test_mcp_replies_translate_service_error(db_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    server, service = reply_server(db_path)
    monkeypatch.setattr(
        service,
        "list_replies",
        AsyncMock(side_effect=ThreadsError("Check threads_read_replies.")),
    )
    with pytest.raises(ToolError, match="threads_read_replies"):
        asyncio.run(
            server.call_tool(
                "threads_list_replies", {"account_id": "123", "post_id": "456"}
            )
        )
