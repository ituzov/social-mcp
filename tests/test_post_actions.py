import asyncio
import traceback
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import SecretStr

from social_mcp.accounts import Account, save_account
from social_mcp.threads import ThreadsService, client
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ContainerStatus, PostsQuery
from tests.support import PUBLISHING_TOKEN as TOKEN
from tests.support import TEXT, Connected, mock_http


def test_quote_mcp_flow_preserves_exact_text_and_original_id(
    connected: Connected, monkeypatch, caplog
):
    server, _, pause = connected
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        if len(requests) == 1:
            assert request.method == "POST" and request.url.path == "/v1.0/123/threads"
            assert not request.url.query
            assert parse_qs(request.content.decode()) == {
                "media_type": ["TEXT"],
                "text": [TEXT],
                "auto_publish_text": ["false"],
                "quote_post_id": ["456"],
            }
            return httpx.Response(200, json={"id": "789", "secret": TOKEN})
        if len(requests) == 2:
            assert request.method == "GET" and request.url.path == "/v1.0/789"
            return httpx.Response(200, json={"status": "FINISHED"})
        assert len(requests) == 3
        assert (
            request.method == "POST" and request.url.path == "/v1.0/123/threads_publish"
        )
        assert parse_qs(request.content.decode()) == {"creation_id": ["789"]}
        return httpx.Response(200, json={"id": "999", "text": TOKEN})

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    result = asyncio.run(
        server.call_tool(
            "threads_quote_post", {"account_id": "123", "post_id": "456", "text": TEXT}
        )
    )
    assert isinstance(result, CallToolResult)
    assert not result.is_error and result.structured_content == {"post_id": "999"}
    assert len(requests) == 3
    pause.assert_awaited_once_with(30)
    assert TOKEN not in result.model_dump_json() + caplog.text
    assert TEXT not in caplog.text


@pytest.mark.parametrize("action", ["repost", "delete"])
def test_immediate_action_uses_exact_http_method_target_and_public_result(
    connected: Connected, monkeypatch, caplog, action
):
    server, _, pause = connected
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert not request.url.query and not request.content
        if action == "repost":
            assert request.method == "POST" and request.url.path == "/v1.0/456/repost"
            return httpx.Response(200, json={"id": "789", "access_token": TOKEN})
        assert request.method == "DELETE" and request.url.path == "/v1.0/456"
        return httpx.Response(
            200, json={"success": True, "deleted_id": "456", "access_token": TOKEN}
        )

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    tool = "threads_repost" if action == "repost" else "threads_delete_post"
    result = asyncio.run(
        server.call_tool(tool, {"account_id": "123", "post_id": "456"})
    )
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    assert result.structured_content == (
        {"post_id": "789"}
        if action == "repost"
        else {"success": True, "deleted_id": "456"}
    )
    assert len(requests) == 1
    pause.assert_not_awaited()
    assert TOKEN not in result.model_dump_json() + caplog.text
    assert f"stage={action} reason=confirmed" in caplog.text


def test_post_action_tools_expose_write_and_destructive_annotations(
    connected: Connected,
):
    server, _, _ = connected
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in ("threads_quote_post", "threads_repost", "threads_delete_post"):
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.idempotent_hint is False
        assert tool.annotations.destructive_hint is (name == "threads_delete_post")
        assert set(tool.input_schema["required"]) == (
            {"account_id", "post_id", "text"}
            if name == "threads_quote_post"
            else {"account_id", "post_id"}
        )


@pytest.mark.parametrize(
    "method, tool",
    [
        ("quote_post", "threads_quote_post"),
        ("repost", "threads_repost"),
        ("delete_post", "threads_delete_post"),
    ],
)
@pytest.mark.parametrize(
    "post_id", ["", TOKEN, "https://www.threads.com/@example/post/abc", 123, True, None]
)
def test_invalid_target_is_rejected_before_credentials_for_service_and_mcp(
    connected: Connected, monkeypatch, method, tool, post_id
):
    server, service, _ = connected
    current = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    kwargs = {"text": TEXT} if method == "quote_post" else {}
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(getattr(service, method)("123", post_id, **kwargs))
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server.call_tool(tool, {"account_id": "123", "post_id": post_id, **kwargs})
        )
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    current.assert_not_awaited()


