import asyncio
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from social_mcp.mcp_server import SocialMCPServer
from social_mcp.threads import (
    ThreadsConnection,
    ThreadsService,
    client,
    register_threads_tools,
)
from social_mcp.threads.errors import ThreadsError, ThreadsTransientError
from social_mcp.threads.schemas import PostsQuery
from tests.support import INSIGHTS_TOKEN as TOKEN
from tests.support import mock_http


@pytest.mark.parametrize(
    "name, arguments, hint",
    [
        ("threads_list_posts", {"account_id": "123", "limit": 0}, "from 1 to 100"),
        ("threads_list_posts", {"account_id": "123", "limit": True}, "integer"),
        ("threads_list_posts", {"account_id": "123", "limit": TOKEN}, "integer"),
        ("threads_list_posts", {"account_id": "123", "after": ""}, "next_cursor"),
        ("threads_get_post_insights", {"account_id": "123", "post_id": ""}, "post_id"),
        (
            "threads_get_post_insights",
            {"account_id": "123", "post_id": TOKEN},
            "post_id",
        ),
        ("threads_get_post_insights", {"account_id": "123"}, "post_id"),
        ("threads_get_account_insights", {"account_id": TOKEN}, "account_id"),
        (
            "threads_get_account_insights",
            {"account_id": "123", "since": TOKEN},
            "Invalid tool arguments",
        ),
        (
            "threads_get_account_insights",
            {"account_id": "123", "until": True},
            "Invalid tool arguments",
        ),
        (
            "threads_get_account_insights",
            {"account_id": "123", "since": 1712991599},
            "Invalid tool arguments",
        ),
        (
            "threads_get_account_insights",
            {"account_id": "123", "since": 1712991600},
            "both since and until",
        ),
        ("threads_get_profile", {"account_id": TOKEN}, "account_id"),
        ("threads_publish_image", {"account_id": "123", "image_url": ""}, "image_url"),
        (
            "threads_publish_image",
            {
                "account_id": "123",
                "image_url": "https://example.com/photo.jpg",
                "alt_text": True,
            },
            "alt_text",
        ),
        (
            "threads_publish_carousel",
            {"account_id": "123", "images": [{"image_url": TOKEN}]},
            "images",
        ),
        ("threads_get_profile", {"account_id": 123}, "account_id"),
        ("threads_connect", {"action": TOKEN}, "start, restart, or cancel"),
    ],
)
def test_invalid_arguments_are_short_and_never_reach_credentials(
    db_path, monkeypatch, name, arguments, hint
):
    server = SocialMCPServer("test")
    service = ThreadsService(db_path)
    get_account = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", get_account)
    register_threads_tools(server, service, ThreadsConnection(service))
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool(name, arguments))
    message = str(caught.value)
    assert hint in message
    assert TOKEN not in message
    assert "pydantic" not in message.lower()
    assert "input_value" not in message
    get_account.assert_not_awaited()


def test_unknown_validation_field_does_not_echo_input_or_location():
    server = SocialMCPServer("test")

    @server.tool()
    async def example(unfamiliar: int) -> int:
        return unfamiliar

    with pytest.raises(ToolError, match="Invalid tool arguments") as caught:
        asyncio.run(server.call_tool("example", {"unfamiliar": TOKEN}))
    assert TOKEN not in str(caught.value)


def test_output_validation_crash_is_not_reported_as_bad_arguments():
    server = SocialMCPServer("test")

    @server.tool()
    async def broken() -> int:
        return TOKEN

    with pytest.raises(UnexpectedToolError) as caught:
        asyncio.run(server.call_tool("broken", {}))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "status, code, expected, transient",
    [
        (400, 100, "not found or is unavailable", False),
        (404, None, "not found or is unavailable", False),
        (403, 10, "threads_manage_insights", False),
        (400, 190, "rejected the token", False),
        (429, 4, "rate limit", True),
        (503, 2, "Temporary", True),
        (400, None, "Check the request parameters", False),
    ],
)
def test_insights_error_context_preserves_auth_limits_and_server_failures(
    monkeypatch, status, code, expected, transient
):
    import httpx

    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            status, json={"error": {"code": code, "message": TOKEN}}
        ),
    )
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(client.fetch_post_insights(TOKEN, "123"))
    assert expected in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert isinstance(caught.value, ThreadsTransientError) is transient
    if not transient:
        assert "Try again later" not in str(caught.value)


@pytest.mark.parametrize("status, code", [(400, 100), (500, 1)])
def test_cursor_error_has_recovery_hint_without_claiming_every_500_is_invalid_cursor(
    monkeypatch, status, code
):
    import httpx

    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            status, json={"error": {"code": code, "message": TOKEN}}
        ),
    )
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(client.fetch_posts(TOKEN, "123", PostsQuery(after="old-cursor")))
    message = str(caught.value)
    assert "without after" in message and "next_cursor" in message
    assert TOKEN not in message
    assert isinstance(caught.value, ThreadsTransientError) is (status == 500)
    if status == 500:
        assert "Temporary" in message


def test_argument_hints_are_scoped_to_tool_and_server():
    first = SocialMCPServer("first")
    second = SocialMCPServer("second")
    hints = {
        "example": {"text": "First platform's text hint."},
        "other": {"text": "Another platform's text hint."},
    }
    first.register_argument_hints(hints)
    hints["example"]["text"] = TOKEN
    for server in (first, second):

        @server.tool()
        async def example(text: int) -> int:
            return text

        @server.tool()
        async def other(text: int) -> int:
            return text

    for server, name, expected in (
        (first, "example", "First platform's text hint."),
        (first, "other", "Another platform's text hint."),
        (second, "example", "Invalid tool arguments"),
    ):
        with pytest.raises(ToolError, match=expected) as caught:
            asyncio.run(server.call_tool(name, {"text": TOKEN}))
        assert TOKEN not in str(caught.value)
        assert "threads" not in str(caught.value).lower()


@pytest.mark.parametrize("name", ["threads_publish_image", "threads_publish_carousel"])
def test_caption_validation_does_not_request_nonblank_text(connected, name):
    server, _, _ = connected
    arguments = {"account_id": "123", "text": "x" * 501}
    if name == "threads_publish_image":
        arguments["image_url"] = "https://example.com/image.jpg"
    else:
        arguments["images"] = [{"image_url": "https://example.com/image.jpg"}] * 2
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool(name, arguments))
    assert "omit it" in str(caught.value)
    assert "non-blank" not in str(caught.value)


@pytest.mark.parametrize("name", ["threads_quote_post", "threads_repost"])
def test_repost_and_quote_hint_accepts_accessible_original(connected, name):
    server, _, _ = connected
    with pytest.raises(ToolError) as caught:
        asyncio.run(
            server.call_tool(
                name, {"account_id": "123", "post_id": TOKEN, "text": "quote"}
            )
        )
    assert "accessible original" in str(caught.value)
    assert TOKEN not in str(caught.value)
