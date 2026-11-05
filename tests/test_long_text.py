import asyncio
import json
import traceback
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import ValidationError

from social_mcp.threads import client
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import LongTextPostInput, PostsQuery
from tests.support import PUBLISHING_TOKEN as TOKEN
from tests.support import Connected, mock_http

PREVIEW = "  Preview & <tag>\nSecond line  "
BODY = ("  Title & <tag>\n" + "Ж🙂\n" * 4000)[:9998] + "  "
LINK = "https://EXAMPLE.com:443/path%2Farticle?signature=synthetic-link-secret&a=1&a=2"


@pytest.mark.parametrize("link", [None, LINK])
@pytest.mark.parametrize("reply_to_id", [None, "456"])
@pytest.mark.parametrize("reply_settings", [False, True])
def test_long_post_and_reply_preserve_full_content_and_explicit_options(
    connected: Connected, monkeypatch, caplog, link, reply_to_id, reply_settings
):
    server, _, pause = connected
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url) and "synthetic-link-secret" not in str(
            request.url
        )
        if len(requests) == 1:
            assert request.method == "POST" and request.url.path == "/v1.0/123/threads"
            fields = parse_qs(request.content.decode())
            attachment = {"plaintext": BODY}
            if link is not None:
                attachment["link_attachment_url"] = link
            assert json.loads(fields.pop("text_attachment")[0]) == attachment
            expected = {
                "media_type": ["TEXT"],
                "text": [PREVIEW],
                "auto_publish_text": ["false"],
            }
            if reply_to_id is not None:
                expected["reply_to_id"] = [reply_to_id]
            if reply_settings:
                expected.update(
                    reply_control=["followers_only"], enable_reply_approvals=["true"]
                )
            assert fields == expected
            return httpx.Response(200, json={"id": "789", "secret": TOKEN})
        if len(requests) == 2:
            assert request.method == "GET" and request.url.path == "/v1.0/789"
            return httpx.Response(200, json={"status": "FINISHED"})
        assert len(requests) == 3
        assert request.url.path == "/v1.0/123/threads_publish"
        assert parse_qs(request.content.decode()) == {"creation_id": ["789"]}
        return httpx.Response(200, json={"id": "999", "plaintext": TOKEN})

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    result = asyncio.run(
        server.call_tool(
            "threads_publish_long_text",
            {
                "account_id": "123",
                "text": PREVIEW,
                "attachment_text": BODY,
                "link_url": link,
                "reply_to_id": reply_to_id,
                "reply_control": "followers_only" if reply_settings else None,
                "enable_reply_approvals": reply_settings,
            },
        )
    )
    assert isinstance(result, CallToolResult)
    assert not result.is_error and result.structured_content == {"post_id": "999"}
    pause.assert_awaited_once_with(30)
    for value in (TOKEN, PREVIEW, BODY, LINK, "synthetic-link-secret"):
        assert value not in caplog.text + result.model_dump_json()


def test_long_text_boundaries_are_exact_and_not_normalized():
    post = LongTextPostInput(text="я" * 500, attachment_text=BODY, link_url=LINK)
    assert len(post.attachment_text) == 10000
    assert post.attachment_text == BODY
    assert post.text == "я" * 500
    assert post.link_url == LINK
    assert BODY not in repr(post) and LINK not in repr(post)


@pytest.mark.parametrize(
    "changes",
    [
        {"text": ""},
        {"text": "x" * 501},
        {"attachment_text": ""},
        {"attachment_text": " \n\t"},
        {"attachment_text": "x" * 10001},
        {"attachment_text": None},
        {"attachment_text": True},
        {"link_url": TOKEN},
        {"link_url": "file:///private/article.txt"},
        {"link_url": "https://example.com/ spaced"},
        {"reply_to_id": TOKEN},
        {"reply_control": "invalid"},
        {"enable_reply_approvals": "true"},
    ],
)
def test_invalid_long_text_never_reads_credentials(
    connected: Connected, monkeypatch, changes
):
    _, service, _ = connected
    current = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    kwargs = {"text": PREVIEW, "attachment_text": BODY, **changes}
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(service.publish_long_text("123", **kwargs))
    current.assert_not_awaited()
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "changes, hint",
    [
        ({"attachment_text": TOKEN * 1000}, "attachment_text"),
        ({"attachment_text": None}, "attachment_text"),
        ({"link_url": True}, "link_url"),
        ({"enable_reply_approvals": "true"}, "enable_reply_approvals"),
        ({"reply_control": TOKEN}, "reply_control"),
    ],
)
def test_mcp_long_text_argument_errors_do_not_echo_values(
    connected: Connected, monkeypatch, changes, hint
):
    server, service, _ = connected
    current = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    with pytest.raises(ToolError, match=hint) as caught:
        asyncio.run(
            server.call_tool(
                "threads_publish_long_text",
                {
                    "account_id": "123",
                    "text": PREVIEW,
                    "attachment_text": BODY,
                    **changes,
                },
            )
        )
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    current.assert_not_awaited()


