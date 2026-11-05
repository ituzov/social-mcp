import asyncio
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import SecretStr

from social_mcp.accounts import Account, save_account
from social_mcp.threads import (
    ThreadsService,
    client,
)
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ContainerStatus, TextPostInput
from tests.support import mock_http

TOKEN = "synthetic-publishing-secret"
TEXT = "  Привет & hello\nhttps://example.com/?a=1&b=2 😀  "


def test_publish_complete_mcp_flow_exact_text_and_no_secret_leaks(
    connected, monkeypatch, caplog
):
    server, _, pause = connected
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TEXT not in str(request.url) and TOKEN not in str(request.url)
        if len(requests) == 1:
            assert request.method == "POST" and request.url.path == "/v1.0/123/threads"
            assert not request.url.query
            assert request.headers["Content-Type"].startswith(
                "application/x-www-form-urlencoded"
            )
            assert parse_qs(request.content.decode()) == {
                "media_type": ["TEXT"],
                "text": [TEXT],
                "auto_publish_text": ["false"],
            }
            return httpx.Response(200, json={"id": "456", "access_token": TOKEN})
        if len(requests) == 2:
            assert request.method == "GET" and request.url.path == "/v1.0/456"
            assert dict(request.url.params) == {"fields": "status"}
            return httpx.Response(200, json={"status": "FINISHED"})
        assert len(requests) == 3
        assert (
            request.method == "POST" and request.url.path == "/v1.0/123/threads_publish"
        )
        assert not request.url.query
        assert parse_qs(request.content.decode()) == {"creation_id": ["456"]}
        return httpx.Response(200, json={"id": "789", "access_token": TOKEN})

    mock_http(monkeypatch, respond)
    caplog.set_level(5)
    result = asyncio.run(
        server.call_tool("threads_publish_text", {"account_id": "123", "text": TEXT})
    )
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.is_error is False
    assert result.structured_content == {"post_id": "789"}
    assert len(requests) == 3
    pause.assert_awaited_once_with(30)
    assert TOKEN not in result.model_dump_json() and TOKEN not in caplog.text
    assert TEXT not in caplog.text


@pytest.mark.parametrize("text", ["", " \n\t", "x" * 501, 123, True, None])
def test_invalid_text_never_loads_credentials_or_creates_container(
    connected, monkeypatch, text
):
    server, service, _ = connected
    get = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get)
    monkeypatch.setattr("social_mcp.threads.service.create_text_container", create)
    with pytest.raises(ToolError, match="non-blank text"):
        asyncio.run(
            server.call_tool(
                "threads_publish_text", {"account_id": "123", "text": text}
            )
        )
    with pytest.raises(ThreadsError, match="non-blank text"):
        asyncio.run(service.publish_text("123", text))
    get.assert_not_awaited()
    create.assert_not_awaited()


def test_invalid_account_id_is_hidden_before_token_access(connected, monkeypatch):
    server, service, _ = connected
    get = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get)
    with pytest.raises(ToolError, match="account_id") as caught:
        asyncio.run(
            server.call_tool(
                "threads_publish_text", {"account_id": TOKEN, "text": "Hello"}
            )
        )
    assert TOKEN not in str(caught.value)
    get.assert_not_awaited()


@pytest.mark.parametrize("text", ["a" * 500, "я" * 500, "😀", "a\nb", "  text  "])
def test_text_validation_preserves_approved_content(text):
    assert TextPostInput(text=text).text == text


@pytest.mark.parametrize(
    "status", ["IN_PROGRESS", "ERROR", "EXPIRED", "NEW_UNKNOWN_STATUS", "PUBLISHED"]
)
def test_unready_or_already_published_container_is_not_published_again(
    connected, monkeypatch, status
):
    _, service, _ = connected
    create = AsyncMock(return_value="456")
    publish = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.create_text_container", create)
    monkeypatch.setattr(
        "social_mcp.threads.service.fetch_container_status",
        AsyncMock(return_value=ContainerStatus(status=status)),
    )
    monkeypatch.setattr("social_mcp.threads.service.publish_container", publish)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(service.publish_text("123", "Hello"))
    assert "No publication was attempted" in str(
        caught.value
    ) or "outcome is unknown" in str(caught.value)
    publish.assert_not_awaited()
    create.assert_awaited_once()


