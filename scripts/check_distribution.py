"""Check an installed wheel, bundled resources, and MCP over stdio without Meta."""

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from social_mcp.threads.connection.views import form_page


async def check_package() -> None:
    for language in ("en", "ru"):
        page = form_page("/connect/package-check", language=language)
        assert page.status_code == 200
        assert page.headers["content-language"] == language
        assert f'<html lang="{language}">' in page.body.decode()
    with TemporaryDirectory(prefix="social-mcp-package-") as directory:
        server = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from social_mcp.app import main; main()"],
            cwd=directory,
            env={
                **os.environ,
                "SOCIAL_MCP_DB_PATH": str(Path(directory) / "accounts.sqlite3"),
            },
        )
        async with asyncio.timeout(30):
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert {
                        "threads_connect",
                        "threads_list_accounts",
                        "threads_get_profile",
                    } <= names
                    accounts = await session.call_tool("threads_list_accounts", {})
                    assert (
                        isinstance(accounts, CallToolResult) and not accounts.is_error
                    )
                    connection = await session.call_tool(
                        "threads_connect", {"action": "start"}
                    )
                    assert (
                        isinstance(connection, CallToolResult)
                        and not connection.is_error
                    )
                    assert connection.structured_content is not None
                    url = connection.structured_content["url"]
                    assert isinstance(url, str)
                    async with httpx.AsyncClient(trust_env=False) as client:
                        page = await client.get(url, headers={"Accept-Language": "ru"})
                        assert page.status_code == 200
                        assert page.headers["content-language"] == "ru"
                        assert "Вставь токен" in page.text
                    closed = await session.call_tool(
                        "threads_connect", {"action": "cancel"}
                    )
                    assert isinstance(closed, CallToolResult) and not closed.is_error
    print(
        "Installed package: migrations, templates, EN/RU assets, and MCP stdio verified."
    )


if __name__ == "__main__":
    asyncio.run(check_package())
