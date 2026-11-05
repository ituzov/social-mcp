"""Run MCP over stdio and connect accounts from the terminal."""

import argparse
import asyncio
import getpass
import sys
import warnings
from importlib.metadata import version

from social_mcp.config import Settings
from social_mcp.database import initialize_database
from social_mcp.mcp_server import SocialMCPServer
from social_mcp.threads import (
    ThreadsConnection,
    ThreadsError,
    ThreadsService,
    register_threads_tools,
)
from social_mcp.updates import UpdateChecker


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="social-mcp")
    commands = parser.add_subparsers(dest="command")
    connect = commands.add_parser(
        "connect", help="Connect an account using an access token"
    )
    connect.add_argument("platform", choices=["threads"])
    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "connect":
        _connect_threads(settings)
        return
    initialize_database(settings.db_path)
    service = ThreadsService(settings.db_path)
    connection = ThreadsConnection(service)
    installed_version = version("social-mcp")
    server = SocialMCPServer(
        "social-mcp",
        version=installed_version,
        lifespan=connection.lifespan,
        update_checker=UpdateChecker(installed_version, enabled=settings.check_updates),
    )
    register_threads_tools(server, service, connection)
    server.run(transport="stdio")


def _connect_threads(settings: Settings) -> None:
    try:
        with warnings.catch_warnings(record=False):
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Threads access token (hidden input): ").strip()
        if not token:
            raise ThreadsError("The Threads access token must not be empty.")
        initialize_database(settings.db_path)
        profile = asyncio.run(ThreadsService(settings.db_path).connect(token))
    except getpass.GetPassWarning:
        print(
            "Run this command in a terminal to enter the token securely.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except EOFError, KeyboardInterrupt:
        print("Connection cancelled.", file=sys.stderr)
        raise SystemExit(1) from None
    except ThreadsError as error:
        print(error.user_message, file=sys.stderr)
        raise SystemExit(1) from None
    print(f"Connected Threads: @{profile.username} (ID: {profile.id}).")
    print(
        "Token saved in SQLite and expiry verified. Long-lived tokens are refreshed on use."
    )
