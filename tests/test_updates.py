"""Update checks remain bounded, private, cached, and independent of tool results."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from social_mcp import updates
from social_mcp.mcp_server import SocialMCPServer
from social_mcp.updates import UpdateChecker
from tests.support import mock_http

SECRET = "synthetic-update-response-secret"


def release(tag="v0.1.2", **extra):
    return {"tag_name": tag, "draft": False, "prerelease": False, **extra}


@pytest.mark.parametrize(
    "current,latest,newer",
    [
        ("0.1.1", "v0.1.2", True),
        ("0.9.0", "v0.10.0", True),
        ("0.1.1", "v0.1.1", False),
        ("0.2.0", "v0.1.1", False),
    ],
)
def test_stable_versions_are_compared_numerically(monkeypatch, current, latest, newer):
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == updates.LATEST_RELEASE_URL
        assert request.method == "GET" and not request.url.query
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json=release(
                latest, body=SECRET, html_url="https://evil.example/" + SECRET
            ),
        )

    mock_http(monkeypatch, respond)
    result = asyncio.run(UpdateChecker(current).check())
    assert result.update_available is newer
    assert result.status == ("update_available" if newer else "up_to_date")
    assert result.latest_version == latest[1:]
    assert result.release_url == updates.RELEASES_URL + latest
    assert (
        SECRET not in result.model_dump_json()
        and "evil.example" not in result.model_dump_json()
    )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        release("v0.1.2rc1"),
        release("v0.2.0", draft=True),
        release("v0.2.0", prerelease=True),
        release("v0.2.0", draft="false"),
        release(SECRET),
        release("v0.2.0\n"),
        release("v0.2.0?secret=" + SECRET),
    ],
)
def test_unexpected_response_is_unknown_not_up_to_date(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    status = asyncio.run(UpdateChecker("0.1.1").check())
    assert status.status == "unavailable" and status.update_available is None
    assert status.latest_version is None and status.release_url is None
    assert SECRET not in status.model_dump_json()


@pytest.mark.parametrize("failure", [403, 404, 429, 500, "json", "timeout", "network"])
def test_network_failures_are_safe_cached_and_can_be_retried_explicitly(
    monkeypatch, failure, caplog
):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) > 1:
            return httpx.Response(200, json=release())
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "network":
            raise httpx.ConnectError(SECRET, request=request)
        if failure == "json":
            return httpx.Response(200, text=SECRET)
        return httpx.Response(failure, json={"message": SECRET})

    mock_http(monkeypatch, respond)
    checker = UpdateChecker("0.1.1")

    async def check():
        first = await checker.check()
        assert first.status == "unavailable" and first.update_available is None
        assert (await checker.check()) == first
        assert len(calls) == 1
        refreshed = await checker.check(force=True)
        assert refreshed.update_available is True

    asyncio.run(check())
    assert SECRET not in caplog.text
    assert len(calls) == 2


def test_cache_expires_inside_a_long_running_session(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(
        updates,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1800000000),
    )

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json=release("v0.1.1" if len(calls) == 1 else "v0.1.2")
        )

    mock_http(monkeypatch, respond)

    async def check():
        checker = UpdateChecker("0.1.1")
        assert (await checker.check()).update_available is False
        clock[0] = updates.CACHE_SECONDS - 1
        assert (await checker.check()).update_available is False
        assert len(calls) == 1
        clock[0] = updates.CACHE_SECONDS
        assert (await checker.check()).update_available is True

    asyncio.run(check())
    assert len(calls) == 2


@pytest.mark.parametrize("force", [False, True])
def test_parallel_checks_share_one_request(monkeypatch, force):
    calls = []

    async def respond(request):
        calls.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json=release())

    mock_http(monkeypatch, respond)

    async def check():
        checker = UpdateChecker("0.1.1")
        results = await asyncio.gather(*(checker.check(force=force) for _ in range(8)))
        assert all(r.update_available is True for r in results)

    asyncio.run(check())
    assert len(calls) == 1


def test_disabled_checks_never_create_an_http_client(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("Network must stay disabled")

    monkeypatch.setattr("social_mcp.updates.httpx.AsyncClient", forbidden)

    async def check():
        checker = UpdateChecker("0.1.1", enabled=False)
        assert (await checker.check()).status == "disabled"
        assert (await checker.check(force=True)).update_available is None

    asyncio.run(check())


def test_total_deadline_and_cancellation_do_not_poison_the_checker(monkeypatch):
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            await asyncio.Event().wait()
        return httpx.Response(200, json=release())

    mock_http(monkeypatch, respond)
    monkeypatch.setattr(updates, "REQUEST_TIMEOUT", 0.02)

    async def check():
        checker = UpdateChecker("0.1.1")
        assert (await checker.check()).status == "unavailable"
        assert (await checker.check(force=True)).update_available is True
        assert len(calls) == 2

    asyncio.run(check())


def test_cancellation_propagates_and_releases_the_lock(monkeypatch):
    started = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=release())

    mock_http(monkeypatch, respond)

    async def check():
        checker = UpdateChecker("0.1.1")
        task = asyncio.create_task(checker.check())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await checker.check()).update_available is True

    asyncio.run(check())


def test_notice_preserves_structured_data_and_appears_once_per_release(monkeypatch):
    clock = [0.0]
    tags = ["v0.1.2"]
    monkeypatch.setattr(
        updates,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1800000000),
    )
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, json=release(tags[0], body=SECRET)),
    )
    checker = UpdateChecker("0.1.1")
    server = SocialMCPServer("test", update_checker=checker)
    calls = []

    @server.tool()
    async def sample() -> dict[str, int]:
        calls.append("sample")
        return {"count": 7}

    async def check():
        first = await server.call_tool("sample", {})
        assert isinstance(first, CallToolResult)
        original = first.structured_content
        assert original == {"count": 7}
        assert isinstance(first.content[-1], TextContent)
        assert "update available: 0.1.2" in first.content[-1].text
        assert SECRET not in first.model_dump_json()
        second = await server.call_tool("sample", {})
        assert (
            isinstance(second, CallToolResult) and second.structured_content == original
        )
        assert len(first.content) == len(second.content) + 1
        clock[0] = updates.CACHE_SECONDS
        tags[0] = "v0.1.3"
        third = await server.call_tool("sample", {})
        assert isinstance(third, CallToolResult)
        assert "update available: 0.1.3" in third.content[-1].text

    asyncio.run(check())
    assert calls == ["sample"] * 3


def test_offline_check_cannot_break_or_repeat_a_write(monkeypatch):
    def respond(request):
        raise httpx.ConnectError(SECRET, request=request)

    mock_http(monkeypatch, respond)
    server = SocialMCPServer("test", update_checker=UpdateChecker("0.1.1"))
    writes = []

    @server.tool()
    async def write() -> str:
        writes.append("write")
        return "confirmed"

    result = asyncio.run(server.call_tool("write", {}))
    assert isinstance(result, CallToolResult) and not result.is_error
    assert writes == ["write"]
    assert SECRET not in result.model_dump_json()


def test_tool_errors_stay_errors_and_do_not_consume_the_notice(monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=release()))
    server = SocialMCPServer("test", update_checker=UpdateChecker("0.1.1"))

    @server.tool()
    async def broken() -> int:
        raise ToolError("Expected failure")

    @server.tool()
    async def good() -> int:
        return 1

    async def check():
        with pytest.raises(ToolError, match="Expected failure"):
            await server.call_tool("broken", {})
        result = await server.call_tool("good", {})
        assert (
            isinstance(result, CallToolResult)
            and "update available" in result.content[-1].text
        )

    asyncio.run(check())


def test_explicit_tool_checks_only_once_and_reports_disabled(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=release())

    mock_http(monkeypatch, respond)

    async def check():
        for enabled in (True, False):
            server = SocialMCPServer(
                "test", update_checker=UpdateChecker("0.1.1", enabled=enabled)
            )
            result = await server.call_tool("social_mcp_check_update", {})
            assert isinstance(result, CallToolResult)
            assert result.structured_content is not None
            assert result.structured_content["status"] == (
                "update_available" if enabled else "disabled"
            )
            assert len(result.content) == 1

    asyncio.run(check())
    assert len(calls) == 1


def test_parallel_tool_calls_emit_only_one_notice(monkeypatch):
    async def respond(request):
        await asyncio.sleep(0)
        return httpx.Response(200, json=release())

    mock_http(monkeypatch, respond)
    server = SocialMCPServer("test", update_checker=UpdateChecker("0.1.1"))

    @server.tool()
    async def sample() -> int:
        await asyncio.sleep(0)
        return 7

    async def check():
        results = await asyncio.gather(
            *(server.call_tool("sample", {}) for _ in range(8))
        )
        assert all(isinstance(result, CallToolResult) for result in results)
        assert sum(len(result.content) == 2 for result in results) == 1
        assert all(result.structured_content == {"result": 7} for result in results)

    asyncio.run(check())
