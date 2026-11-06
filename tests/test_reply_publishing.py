import asyncio
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

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
    register_threads_tools,
)
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ContainerStatus
from tests.support import mock_http

TOKEN = "synthetic-reply-publishing-token"
TEXT = "  An exact reply & a link\nhttps://example.com/?a=1&b=2 😀  "


@pytest.fixture
def reply_server(db_path, monkeypatch):
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


@pytest.mark.parametrize("parent_id", ["456", "789"])
def test_mcp_reply_targets_exact_post_or_comment(
    reply_server, monkeypatch, caplog, parent_id
):
    server, _, pause = reply_server
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url) and TEXT not in str(request.url)
        if len(requests) == 1:
            assert request.method == "POST" and request.url.path == "/v1.0/123/threads"
            assert not request.url.query
            assert parse_qs(request.content.decode()) == {
                "media_type": ["TEXT"],
                "text": [TEXT],
                "auto_publish_text": ["false"],
                "reply_to_id": [parent_id],
            }
            return httpx.Response(200, json={"id": "111"})
        if len(requests) == 2:
            assert request.method == "GET" and request.url.path == "/v1.0/111"
            return httpx.Response(200, json={"status": "FINISHED"})
        assert len(requests) == 3
        assert (
            request.method == "POST" and request.url.path == "/v1.0/123/threads_publish"
        )
        assert not request.url.query
        assert parse_qs(request.content.decode()) == {"creation_id": ["111"]}
        return httpx.Response(200, json={"id": "222", "access_token": TOKEN})

    mock_http(monkeypatch, respond)
    caplog.set_level(5)
    result = asyncio.run(
        server.call_tool(
            "threads_reply",
            {
                "account_id": "123",
                "reply_to_id": parent_id,
                "text": TEXT,
            },
        )
    )
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.structured_content == {"post_id": "222"}
    assert result.is_error is False
    assert len(requests) == 3
    pause.assert_awaited_once_with(30)
    assert TOKEN not in result.model_dump_json() and TOKEN not in caplog.text
    assert TEXT not in caplog.text


@pytest.mark.parametrize("parent_id", ["", TOKEN, "https://example.com", 456, None])
def test_invalid_parent_is_hidden_and_never_loads_token(
    reply_server, monkeypatch, parent_id
):
    server, service, _ = reply_server
    get = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get)
    monkeypatch.setattr("social_mcp.threads.service.create_text_container", create)
    with pytest.raises(ToolError, match="reply_to_id") as caught:
        asyncio.run(
            server.call_tool(
                "threads_reply",
                {"account_id": "123", "reply_to_id": parent_id, "text": TEXT},
            )
        )
    assert TOKEN not in str(caught.value)
    with pytest.raises(ThreadsError, match="numeric reply_to_id"):
        asyncio.run(service.reply("123", parent_id, TEXT))
    get.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.parametrize("text", ["", " \n\t", "x" * 501, 123, None])
def test_invalid_reply_text_is_rejected_before_token_access(
    reply_server, monkeypatch, text
):
    server, service, _ = reply_server
    get = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get)
    with pytest.raises(ToolError, match="non-blank text"):
        asyncio.run(
            server.call_tool(
                "threads_reply",
                {"account_id": "123", "reply_to_id": "456", "text": text},
            )
        )
    with pytest.raises(ThreadsError, match="non-blank text"):
        asyncio.run(service.reply("123", "456", text))
    get.assert_not_awaited()


@pytest.mark.parametrize("expired", [False, True])
def test_reply_requires_usable_connected_account(db_path, monkeypatch, expired):
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
    create = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.create_text_container", create)
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(ThreadsService(db_path).reply("123", "456", TEXT))
    create.assert_not_awaited()