@pytest.mark.parametrize(
    "stage, reply_to_id",
    [("create", None), ("status", None), ("publish", None), ("publish", "456")],
)
@pytest.mark.parametrize(
    "failure", ["timeout", "network", "cancel", "json", "schema", 403, 429, 503]
)
def test_failed_long_text_stage_never_retries_or_reports_success(
    connected: Connected, monkeypatch, stage, reply_to_id, failure
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
            raise httpx.ReadTimeout(TOKEN + BODY, request=request)
        if failure == "network":
            raise httpx.ConnectError(TOKEN + BODY, request=request)
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "json":
            return httpx.Response(200, text=TOKEN + BODY)
        if failure == "schema":
            return httpx.Response(200, json={"id": TOKEN, "status": TOKEN})
        assert isinstance(failure, int)
        return httpx.Response(failure, json={"error": {"message": TOKEN + BODY}})

    mock_http(monkeypatch, respond)
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else ThreadsError
    ) as caught:
        asyncio.run(
            service.publish_long_text("123", PREVIEW, BODY, reply_to_id=reply_to_id)
        )
    assert events[-1] == stage and events.count(stage) == 1
    assert events.count("publish") == (1 if stage == "publish" else 0)
    if stage == "publish" and failure not in ("cancel", 403):
        assert "unknown" in str(caught.value)
        assert ("threads_list_replies" if reply_to_id else "threads_list_posts") in str(
            caught.value
        )
    assert TOKEN not in str(caught.value) and BODY not in str(caught.value)


@pytest.mark.parametrize(
    "settings",
    [
        {"reply_control": "everyone"},
        {"reply_control": "followers_only", "enable_reply_approvals": True},
        {"reply_control": "accounts_you_follow"},
        {"reply_control": "mentioned_only"},
        {"reply_control": "parent_post_author_only"},
        {"enable_reply_approvals": False},
    ],
)
def test_normal_text_posts_forward_only_explicit_non_default_reply_settings(
    connected: Connected, monkeypatch, settings
):
    _, service, _ = connected
    create = AsyncMock(side_effect=ThreadsError("stop before creation"))
    monkeypatch.setattr("social_mcp.threads.service.create_text_container", create)
    with pytest.raises(ThreadsError, match="stop before"):
        asyncio.run(service.publish_text("123", PREVIEW, **settings))
    expected = {
        "reply_to_id": None,
        **{k: v for k, v in settings.items() if v is not False},
    }
    create.assert_awaited_once_with(TOKEN, "123", PREVIEW, **expected)


def test_low_level_text_creation_encodes_reply_settings(monkeypatch):
    def respond(request):
        assert parse_qs(request.content.decode()) == {
            "media_type": ["TEXT"],
            "text": [PREVIEW],
            "auto_publish_text": ["false"],
            "reply_control": ["mentioned_only"],
            "enable_reply_approvals": ["true"],
        }
        return httpx.Response(200, json={"id": "789"})

    mock_http(monkeypatch, respond)
    assert (
        asyncio.run(
            client.create_text_container(
                TOKEN,
                "123",
                PREVIEW,
                reply_control="mentioned_only",
                enable_reply_approvals=True,
            )
        )
        == "789"
    )


def test_attachment_is_available_on_post_reads_without_extra_metadata(monkeypatch):
    def respond(request):
        assert "text_attachment" in request.url.params["fields"].split(",")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "789",
                        "text": PREVIEW,
                        "text_attachment": {
                            "plaintext": BODY,
                            "link_attachment_url": LINK,
                            "text_with_styling_info": [{"secret": TOKEN}],
                        },
                    }
                ]
            },
        )

    mock_http(monkeypatch, respond)
    result = asyncio.run(client.fetch_posts(TOKEN, "123", PostsQuery()))
    attachment = result.items[0].text_attachment
    assert attachment is not None
    assert attachment.plaintext == BODY and attachment.link_attachment_url == LINK
    assert TOKEN not in result.model_dump_json()


def test_long_text_tool_schema_has_separate_limits_and_write_annotations(
    connected: Connected,
):
    server, _, _ = connected
    tool = next(
        t
        for t in asyncio.run(server.list_tools())
        if t.name == "threads_publish_long_text"
    )
    assert tool.annotations is not None and tool.annotations.read_only_hint is False
    assert tool.annotations.idempotent_hint is False
    for field, limit in (("text", 500), ("attachment_text", 10000)):
        reference = tool.input_schema["properties"][field]["$ref"]
        definition = tool.input_schema["$defs"][reference.rsplit("/", 1)[-1]]
        assert definition["maxLength"] == limit


def test_input_model_rejects_oversized_body_instead_of_truncating():
    with pytest.raises(ValidationError):
        LongTextPostInput(text=PREVIEW, attachment_text="x" * 10001)