@pytest.mark.parametrize("text", ["", "  \n", "x" * 501, True, None])
def test_invalid_quote_text_never_accesses_credentials(
    connected: Connected, monkeypatch, text
):
    _, service, _ = connected
    current = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    with pytest.raises(ThreadsError):
        asyncio.run(service.quote_post("123", "456", text))
    current.assert_not_awaited()


@pytest.mark.parametrize("method", ["quote_post", "repost", "delete_post"])
@pytest.mark.parametrize("expired", [False, True])
def test_missing_or_expired_account_cannot_perform_post_actions(
    db_path, monkeypatch, method, expired
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
    request = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.client.http.publication_request", request)
    kwargs = {"text": TEXT} if method == "quote_post" else {}
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(getattr(ThreadsService(db_path), method)("123", "456", **kwargs))
    request.assert_not_awaited()


@pytest.mark.parametrize("method", ["quote_post", "repost", "delete_post"])
def test_post_actions_use_current_credentials_for_selected_account(
    connected: Connected, monkeypatch, method
):
    _, service, _ = connected
    account = Account(
        platform="threads",
        account_id="987",
        username="selected",
        access_token=SecretStr("synthetic-current-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    current = AsyncMock(return_value=account)
    monkeypatch.setattr(service.tokens, "get_account", current)
    target = {
        "quote_post": "create_quote_container",
        "repost": "repost_post",
        "delete_post": "delete_post",
    }[method]
    call = AsyncMock(side_effect=ThreadsError("stop before writing"))
    monkeypatch.setattr("social_mcp.threads.service." + target, call)
    kwargs = {"text": TEXT} if method == "quote_post" else {}
    with pytest.raises(ThreadsError, match="stop before"):
        asyncio.run(getattr(service, method)("987", "456", **kwargs))
    current.assert_awaited_once_with("987")
    call.assert_awaited_once_with(
        *(
            ("synthetic-current-token", "987", TEXT, "456")
            if method == "quote_post"
            else ("synthetic-current-token", "456")
        )
    )


@pytest.mark.parametrize("action", ["repost", "delete"])
@pytest.mark.parametrize(
    "failure",
    ["timeout", "network", "cancel", "json", "schema", 401, 403, 404, 429, 503],
)
def test_immediate_action_errors_are_private_and_never_retried(
    monkeypatch, caplog, action, failure
):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "network":
            raise httpx.ConnectError(TOKEN, request=request)
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "json":
            return httpx.Response(200, text=TOKEN)
        if failure == "schema":
            return httpx.Response(
                200, json={"id": TOKEN, "success": TOKEN, "deleted_id": TOKEN}
            )
        assert isinstance(failure, int)
        return httpx.Response(
            failure, json={"error": {"message": TOKEN, "error_user_msg": TEXT}}
        )

    mock_http(monkeypatch, respond)
    caplog.set_level(5)
    call = client.repost_post if action == "repost" else client.delete_post
    error = asyncio.CancelledError if failure == "cancel" else ThreadsError
    with pytest.raises(error) as caught:
        asyncio.run(call(TOKEN, "456"))
    assert len(requests) == 1
    if failure in ("timeout", "network", "json", "schema", 429, 503):
        assert "outcome is unknown" in str(caught.value)
        assert "retry" in str(caught.value)
    if failure == 403:
        assert (
            "threads_delete" if action == "delete" else "threads_content_publish"
        ) in str(caught.value)
    if failure != "cancel":
        assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert TOKEN not in caplog.text and TEXT not in caplog.text
    assert f"stage={action}" in caplog.text


@pytest.mark.parametrize("action", ["repost", "delete"])
@pytest.mark.parametrize("code", [10, 190, 4])
def test_http_success_with_graph_error_is_not_reported_as_action_success(
    monkeypatch, action, code
):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"error": {"code": code, "message": TOKEN}}
        ),
    )
    call = client.repost_post if action == "repost" else client.delete_post
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(call(TOKEN, "456"))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"success": True},
        {"deleted_id": "456"},
        {"success": False, "deleted_id": "456"},
        {"success": 1, "deleted_id": "456"},
        {"success": "true", "deleted_id": "456"},
        {"success": True, "deleted_id": 456},
        {"success": True, "deleted_id": "789"},
        {"success": True, "deleted_id": TOKEN},
    ],
)
def test_deletion_requires_true_success_and_exact_numeric_deleted_id(
    monkeypatch, payload
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={**payload, "secret": TOKEN})

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError, match="Deletion outcome is unknown") as caught:
        asyncio.run(client.delete_post(TOKEN, "456"))
    assert len(requests) == 1
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("stage", ["create", "status", "publish"])
@pytest.mark.parametrize("failure", ["timeout", "cancel", "json", "schema", 403, 503])
def test_failed_quote_stage_stops_without_retry_or_unconfirmed_success(
    connected: Connected, monkeypatch, stage, failure
):
    _, service, _ = connected
    events = []

    def respond(request):
        current = (
            "status"
            if request.method == "GET"
            else (
                "publish" if request.url.path.endswith("/threads_publish") else "create"
            )
        )
        events.append(current)
        if current != stage:
            return httpx.Response(
                200,
                json={"status": "FINISHED"} if current == "status" else {"id": "789"},
            )
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "json":
            return httpx.Response(200, text=TOKEN)
        if failure == "schema":
            return httpx.Response(200, json={"id": TOKEN, "status": TOKEN})
        assert isinstance(failure, int)
        return httpx.Response(failure, json={"error": {"message": TOKEN}})

    mock_http(monkeypatch, respond)
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else ThreadsError
    ) as caught:
        asyncio.run(service.quote_post("123", "456", TEXT))
    assert events[-1] == stage and events.count(stage) == 1
    assert events.count("publish") == (1 if stage == "publish" else 0)
    if stage == "publish" and failure not in ("cancel", 403):
        assert "unknown" in str(caught.value)
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "state", ["IN_PROGRESS", "ERROR", "EXPIRED", "PUBLISHED", "UNKNOWN"]
)
def test_quote_never_publishes_unready_or_already_published_container(
    connected: Connected, monkeypatch, state
):
    _, service, _ = connected
    create = AsyncMock(return_value="789")
    status = AsyncMock(return_value=ContainerStatus(status=state))
    publish = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.create_quote_container", create)
    monkeypatch.setattr("social_mcp.threads.publishing.fetch_container_status", status)
    monkeypatch.setattr("social_mcp.threads.publishing.publish_container", publish)
    with pytest.raises(ThreadsError):
        asyncio.run(service.quote_post("123", "456", TEXT))
    publish.assert_not_awaited()


