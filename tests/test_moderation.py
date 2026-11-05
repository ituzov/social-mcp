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
from social_mcp.threads.errors import ThreadsError, ThreadsTransientError
from social_mcp.threads.schemas import (
    PendingRepliesQuery,
    PostsQuery,
    RepliesPage,
    ReplyModerationAction,
)
from tests.support import PUBLISHING_TOKEN as TOKEN
from tests.support import Connected, mock_http


@pytest.mark.parametrize(
    "operation, edge, fields",
    [
        ("hide", "manage_reply", {"hide": ["true"]}),
        ("unhide", "manage_reply", {"hide": ["false"]}),
        ("approve", "manage_pending_reply", {"approve": ["true"]}),
        ("ignore", "manage_pending_reply", {"approve": ["false"]}),
    ],
)
def test_all_moderation_operations_use_exact_endpoint_and_boolean(
    connected: Connected, monkeypatch, caplog, operation, edge, fields
):
    server, _, pause = connected
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "POST" and request.url.path == f"/v1.0/456/{edge}"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert not request.url.query
        assert parse_qs(request.content.decode()) == fields
        return httpx.Response(200, json={"success": True, "secret": TOKEN})

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    result = asyncio.run(
        server.call_tool(
            "threads_moderate_reply",
            {"account_id": "123", "reply_id": "456", "operation": operation},
        )
    )
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    assert result.structured_content == {
        "success": True,
        "reply_id": "456",
        "operation": operation,
    }
    assert len(requests) == 1
    pause.assert_not_awaited()
    assert TOKEN not in result.model_dump_json() + caplog.text
    assert "stage=moderate reason=confirmed" in caplog.text


@pytest.mark.parametrize("operation", ["hide", "unhide", "approve", "ignore"])
@pytest.mark.parametrize(
    "failure", ["timeout", "network", "cancel", "json", "schema", 401, 403, 429, 503]
)
def test_moderation_failure_is_private_and_never_retried(
    monkeypatch, caplog, operation: ReplyModerationAction, failure
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
            return httpx.Response(200, json={"success": TOKEN})
        assert isinstance(failure, int)
        return httpx.Response(failure, json={"error": {"message": TOKEN}})

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else ThreadsError
    ) as caught:
        asyncio.run(client.moderate_reply(TOKEN, "456", operation))
    assert len(requests) == 1
    if failure in ("timeout", "network", "json", "schema", 429, 503):
        assert "unknown" in str(caught.value) and "retry" in str(caught.value)
    if failure == 403:
        assert "threads_manage_replies" in str(caught.value)
    assert TOKEN not in "".join(traceback.format_exception(caught.value)) + caplog.text


@pytest.mark.parametrize(
    "payload",
    [{}, {"success": False}, {"success": 1}, {"success": "true"}, {"success": None}],
)
def test_moderation_requires_true_strict_acknowledgement(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ThreadsError, match="unknown"):
        asyncio.run(client.moderate_reply(TOKEN, "456", "hide"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reply_id": TOKEN, "operation": "hide"},
        {"reply_id": "456", "operation": TOKEN},
        {"reply_id": "456", "operation": True},
        {"reply_id": True, "operation": "approve"},
    ],
)
def test_invalid_moderation_never_reads_credentials_or_echoes_values(
    connected: Connected, monkeypatch, kwargs
):
    server, service, _ = connected
    current = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(service.moderate_reply("123", **kwargs))
    assert TOKEN not in str(caught.value)
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server.call_tool("threads_moderate_reply", {"account_id": "123", **kwargs})
        )
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    current.assert_not_awaited()


