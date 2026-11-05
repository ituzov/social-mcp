import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult


def test_startup_and_tool_discovery(tmp_path):
    async def check():
        path = tmp_path / "accounts.sqlite3"
        project = Path(__file__).resolve().parents[1]
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from social_mcp.app import main; main()"],
            cwd=str(project),
            env={
                "SOCIAL_MCP_DB_PATH": str(path),
                "PYTHONPATH": str(project / "src"),
            },
        )
        async with asyncio.timeout(20):
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    result = await session.initialize()
                    assert result.server_info.name == "social-mcp"
                    tools = (await session.list_tools()).tools
                    by_name = {tool.name: tool for tool in tools}
                    assert set(by_name) == {
                        "threads_connect",
                        "threads_get_profile",
                        "threads_list_accounts",
                        "threads_list_posts",
                        "threads_get_post_insights",
                        "threads_get_account_insights",
                        "threads_list_replies",
                        "threads_publish_text",
                        "threads_publish_image",
                        "threads_publish_carousel",
                        "threads_quote_post",
                        "threads_repost",
                        "threads_delete_post",
                        "threads_moderate_reply",
                        "threads_list_pending_replies",
                        "threads_publish_long_text",
                        "threads_reply",
                        "threads_get_conversation",
                    }
                    profile = by_name["threads_get_profile"]
                    assert profile.annotations is not None
                    assert profile.annotations.read_only_hint is True
                    insights = by_name["threads_get_post_insights"]
                    assert insights.annotations is not None
                    conversation = by_name["threads_get_conversation"]
                    assert conversation.annotations is not None
                    assert conversation.annotations.read_only_hint is True
                    assert set(conversation.input_schema["required"]) == {
                        "account_id",
                        "post_id",
                    }
                    reply = by_name["threads_reply"]
                    assert reply.annotations is not None
                    assert reply.annotations.read_only_hint is False
                    assert reply.annotations.idempotent_hint is False
                    assert set(reply.input_schema["required"]) == {
                        "account_id",
                        "reply_to_id",
                        "text",
                    }
                    publishing = by_name["threads_publish_text"]
                    assert publishing.annotations is not None
                    assert publishing.annotations.read_only_hint is False
                    assert publishing.annotations.idempotent_hint is False
                    assert set(publishing.input_schema["required"]) == {
                        "account_id",
                        "text",
                    }
                    replies = by_name["threads_list_replies"]
                    assert replies.annotations is not None
                    assert replies.annotations.read_only_hint is True
                    assert set(replies.input_schema["required"]) == {
                        "account_id",
                        "post_id",
                    }
                    assert insights.annotations.read_only_hint is True
                    assert set(insights.input_schema["required"]) == {
                        "account_id",
                        "post_id",
                    }
                    connect = by_name["threads_connect"]
                    assert connect.annotations is not None
                    assert connect.annotations.read_only_hint is False
                    schema = by_name["threads_connect"].input_schema
                    assert set(schema["properties"]) == {"action"}
                    assert set(schema["properties"]["action"]["enum"]) == {
                        "start",
                        "restart",
                        "cancel",
                    }
                    response = await session.call_tool(
                        "threads_get_profile", {"account_id": "999"}
                    )
                    assert isinstance(response, CallToolResult)
                    assert response.is_error is True
                    assert "is connected" in response.model_dump_json()
                    rejected = await session.call_tool(
                        "threads_list_posts",
                        {"account_id": "123", "limit": "synthetic-private-argument"},
                    )
                    assert isinstance(rejected, CallToolResult)
                    assert rejected.is_error is True
                    message = rejected.model_dump_json()
                    assert "from 1 to 100" in message
                    assert "synthetic-private-argument" not in message
                    assert "pydantic" not in message.lower()
                    opened = await session.call_tool("threads_connect", {})
                    assert isinstance(opened, CallToolResult)
                    assert opened.structured_content is not None
                    assert opened.is_error is False
                    assert opened.structured_content["status"] == "pending"
                    url = opened.structured_content["url"]
                    repeated = await session.call_tool("threads_connect", {})
                    assert isinstance(repeated, CallToolResult)
                    assert repeated.structured_content is not None
                    assert repeated.structured_content["url"] == url
                    async with httpx.AsyncClient(trust_env=False) as client:
                        assert (await client.get(url)).status_code == 200
                        cancelled = await session.call_tool(
                            "threads_connect", {"action": "cancel"}
                        )
                        assert isinstance(cancelled, CallToolResult)
                        assert cancelled.structured_content is not None
                        assert cancelled.structured_content["status"] == "closed"
                        with pytest.raises(httpx.ConnectError):
                            await client.get(url)
                    pending = await session.call_tool("threads_connect", {})
                    assert isinstance(pending, CallToolResult)
                    assert pending.structured_content is not None
                    shutdown_url = pending.structured_content["url"]
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get(shutdown_url)
        assert path.is_file()

    asyncio.run(check())