@pytest.mark.parametrize("expired", [False, True])
def test_missing_or_expired_connection_cannot_create_container(
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
    create = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.create_text_container", create)
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(ThreadsService(db_path).publish_text("123", "Hello"))
    create.assert_not_awaited()


def test_publishing_uses_current_token_returned_by_lifecycle(connected, monkeypatch):
    _, service, _ = connected
    account = Account(
        platform="threads",
        account_id="123",
        username="example",
        access_token=SecretStr("synthetic-renewed-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    get = AsyncMock(return_value=account)
    create = AsyncMock(return_value="456")
    publish = AsyncMock(return_value="789")
    status = AsyncMock(return_value=ContainerStatus(status="FINISHED"))
    monkeypatch.setattr(service.tokens, "get_account", get)
    monkeypatch.setattr("social_mcp.threads.service.create_text_container", create)
    monkeypatch.setattr("social_mcp.threads.service.fetch_container_status", status)
    monkeypatch.setattr("social_mcp.threads.service.publish_container", publish)
    result = asyncio.run(service.publish_text("123", TEXT))
    assert result.post_id == "789"
    get.assert_awaited_once_with("123")
    create.assert_awaited_once_with(
        "synthetic-renewed-token", "123", TEXT, reply_to_id=None
    )
    status.assert_awaited_once_with("synthetic-renewed-token", "456")
    publish.assert_awaited_once_with(
        "synthetic-renewed-token", "123", "456", reply_to_id=None
    )


@pytest.mark.parametrize("phase", ["create", "status", "publish"])
@pytest.mark.parametrize("failure", ["timeout", "network", "server", "malformed"])
def test_failures_stop_flow_without_retry_and_hide_secrets(
    connected, monkeypatch, phase, failure
):
    server, _, _ = connected
    requests = []

    def respond(request):
        requests.append(request)
        actual_phase = (
            "create"
            if request.url.path.endswith("/threads")
            else "publish"
            if request.url.path.endswith("/threads_publish")
            else "status"
        )
        if actual_phase == phase:
            if failure == "timeout":
                raise httpx.ReadTimeout(TOKEN, request=request)
            if failure == "network":
                raise httpx.ConnectError(TOKEN, request=request)
            if failure == "server":
                return httpx.Response(503, json={"error": {"message": TOKEN}})
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(
            200,
            json={"id": "456"} if actual_phase == "create" else {"status": "FINISHED"},
        )

    mock_http(monkeypatch, respond)
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server.call_tool(
                "threads_publish_text", {"account_id": "123", "text": "Hello"}
            )
        )
    message = str(caught.value)
    assert TOKEN not in message
    assert len(requests) == {"create": 1, "status": 2, "publish": 3}[phase]
    if phase == "publish":
        assert "outcome is unknown" in message and "Do not retry" in message
        assert "456" in message and "threads_list_posts" in message
        assert "Try again later" not in message


@pytest.mark.parametrize("phase", ["create", "publish"])
@pytest.mark.parametrize(
    "status, code, hint",
    [
        (401, 190, "token"),
        (403, 10, "threads_content_publish"),
        (429, 4, "rate limit"),
        (400, 100, "parameters"),
    ],
)
def test_write_api_rejections_do_not_retry(monkeypatch, phase, status, code, hint):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"code": code, "message": TOKEN}})

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError) as caught:
        if phase == "create":
            asyncio.run(client.create_text_container(TOKEN, "123", "Hello"))
        else:
            asyncio.run(client.publish_container(TOKEN, "123", "456"))
    assert TOKEN not in str(caught.value)
    assert hint in str(caught.value) or (
        phase == "publish" and "outcome is unknown" in str(caught.value)
    )
    assert len(requests) == 1


@pytest.mark.parametrize("payload", [{}, {"id": 789}, {"id": TOKEN}, {"id": ""}])
def test_bad_publication_id_is_unknown_not_a_success(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ThreadsError, match="outcome is unknown") as caught:
        asyncio.run(client.publish_container(TOKEN, "123", "456"))
    assert TOKEN not in str(caught.value)


def test_write_does_not_follow_redirects(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            307, headers={"Location": "https://untrusted.example/collect"}
        )

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError):
        asyncio.run(client.create_text_container(TOKEN, "123", "Hello"))
    assert len(requests) == 1
    assert requests[0].url.host == "graph.threads.com"


def test_cancelled_publish_is_not_retried(connected, monkeypatch):
    _, service, _ = connected
    monkeypatch.setattr(
        "social_mcp.threads.service.create_text_container",
        AsyncMock(return_value="456"),
    )
    monkeypatch.setattr(
        "social_mcp.threads.service.fetch_container_status",
        AsyncMock(return_value=ContainerStatus(status="FINISHED")),
    )
    publish = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr("social_mcp.threads.service.publish_container", publish)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.publish_text("123", "Hello"))
    publish.assert_awaited_once()
