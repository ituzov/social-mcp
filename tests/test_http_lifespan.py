"""Connection pooling must retain request isolation, redaction and cleanup."""

import asyncio

import httpx
import pytest

from social_mcp.threads.client import client_lifespan
from social_mcp.threads.client.http import (
    _client,
    publication_request,
    read_request,
    token_request,
)
from social_mcp.threads.transport import RedactingTransport

ASYNC_CLIENT = httpx.AsyncClient


@pytest.mark.parametrize("cancelled", [False, True])
def test_pool_is_shared_by_child_tasks_and_closed_on_exit(
    monkeypatch, cancelled, caplog
):
    created = []
    requests = []

    def respond(request):
        requests.append(request)
        assert "cookie" not in request.headers
        if request.url.path == "/refresh_access_token":
            assert "Authorization" not in request.headers
            assert request.url.params["access_token"] == "synthetic-secret-token"
        return httpx.Response(
            200, json={"ok": True}, headers={"Set-Cookie": "account=private; Path=/"}
        )

    def client(**kwargs):
        transport = httpx.MockTransport(respond)
        if isinstance(kwargs.get("transport"), RedactingTransport):
            transport = RedactingTransport(transport)
        kwargs["transport"] = transport
        result = ASYNC_CLIENT(**kwargs)
        created.append(result)
        return result

    monkeypatch.setattr("httpx.AsyncClient", client)
    caplog.set_level(5)

    async def run():
        try:
            async with client_lifespan():
                await read_request("me", "account-one", {})
                await asyncio.create_task(read_request("me", "account-two", {}))
                await publication_request(
                    "123/threads",
                    "account-three",
                    {"media_type": "TEXT"},
                    method="POST",
                    stage="create",
                )
                await token_request(
                    "refresh_access_token", {"access_token": "synthetic-secret-token"}
                )
                await token_request(
                    "refresh_access_token", {"access_token": "synthetic-secret-token"}
                )
                assert len(created) == 2 and all(
                    not client.is_closed for client in created
                )
                if cancelled:
                    raise asyncio.CancelledError()
        except asyncio.CancelledError:
            assert cancelled
        assert all(client.is_closed for client in created)
        async with _client() as standalone:
            assert standalone is not created[0] and standalone is not created[1]
        assert standalone.is_closed

    asyncio.run(run())
    assert [r.headers.get("Authorization") for r in requests[:3]] == [
        "Bearer account-one",
        "Bearer account-two",
        "Bearer account-three",
    ]
    assert "synthetic-secret-token" not in caplog.text
    assert all("synthetic-secret-token" not in str(r.url) for r in requests)


def test_scopes_do_not_share_clients_across_event_loops(monkeypatch):
    created = []

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        )
        result = ASYNC_CLIENT(**kwargs)
        created.append(result)
        return result

    monkeypatch.setattr("httpx.AsyncClient", client)

    async def run():
        async with client_lifespan():
            await read_request("me", "synthetic-token", {})

    asyncio.run(run())
    asyncio.run(run())
    assert len(created) == 4
    assert all(client.is_closed for client in created)


def test_real_http11_requests_reuse_one_connection():
    async def run():
        connections = []
        finished = asyncio.Event()

        async def serve(reader, writer):
            connections.append(writer)
            try:
                while True:
                    try:
                        await reader.readuntil(b"\r\n\r\n")
                    except asyncio.IncompleteReadError:
                        break
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with client_lifespan():
                for _ in range(3):
                    async with _client() as client:
                        response = await client.send(
                            httpx.Request("GET", f"http://127.0.0.1:{port}/")
                        )
                        assert response.json() == {}
            await asyncio.wait_for(finished.wait(), 2)
        assert len(connections) == 1

    asyncio.run(run())