def test_quote_and_repost_original_references_survive_safe_post_reads(monkeypatch):
    def respond(request):
        assert {"is_quote_post", "quoted_post", "reposted_post"} <= set(
            request.url.params["fields"].split(",")
        )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "789",
                        "is_quote_post": True,
                        "quoted_post": {"id": "456", "secret": TOKEN},
                    },
                    {
                        "id": "999",
                        "media_type": "REPOST_FACADE",
                        "reposted_post": {"id": "456", "secret": TOKEN},
                    },
                ]
            },
        )

    mock_http(monkeypatch, respond)
    result = asyncio.run(client.fetch_posts(TOKEN, "123", PostsQuery()))
    assert result.items[0].is_quote_post is True
    quote = result.items[0].quoted_post
    repost = result.items[1].reposted_post
    assert quote is not None
    assert quote.id == "456"
    assert repost is not None
    assert repost.id == "456"
    assert TOKEN not in result.model_dump_json()


@pytest.mark.parametrize("field", ["quoted_post", "reposted_post"])
def test_malformed_original_reference_is_rejected_without_leaking_payload(
    monkeypatch, field
):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": [{"id": "789", field: {"id": TOKEN}}]}
        ),
    )
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(client.fetch_posts(TOKEN, "123", PostsQuery()))
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