@pytest.mark.parametrize("status", [None, "pending", "ignored"])
def test_pending_reply_pages_preserve_state_filter_and_safe_cursor(
    connected: Connected, monkeypatch, caplog, status
):
    server, _, _ = connected
    requests = []

    def respond(request):
        requests.append(request)
        assert (
            request.method == "GET" and request.url.path == "/v1.0/789/pending_replies"
        )
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert "reply_approval_status" in request.url.params["fields"].split(",")
        assert "text_attachment" not in request.url.params["fields"].split(",")
        assert (
            request.url.params["limit"] == "1"
            and request.url.params["reverse"] == "true"
        )
        if status is None:
            assert "approval_status" not in request.url.params
        else:
            assert request.url.params["approval_status"] == status
        if "after" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "456",
                            "reply_approval_status": status or "pending",
                            "secret": TOKEN,
                        }
                    ],
                    "paging": {
                        "cursors": {"after": "next"},
                        "next": f"https://graph.threads.com/next?access_token={TOKEN}",
                    },
                },
            )
        assert request.url.params["after"] == "next"
        return httpx.Response(200, json={"data": []})

    caplog.set_level(5)
    mock_http(monkeypatch, respond)

    async def check():
        args = {
            "account_id": "123",
            "post_id": "789",
            "approval_status": status,
            "limit": 1,
        }
        first = await server.call_tool("threads_list_pending_replies", args)
        assert isinstance(first, CallToolResult)
        assert first.structured_content is not None
        assert first.structured_content["items"][0]["reply_approval_status"] == (
            status or "pending"
        )
        assert first.structured_content["next_cursor"] == "next"
        second = await server.call_tool(
            "threads_list_pending_replies", {**args, "after": "next"}
        )
        assert isinstance(second, CallToolResult)
        assert second.structured_content == {"items": [], "next_cursor": None}
        assert TOKEN not in first.model_dump_json() + second.model_dump_json()
        assert "graph.threads.com" not in first.model_dump_json()

    asyncio.run(check())
    assert len(requests) == 2 and TOKEN not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {
            "data": [],
            "paging": {
                "cursors": {"after": "next"},
                "next": "https://graph.threads.com/next",
            },
        },
        {"data": [{"id": "456", "reply_approval_status": "future_state"}]},
    ],
)
def test_empty_pending_pages_and_future_response_states_are_valid(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    page = asyncio.run(
        client.fetch_pending_replies(TOKEN, "789", PendingRepliesQuery())
    )
    assert page.next_cursor == ("next" if "paging" in payload else None)
    if page.items:
        assert page.items[0].reply_approval_status == "future_state"


@pytest.mark.parametrize(
    "paging",
    [
        {"next": "https://graph.threads.com/private"},
        {"cursors": {"after": "same"}, "next": "https://graph.threads.com/private"},
    ],
)
def test_pending_cursor_must_exist_and_advance(monkeypatch, paging):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": [], "paging": paging}),
    )
    with pytest.raises(ThreadsError, match="pagination"):
        asyncio.run(
            client.fetch_pending_replies(
                TOKEN, "789", PendingRepliesQuery(after="same")
            )
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": [None]},
        {"data": [{"id": 123}]},
        {"data": [{"id": "456", "reply_approval_status": True}]},
    ],
)
def test_malformed_pending_response_is_private(monkeypatch, payload):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, json={**payload, "secret": TOKEN}),
    )
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(client.fetch_pending_replies(TOKEN, "789", PendingRepliesQuery()))
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"approval_status": "approved"},
        {"limit": True},
        {"after": ""},
        {"post_id": TOKEN},
    ],
)
def test_bad_pending_query_is_rejected_before_credentials(
    connected: Connected, monkeypatch, kwargs
):
    _, service, _ = connected
    current = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    with pytest.raises(ThreadsError):
        asyncio.run(service.list_pending_replies("123", **{"post_id": "789", **kwargs}))
    current.assert_not_awaited()


