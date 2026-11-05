import asyncio
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from social_mcp import app
from social_mcp.accounts import get_account
from social_mcp.threads import ThreadsConnection, ThreadsService, register_threads_tools
from social_mcp.threads.connection import manager
from social_mcp.threads.schemas import ThreadsProfile

pytestmark = pytest.mark.usefixtures("verified_token")

TOKEN = "synthetic-tool-form-token"
PROFILE = ThreadsProfile(id="123", username="example")


def setup_server(db_path):
    service = ThreadsService(db_path)
    connection = ThreadsConnection(service)
    server = MCPServer("test", lifespan=connection.lifespan)
    register_threads_tools(server, service, connection)
    return server, connection


async def call_connect(server, action="start"):
    result = await server.call_tool("threads_connect", {"action": action})
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    assert result.is_error is False
    return result.structured_content


def test_tool_returns_verified_profile_after_private_http_submission(
    db_path, monkeypatch, caplog, capsys
):
    monkeypatch.setattr(
        "social_mcp.threads.service.fetch_profile", AsyncMock(return_value=PROFILE)
    )
    caplog.set_level(5)

    async def check():
        server, connection = setup_server(db_path)
        async with connection.lifespan(server):
            opened = await call_connect(server)
            assert opened["status"] == "pending"
            assert opened["profile"] is None
            url = opened["url"]
            origin = urlsplit(url)
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    url,
                    data={"token": TOKEN},
                    headers={"Origin": f"{origin.scheme}://{origin.netloc}"},
                )
                assert response.status_code == 200
            result = await call_connect(server)
            assert result["status"] == "connected"
            assert result["url"] is None
            assert result["profile"]["id"] == "123"
            assert result["profile"]["username"] == "example"
            assert result["expires_at"] == 4_000_000_000
            assert TOKEN not in str(result)
            assert await call_connect(server) == result
            account = get_account(db_path, platform="threads", account_id="123")
            assert account is not None
            assert account.access_token.get_secret_value() == TOKEN
            profile = await server.call_tool(
                "threads_get_profile", {"account_id": "123"}
            )
            assert isinstance(profile, CallToolResult)
            assert TOKEN not in profile.model_dump_json()
            assert "example" in profile.model_dump_json()

    asyncio.run(check())
    assert TOKEN not in caplog.text
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_concurrent_tool_calls_share_one_page_and_shutdown_closes_it(db_path):
    async def check():
        server, connection = setup_server(db_path)
        async with connection.lifespan(server):
            results = await asyncio.gather(*(call_connect(server) for _ in range(4)))
            urls = {result["url"] for result in results}
            assert len(urls) == 1
            url = urls.pop()
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (await client.get(url)).status_code == 200
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(url)
        with pytest.raises(ToolError, match="shutting down"):
            await call_connect(server)

    asyncio.run(check())


def test_restart_and_cancel_close_old_pages(db_path):
    async def check():
        server, connection = setup_server(db_path)
        async with connection.lifespan(server):
            first = await call_connect(server)
            restarted = await call_connect(server, "restart")
            assert restarted["status"] == "pending"
            assert restarted["url"] != first["url"]
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get(first["url"])
                assert (await client.get(restarted["url"])).status_code == 200
                cancelled = await call_connect(server, "cancel")
                assert cancelled == {
                    "status": "closed",
                    "url": None,
                    "profile": None,
                    "expires_at": None,
                }
                with pytest.raises(httpx.ConnectError):
                    await client.get(restarted["url"])
            assert await call_connect(server, "cancel") == cancelled
            next_attempt = await call_connect(server)
            assert next_attempt["url"] != restarted["url"]

    asyncio.run(check())


def test_startup_failure_is_safe_and_next_attempt_can_succeed(db_path, monkeypatch):
    page_class = manager.ConnectionPage

    def fail(*_args, **_kwargs):
        raise OSError(TOKEN)

    async def check():
        server, connection = setup_server(db_path)
        async with connection.lifespan(server):
            monkeypatch.setattr(
                "social_mcp.threads.connection.manager.ConnectionPage", fail
            )
            with pytest.raises(ToolError, match="open") as caught:
                await call_connect(server)
            assert TOKEN not in str(caught.value)
            monkeypatch.setattr(
                "social_mcp.threads.connection.manager.ConnectionPage", page_class
            )
            assert (await call_connect(server))["status"] == "pending"

    asyncio.run(check())


def test_close_failure_does_not_expose_raw_exception(db_path, monkeypatch):
    close = manager.ConnectionPage.close

    async def fail(page):
        await close(page)
        raise OSError(TOKEN)

    async def check():
        server, connection = setup_server(db_path)
        async with connection.lifespan(server):
            await call_connect(server)
            monkeypatch.setattr(
                "social_mcp.threads.connection.manager.ConnectionPage.close", fail
            )
            with pytest.raises(ToolError, match="close") as caught:
                await call_connect(server, "cancel")
            assert TOKEN not in str(caught.value)
            monkeypatch.setattr(
                "social_mcp.threads.connection.manager.ConnectionPage.close", close
            )

    asyncio.run(check())


def test_application_lifespan_closes_pending_page(db_path, monkeypatch):
    monkeypatch.setenv("SOCIAL_MCP_DB_PATH", str(db_path))

    def run(server, *, transport):
        assert transport == "stdio"

        async def check():
            assert server.settings.lifespan is not None
            async with server.settings.lifespan(server):
                opened = await call_connect(server)
                url = opened["url"]
                async with httpx.AsyncClient(trust_env=False) as client:
                    assert (await client.get(url)).status_code == 200
            # Process and event loop are still alive: cleanup must close the port.
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get(url)

        asyncio.run(check())

    monkeypatch.setattr(MCPServer, "run", run)
    app.main([])


def test_failed_close_keeps_page_for_retry_and_blocks_restart(db_path, monkeypatch):
    close = manager.ConnectionPage.close
    attempts = []

    async def fail_before_closing(page):
        attempts.append(page)
        raise OSError(TOKEN)

    async def check():
        server, connection = setup_server(db_path)
        async with connection.lifespan(server):
            url = (await call_connect(server))["url"]
            monkeypatch.setattr(
                "social_mcp.threads.connection.manager.ConnectionPage.close",
                fail_before_closing,
            )
            async with httpx.AsyncClient(trust_env=False) as client:
                for action in ("cancel", "restart"):
                    with pytest.raises(ToolError, match="close") as caught:
                        await call_connect(server, action)
                    assert TOKEN not in str(caught.value)
                    assert (await client.get(url)).status_code == 200
                assert attempts[0] is attempts[1]
                monkeypatch.setattr(
                    "social_mcp.threads.connection.manager.ConnectionPage.close", close
                )
                assert (await call_connect(server, "cancel"))["status"] == "closed"
                with pytest.raises(httpx.ConnectError):
                    await client.get(url)
                assert (await call_connect(server))["url"] != url

    asyncio.run(check())


def test_list_tool_finds_accounts_after_new_service_instance(db_path):
    from social_mcp.accounts import save_account

    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=1,
        expires_at=999,
    )

    async def check():
        server, _ = setup_server(db_path)
        result = await server.call_tool("threads_list_accounts", {})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        text = result.model_dump_json()
        assert "example" in text and "123" in text and "999" in text
        assert TOKEN not in text
        assert "access_token" not in text

    asyncio.run(check())