def test_reply_uses_current_token_and_preserves_parent(reply_server, monkeypatch):
    _, service, _ = reply_server
    account = Account(
        platform="threads",
        account_id="123",
        username="example",
        access_token=SecretStr("synthetic-current-reply-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    get = AsyncMock(return_value=account)
    create = AsyncMock(return_value="111")
    publish = AsyncMock(return_value="222")
    monkeypatch.setattr(service.tokens, "get_account", get)
    monkeypatch.setattr("social_mcp.threads.service.create_text_container", create)
    monkeypatch.setattr(
        "social_mcp.threads.publishing.fetch_container_status",
        AsyncMock(return_value=ContainerStatus(status="FINISHED")),
    )
    monkeypatch.setattr("social_mcp.threads.publishing.publish_container", publish)
    result = asyncio.run(service.reply("123", "789", TEXT))
    assert result.post_id == "222"
    get.assert_awaited_once_with("123")
    create.assert_awaited_once_with(
        "synthetic-current-reply-token", "123", TEXT, reply_to_id="789"
    )
    publish.assert_awaited_once_with(
        "synthetic-current-reply-token", "123", "111", reply_to_id="789"
    )


@pytest.mark.parametrize("state", ["IN_PROGRESS", "ERROR", "EXPIRED", "PUBLISHED"])
def test_reply_never_publishes_an_unready_or_published_container(
    reply_server, monkeypatch, state
):
    _, service, _ = reply_server
    publish = AsyncMock()
    monkeypatch.setattr(
        "social_mcp.threads.service.create_text_container",
        AsyncMock(return_value="111"),
    )
    monkeypatch.setattr(
        "social_mcp.threads.publishing.fetch_container_status",
        AsyncMock(return_value=ContainerStatus(status=state)),
    )
    monkeypatch.setattr("social_mcp.threads.publishing.publish_container", publish)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(service.reply("123", "789", TEXT))
    if state == "PUBLISHED":
        assert "threads_list_replies" in str(caught.value)
        assert "reply_to_id=789" in str(caught.value)
    publish.assert_not_awaited()


@pytest.mark.parametrize("failure", ["timeout", "network", "server", "malformed"])
def test_uncertain_reply_checks_parent_replies_not_own_posts(
    reply_server, monkeypatch, failure
):
    server, _, _ = reply_server
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "111"})
        if request.method == "GET":
            return httpx.Response(200, json={"status": "FINISHED"})
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "network":
            raise httpx.ConnectError(TOKEN, request=request)
        if failure == "server":
            return httpx.Response(503, json={"error": {"message": TOKEN}})
        return httpx.Response(200, text=TOKEN)

    mock_http(monkeypatch, respond)
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server.call_tool(
                "threads_reply",
                {"account_id": "123", "reply_to_id": "789", "text": TEXT},
            )
        )
    message = str(caught.value)
    assert "outcome is unknown" in message and "Do not retry" in message
    assert "threads_list_replies" in message and "reply_to_id=789" in message
    assert "threads_list_posts" not in message
    assert TOKEN not in message and TEXT not in message
    assert len(requests) == 3


@pytest.mark.parametrize(
    "status, code, hint",
    [
        (403, 10, "threads_manage_replies"),
        (401, 190, "token"),
        (429, 4, "rate limit"),
        (400, 100, "parameters"),
    ],
)
def test_reply_creation_denial_never_reaches_publication(
    reply_server, monkeypatch, status, code, hint
):
    server, _, _ = reply_server
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"code": code, "message": TOKEN}})

    mock_http(monkeypatch, respond)
    with pytest.raises(ToolError, match=hint) as caught:
        asyncio.run(
            server.call_tool(
                "threads_reply",
                {"account_id": "123", "reply_to_id": "789", "text": TEXT},
            )
        )
    assert TOKEN not in str(caught.value)
    assert len(requests) == 1


def test_reply_cancellation_never_retries_publication(reply_server, monkeypatch):
    _, service, _ = reply_server
    monkeypatch.setattr(
        "social_mcp.threads.service.create_text_container",
        AsyncMock(return_value="111"),
    )
    monkeypatch.setattr(
        "social_mcp.threads.publishing.fetch_container_status",
        AsyncMock(return_value=ContainerStatus(status="FINISHED")),
    )
    publish = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr("social_mcp.threads.publishing.publish_container", publish)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.reply("123", "789", TEXT))
    publish.assert_awaited_once()