def test_regular_reply_reads_do_not_request_pending_only_fields(monkeypatch):
    def respond(request):
        fields = request.url.params["fields"].split(",")
        assert "reply_approval_status" not in fields and "text_attachment" not in fields
        return httpx.Response(200, json={"data": []})

    mock_http(monkeypatch, respond)
    assert asyncio.run(client.fetch_replies(TOKEN, "789", PostsQuery())).items == []


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("publish_long_text", {"text": "Preview", "attachment_text": "Body"}),
        ("moderate_reply", {"reply_id": "456", "operation": "hide"}),
        ("list_pending_replies", {"post_id": "789"}),
    ],
)
@pytest.mark.parametrize("expired", [False, True])
def test_missing_or_expired_account_blocks_new_operations(
    db_path, monkeypatch, method, kwargs, expired
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
    public = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.client.http.publication_request", request)
    monkeypatch.setattr("social_mcp.threads.client.http.read_request", public)
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(getattr(ThreadsService(db_path), method)("123", **kwargs))
    request.assert_not_awaited()
    public.assert_not_awaited()


def test_pending_reads_use_selected_accounts_current_token(
    connected: Connected, monkeypatch
):
    _, service, _ = connected
    account = Account(
        platform="threads",
        account_id="987",
        username="selected",
        access_token=SecretStr("synthetic-new-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    current = AsyncMock(return_value=account)
    fetch = AsyncMock(return_value=RepliesPage(items=[]))
    monkeypatch.setattr(service.tokens, "get_account", current)
    monkeypatch.setattr("social_mcp.threads.service.fetch_pending_replies", fetch)
    assert (
        asyncio.run(
            service.list_pending_replies("987", "789", approval_status="ignored")
        )
        is fetch.return_value
    )
    current.assert_awaited_once_with("987")
    fetch.assert_awaited_once_with(
        "synthetic-new-token", "789", PendingRepliesQuery(approval_status="ignored")
    )


def test_moderation_and_pending_annotations_distinguish_read_from_write(
    connected: Connected,
):
    server, _, _ = connected
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    moderation = tools["threads_moderate_reply"].annotations
    pending = tools["threads_list_pending_replies"].annotations
    assert moderation is not None and moderation.read_only_hint is False
    assert moderation.destructive_hint is True and moderation.idempotent_hint is False
    assert pending is not None and pending.read_only_hint is True


@pytest.mark.parametrize("status", [200, 400, 500])
@pytest.mark.parametrize(
    "detail",
    [
        "Reply approvals must be enabled to retrieve pending replies.",
        "  REPLY APPROVALS\nMUST BE ENABLED to retrieve pending replies.  ",
    ],
)
def test_disabled_reply_approvals_are_a_post_precondition_not_a_transient_outage(
    monkeypatch, caplog, status, detail
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": 100,
                    "message": detail + TOKEN,
                    "error_user_msg": TOKEN,
                }
            },
        )

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError, match="Reply approvals are not enabled") as caught:
        asyncio.run(client.fetch_pending_replies(TOKEN, "789", PendingRepliesQuery()))
    assert not isinstance(caught.value, ThreadsTransientError)
    assert "enable_reply_approvals=true" in str(caught.value)
    assert "Try again later" not in str(caught.value)
    assert len(requests) == 1
    assert TOKEN not in "".join(traceback.format_exception(caught.value)) + caplog.text


@pytest.mark.parametrize(
    "code, detail",
    [
        (100, "A different API error " + TOKEN),
        (1, "Reply approvals must be enabled " + TOKEN),
        (100, {"private": TOKEN}),
        (100, None),
    ],
)
def test_other_server_errors_are_not_misclassified_as_disabled_approvals(
    monkeypatch, code, detail
):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            500, json={"error": {"code": code, "message": detail}}
        ),
    )
    with pytest.raises(ThreadsTransientError) as caught:
        asyncio.run(client.fetch_pending_replies(TOKEN, "789", PendingRepliesQuery()))
    assert "enable_reply_approvals" not in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_mcp_reports_disabled_approvals_without_claiming_the_queue_is_empty(
    connected: Connected, monkeypatch
):
    server, _, _ = connected
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            500,
            json={
                "error": {
                    "code": 100,
                    "message": "Reply approvals must be enabled. " + TOKEN,
                }
            },
        ),
    )
    with pytest.raises(ToolError, match="Reply approvals are not enabled") as caught:
        asyncio.run(
            server.call_tool(
                "threads_list_pending_replies", {"account_id": "123", "post_id": "789"}
            )
        )
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
