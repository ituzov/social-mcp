import asyncio
import traceback

import httpx
import pytest

from social_mcp.threads.client import inspect_token, refresh_token
from social_mcp.threads.errors import ThreadsError

TOKEN = "synthetic-private-query-token"
NEW_TOKEN = "synthetic-refreshed-token"
INFO = dict(
    user_id="123",
    is_valid=True,
    issued_at=100,
    expires_at=5_184_100,
    scopes=["threads_basic"],
)


def mock_transport(monkeypatch, handler):
    monkeypatch.setattr(
        "httpx.AsyncHTTPTransport", lambda: httpx.MockTransport(handler)
    )


def test_debug_and_refresh_do_not_log_query_secrets(monkeypatch, caplog):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.params["access_token"] == TOKEN
        if request.url.path == "/v1.0/debug_token":
            assert request.url.params["input_token"] == TOKEN
            return httpx.Response(200, json={"data": INFO})
        assert request.url.path == "/refresh_access_token"
        assert request.url.params["grant_type"] == "th_refresh_token"
        return httpx.Response(
            200, json={"access_token": NEW_TOKEN, "expires_in": 5_184_000}
        )

    mock_transport(monkeypatch, respond)
    caplog.set_level(5)
    assert asyncio.run(inspect_token(TOKEN)).expires_at == INFO["expires_at"]
    refreshed = asyncio.run(refresh_token(TOKEN))
    assert refreshed.access_token.get_secret_value() == NEW_TOKEN
    assert NEW_TOKEN not in repr(refreshed)
    assert TOKEN not in caplog.text
    assert NEW_TOKEN not in caplog.text
    assert TOKEN not in str(requests[0].url)
    assert TOKEN not in str(requests[1].url)
    assert "HTTP Request" in caplog.text


@pytest.mark.parametrize("operation", [inspect_token, refresh_token])
@pytest.mark.parametrize("failure", [401, 429, 503, 302, "timeout", "network", "json"])
def test_token_errors_never_leak_or_repeat(monkeypatch, operation, failure, caplog):
    requests = []

    def respond(request):
        requests.append(request)
        if failure in ("timeout", "network"):
            kind = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            raise kind(TOKEN, request=request)
        if failure == "json":
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(
            failure,
            json={"error": {"message": TOKEN}},
            headers={"Location": "https://example.com"},
        )

    mock_transport(monkeypatch, respond)
    caplog.set_level(5)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(operation(TOKEN))
    assert len(requests) == 1
    assert TOKEN not in str(requests[0].url)
    assert TOKEN not in caplog.text
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "changes",
    [{"expires_at": "123"}, {"issued_at": True}, {"is_valid": 1}, {"scopes": TOKEN}],
)
def test_token_metadata_types_are_checked(monkeypatch, changes):
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": {**INFO, **changes}}),
    )
    with pytest.raises(ThreadsError, match="data"):
        asyncio.run(inspect_token(TOKEN))


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": NEW_TOKEN, "expires_in": 0},
        {"access_token": "", "expires_in": 10},
        {"access_token": NEW_TOKEN, "expires_in": "10"},
    ],
)
def test_bad_refresh_response_is_rejected(monkeypatch, payload):
    mock_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ThreadsError):
        asyncio.run(refresh_token(TOKEN))


def test_redaction_with_real_httpcore_logging(caplog):
    from social_mcp.threads.transport import RedactingTransport

    caplog.set_level(5)

    async def check():
        received = []

        async def respond(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            received.append(request)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with httpx.AsyncClient(
                transport=RedactingTransport(httpx.AsyncHTTPTransport()),
                trust_env=False,
            ) as client:
                response = await client.get(
                    f"http://127.0.0.1:{port}/debug_token",
                    params={"access_token": TOKEN, "input_token": TOKEN},
                )
                assert response.status_code == 200
                assert TOKEN not in str(response.request.url)
            assert TOKEN.encode() in received[0]

    asyncio.run(check())
    assert "httpcore" in caplog.text
    assert TOKEN not in caplog.text
