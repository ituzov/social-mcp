import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import SecretStr

from social_mcp.accounts import Account, save_account
from social_mcp.threads import ThreadsConnection, ThreadsService, register_threads_tools
from social_mcp.threads import client as threads_client
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import PostsPage, PostsQuery
from tests.support import mock_http

TOKEN = "synthetic-posts-token"
POST = {
    "id": "post-1",
    "text": "Hello Threads",
    "timestamp": "2026-11-04T00:00:00+0000",
    "media_type": "TEXT_POST",
    "permalink": "https://www.threads.com/@example/post/test",
}


def server_for(db_path):
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


def test_posts_flow_from_selected_account_through_mcp_and_pagination(
    db_path, monkeypatch, caplog
):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/v1.0/123/threads"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert request.url.params["limit"] == "2"
        assert request.url.params["fields"] == (
            "id,text,timestamp,media_type,permalink,is_quote_post,quoted_post,reposted_post,text_attachment"
        )
        if "after" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [{**POST, "access_token": TOKEN}],
                    "paging": {
                        "cursors": {"after": "page-2"},
                        "next": f"https://graph.threads.com/v1.0/123/threads?access_token={TOKEN}&after=page-2",
                    },
                },
            )
        assert request.url.params["after"] == "page-2"
        return httpx.Response(
            200, json={"data": [], "paging": {"cursors": {"after": "end"}}}
        )

    mock_http(monkeypatch, respond)
    caplog.set_level(5)
    server = server_for(db_path)

    async def check():
        first = await server.call_tool(
            "threads_list_posts", {"account_id": "123", "limit": 2}
        )
        assert isinstance(first, CallToolResult)
        assert first.structured_content is not None
        assert first.is_error is False
        assert first.structured_content["items"][0]["text"] == "Hello Threads"
        assert first.structured_content["next_cursor"] == "page-2"
        assert TOKEN not in first.model_dump_json()
        assert "access_token" not in first.model_dump_json()
        second = await server.call_tool(
            "threads_list_posts",
            {
                "account_id": "123",
                "limit": 2,
                "after": first.structured_content["next_cursor"],
            },
        )
        assert isinstance(second, CallToolResult)
        assert second.structured_content is not None
        assert second.structured_content == {"items": [], "next_cursor": None}

    asyncio.run(check())
    assert len(requests) == 2
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"id": "image", "media_type": "IMAGE"}]},
        {"data": []},
        {
            "data": [],
            "paging": {
                "cursors": {"after": "more"},
                "next": "https://graph.threads.com/next",
            },
        },
    ],
)
def test_missing_optional_fields_and_empty_pages(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    page = asyncio.run(threads_client.fetch_posts(TOKEN, "123", PostsQuery()))
    if payload["data"]:
        assert page.items[0].permalink is None
        assert page.items[0].text is None
    else:
        assert page.items == []
    assert page.next_cursor == ("more" if "paging" in payload else None)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"data": {}},
        {"data": [{"id": 123}]},
        {"data": [], "paging": {"cursors": {"after": 123}}},
        {"data": [], "paging": {"next": f"https://example.com/?access_token={TOKEN}"}},
    ],
)
def test_bad_response_is_safe(monkeypatch, payload):
    mock_http(
        monkeypatch,
        lambda request: (
            httpx.Response(200, json=payload)
            if payload is not None
            else httpx.Response(200, text=TOKEN)
        ),
    )
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(threads_client.fetch_posts(TOKEN, "123", PostsQuery()))

    assert TOKEN not in str(caught.value)


def test_repeated_cursor_cannot_create_infinite_pagination(monkeypatch):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "data": [POST],
                "paging": {"cursors": {"after": "same"}, "next": "https://example.com"},
            },
        ),
    )

    with pytest.raises(ThreadsError, match="pagination"):
        asyncio.run(threads_client.fetch_posts(TOKEN, "123", PostsQuery(after="same")))


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
def test_invalid_query_is_rejected_before_token_or_network(
    db_path, monkeypatch, arguments
):
    service = ThreadsService(db_path)
    get = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get)
    monkeypatch.setattr(
        "social_mcp.threads.service.fetch_posts",
        AsyncMock(return_value=PostsPage(items=[])),
    )

    with pytest.raises(ThreadsError):
        asyncio.run(service.list_posts("123", **arguments))
    get.assert_not_awaited()


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_post_api_errors_are_translated_safely(db_path, monkeypatch, status):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(status, json={"error": {"message": TOKEN}}),
    )
    server = server_for(db_path)
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("threads_list_posts", {"account_id": "123"}))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("expired", [False, True])
def test_missing_or_expired_account_cannot_call_posts_api(
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

    def unexpected(_request):
        pytest.fail("Missing/expired account must not reach posts API")

    mock_http(monkeypatch, unexpected)
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(ThreadsService(db_path).list_posts("123"))


def test_posts_use_credentials_returned_by_token_lifecycle(db_path, monkeypatch):
    service = ThreadsService(db_path)
    account = Account(
        platform="threads",
        account_id="123",
        username="example",
        access_token=SecretStr("synthetic-renewed-posts-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    get = AsyncMock(return_value=account)
    monkeypatch.setattr(service.tokens, "get_account", get)

    def respond(request):
        assert (
            request.headers["Authorization"]
            == f"Bearer {account.access_token.get_secret_value()}"
        )
        return httpx.Response(200, json={"data": []})

    mock_http(monkeypatch, respond)
    asyncio.run(service.list_posts("123"))
    get.assert_awaited_once_with("123")


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 101}, {"limit": True}, {"after": ""}, {"after": 123}],
)
def test_mcp_rejects_invalid_query_before_loading_credentials(
    db_path, monkeypatch, arguments
):
    service = ThreadsService(db_path)
    get = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get)
    server = MCPServer("test")
    register_threads_tools(server, service, ThreadsConnection(service))
    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool("threads_list_posts", {"account_id": "123", **arguments})
        )
    get.assert_not_awaited()
