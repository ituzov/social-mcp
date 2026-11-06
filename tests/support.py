"""Shared synthetic data and HTTP mocking helpers for tests."""

from unittest.mock import AsyncMock

import httpx

from social_mcp.mcp_server import SocialMCPServer
from social_mcp.threads import ThreadsService

PUBLISHING_TOKEN = "synthetic-publishing-secret"
INSIGHTS_TOKEN = "synthetic-insights-token"
TEXT = "  Привет & hello\nhttps://example.com/?a=1&b=2 😀  "
ASYNC_CLIENT = httpx.AsyncClient

type Connected = tuple[SocialMCPServer, ThreadsService, AsyncMock]


def mock_http(monkeypatch, respond):
    """Replace HTTP with an in-process handler; never contact the real API."""

    def client(**kwargs):
        transport = kwargs.pop("transport", None)
        if transport is not None:
            # Preserve query redaction while replacing its actual network transport.
            from social_mcp.threads.transport import RedactingTransport

            assert isinstance(transport, RedactingTransport)
            transport._transport = httpx.MockTransport(respond)
        else:
            transport = httpx.MockTransport(respond)
        return ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", client)
