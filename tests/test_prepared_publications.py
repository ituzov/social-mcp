"""Recovery must reuse prepared containers and never resend an uncertain write."""

import asyncio
import sqlite3
from collections import Counter
from contextlib import closing
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.types import CallToolResult

from social_mcp.threads import ThreadsService
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ImageInput
from tests.support import mock_http

TOKEN = "synthetic-prepared-token"
CAPTION = "Exact approved carousel caption"


@pytest.fixture
def preparation(connected, monkeypatch):
    _, service, _ = connected
    return service


def test_processing_is_polled_without_recreating_media(preparation, monkeypatch):
    service = preparation
    events = Counter()

    def respond(request):
        path = request.url.path
        events[path] += 1
        if request.method == "GET":
            return httpx.Response(
                200, json={"status": "IN_PROGRESS" if events[path] == 1 else "FINISHED"}
            )
        return httpx.Response(
            200, json={"id": "900" if path.endswith("threads_publish") else "101"}
        )

    mock_http(monkeypatch, respond)
    assert asyncio.run(service.publish_text("123", "Approved text")).post_id == "900"
    assert events == {
        "/v1.0/123/threads": 1,
        "/v1.0/101": 2,
        "/v1.0/123/threads_publish": 1,
    }


@pytest.mark.parametrize("pending_stage", ["children", "parent"])
def test_restart_resumes_carousel_without_new_images_or_duplicate_parent(
    preparation, monkeypatch, pending_stage
):
    service = preparation
    events = Counter()
    processing = [True]

    def respond(request):
        path = request.url.path
        if request.method == "GET":
            events[path] += 1
            target = "101" if pending_stage == "children" else "800"
            status = (
                "IN_PROGRESS"
                if processing[0] and path.endswith("/" + target)
                else "FINISHED"
            )
            return httpx.Response(200, json={"status": status})
        if path.endswith("threads_publish"):
            events["publish"] += 1
            return httpx.Response(200, json={"id": "900"})
        fields = parse_qs(request.content.decode())
        if fields["media_type"] == ["CAROUSEL"]:
            events["parent"] += 1
            assert fields["text"] == [CAPTION]
            assert fields["children"] == ["101,102"]
            return httpx.Response(200, json={"id": "800"})
        events["image"] += 1
        return httpx.Response(200, json={"id": str(100 + events["image"])})

    mock_http(monkeypatch, respond)
    images = [
        ImageInput(image_url=f"https://example.com/{i}.png?private=synthetic-url")
        for i in range(2)
    ]
    with pytest.raises(ThreadsError, match="threads_resume_publication"):
        asyncio.run(service.publish_carousel("123", images, text=CAPTION))
    target_path = "/v1.0/101" if pending_stage == "children" else "/v1.0/800"
    assert events[target_path] == 3
    saved = asyncio.run(service.list_publications("123"))
    assert len(saved) == 1 and saved[0].state == "preparing"
    assert CAPTION not in saved[0].model_dump_json()
    assert "synthetic-url" not in saved[0].model_dump_json()
    with closing(sqlite3.connect(service.db_path)) as connection:
        raw = str(connection.execute("SELECT * FROM threads_publications").fetchall())
        assert "synthetic-url" not in raw and TOKEN not in raw
    processing[0] = False
    restarted = ThreadsService(service.db_path)
    result = asyncio.run(restarted.resume_publication("123", saved[0].publication_id))
    assert result.post_id == "900"
    assert events["image"] == 2 and events["parent"] == 1 and events["publish"] == 1
    assert (
        asyncio.run(restarted.resume_publication("123", saved[0].publication_id))
        == result
    )
    assert events["publish"] == 1
    with closing(sqlite3.connect(service.db_path)) as connection:
        assert connection.execute(
            "SELECT caption FROM threads_publications"
        ).fetchone() == (None,)


@pytest.mark.parametrize("failure", ["timeout", "cancel", "json", 403, 503])
def test_uncertain_or_rejected_publish_is_never_resent_after_restart(
    preparation, monkeypatch, failure
):
    service = preparation
    calls = Counter()

    def respond(request):
        calls[request.url.path] += 1
        if request.method == "GET":
            return httpx.Response(200, json={"status": "FINISHED"})
        if not request.url.path.endswith("threads_publish"):
            return httpx.Response(200, json={"id": "101"})
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "json":
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(failure, json={"error": {"message": TOKEN}})

    mock_http(monkeypatch, respond)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ThreadsError):
        asyncio.run(service.publish_text("123", "Approved"))
    saved = asyncio.run(service.list_publications("123"))[0]
    assert saved.state == "publishing"
    before = calls.copy()
    restarted = ThreadsService(service.db_path)
    forbidden = AsyncMock(
        side_effect=AssertionError("No token maintenance for a blocked write")
    )
    monkeypatch.setattr(restarted.tokens, "get_account", forbidden)
    with pytest.raises(ThreadsError, match="outcome is unknown"):
        asyncio.run(restarted.resume_publication("123", saved.publication_id))
    assert calls == before
    assert calls["/v1.0/123/threads_publish"] == 1


def test_processing_deadline_preserves_preparation_without_publishing(
    preparation, monkeypatch
):
    service = preparation
    monkeypatch.setattr("social_mcp.threads.publishing.PREPARATION_TIMEOUT", 0.02)
    started = asyncio.Event()

    async def status(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "social_mcp.threads.service.create_text_container",
        AsyncMock(return_value="101"),
    )
    monkeypatch.setattr("social_mcp.threads.publishing.fetch_container_status", status)
    publish = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.publishing.publish_container", publish)

    async def bounded_attempt():
        return await asyncio.wait_for(service.publish_text("123", "Approved"), 0.5)

    with pytest.raises(ThreadsError, match="No publication was attempted"):
        asyncio.run(bounded_attempt())
    assert asyncio.run(service.list_publications("123"))[0].state == "preparing"
    publish.assert_not_awaited()


def test_cancellation_before_publish_remains_resumable(preparation, monkeypatch):
    service = preparation
    monkeypatch.setattr(
        "social_mcp.threads.service.create_text_container",
        AsyncMock(return_value="101"),
    )
    status = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr("social_mcp.threads.publishing.fetch_container_status", status)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.publish_text("123", "Approved"))
    saved = asyncio.run(service.list_publications("123"))[0]
    assert saved.state == "preparing"
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"status": "FINISHED"} if request.method == "GET" else {"id": "900"},
        ),
    )
    # Restore the real status operation for the new service instance.
    from social_mcp.threads.client import fetch_container_status

    monkeypatch.setattr(
        "social_mcp.threads.publishing.fetch_container_status", fetch_container_status
    )
    assert (
        asyncio.run(
            ThreadsService(service.db_path).resume_publication(
                "123", saved.publication_id
            )
        ).post_id
        == "900"
    )


def test_simultaneous_resume_cannot_send_twice(preparation, monkeypatch):
    service = preparation
    started = asyncio.Event()
    release = asyncio.Event()
    publications = []

    async def respond(request):
        if request.method == "GET":
            started.set()
            await release.wait()
            return httpx.Response(200, json={"status": "FINISHED"})
        publications.append(request)
        return httpx.Response(200, json={"id": "900"})

    mock_http(monkeypatch, respond)

    async def run():
        saved_id = await service.publications.prepare("123", container_id="101")
        first = asyncio.create_task(service.resume_publication("123", saved_id))
        await started.wait()
        with pytest.raises(ThreadsError, match="already being handled"):
            await ThreadsService(service.db_path).resume_publication("123", saved_id)
        release.set()
        assert (await first).post_id == "900"

    asyncio.run(run())
    assert len(publications) == 1


@pytest.mark.parametrize(
    "account_id,publication_id", [("other", "../private"), ("999", "0" * 32)]
)
def test_invalid_or_wrong_owner_resume_never_reads_credentials(
    preparation, monkeypatch, account_id, publication_id
):
    credentials = AsyncMock(side_effect=AssertionError("No credentials"))
    monkeypatch.setattr(preparation.tokens, "get_account", credentials)
    with pytest.raises(ThreadsError):
        asyncio.run(preparation.resume_publication(account_id, publication_id))
    credentials.assert_not_awaited()


def test_recovery_tools_preserve_read_and_write_permissions(connected, monkeypatch):
    server, service, _ = connected
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert tools["threads_list_publications"].annotations.read_only_hint is True
    assert tools["threads_resume_publication"].annotations.read_only_hint is False
    assert tools["threads_resume_publication"].annotations.idempotent_hint is True
    result = asyncio.run(
        server.call_tool("threads_list_publications", {"account_id": "123"})
    )
    assert isinstance(result, CallToolResult) and not result.is_error


@pytest.mark.parametrize("failed_state", ["publishing", "published"])
def test_checkpoint_failure_cannot_allow_a_second_send(
    preparation, monkeypatch, failed_state
):
    service = preparation
    original = service.publications.store.change
    publish = AsyncMock(return_value="900")
    from social_mcp.threads.schemas import ContainerStatus

    def change(publication_id, **values):
        if values.get("state") == failed_state:
            raise ThreadsError("Synthetic storage failure")
        original(publication_id, **values)

    monkeypatch.setattr(service.publications.store, "change", change)
    monkeypatch.setattr(
        "social_mcp.threads.service.create_text_container",
        AsyncMock(return_value="101"),
    )
    monkeypatch.setattr(
        "social_mcp.threads.publishing.fetch_container_status",
        AsyncMock(return_value=ContainerStatus(status="FINISHED")),
    )
    monkeypatch.setattr("social_mcp.threads.publishing.publish_container", publish)
    with pytest.raises(ThreadsError, match="storage failure"):
        asyncio.run(service.publish_text("123", "Approved"))
    assert publish.await_count == (0 if failed_state == "publishing" else 1)
    saved = asyncio.run(service.list_publications("123"))[0]
    assert saved.state == (
        "preparing" if failed_state == "publishing" else "publishing"
    )
    if failed_state == "published":
        with pytest.raises(ThreadsError, match="unknown"):
            asyncio.run(
                ThreadsService(service.db_path).resume_publication(
                    "123", saved.publication_id
                )
            )
        assert publish.await_count == 1


def test_resume_survives_a_real_process_restart(preparation):
    import subprocess
    import sys

    service = preparation
    publication_id = asyncio.run(
        service.publications.prepare("123", container_id="101")
    )
    script = """
import asyncio, sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from social_mcp.threads import ThreadsService
from social_mcp.threads.schemas import ContainerStatus
service = ThreadsService(Path(sys.argv[1]))
with patch("social_mcp.threads.publishing.fetch_container_status", AsyncMock(return_value=ContainerStatus(status="FINISHED"))), patch("social_mcp.threads.publishing.publish_container", AsyncMock(return_value="900")) as publish:
    result = asyncio.run(service.resume_publication("123", sys.argv[2]))
    assert result.post_id == "900" and publish.await_count == 1
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(service.db_path), publication_id],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert (
        asyncio.run(service.resume_publication("123", publication_id)).post_id == "900"
    )


def test_another_process_lock_prevents_publication(preparation):
    import subprocess
    import sys

    service = preparation
    publication_id = asyncio.run(
        service.publications.prepare("123", container_id="101")
    )
    path = service.db_path.with_name(
        f"{service.db_path.name}.publish-{publication_id}.lock"
    )
    script = 'from filelock import FileLock; import sys; lock=FileLock(sys.argv[1]); lock.acquire(); print("ready",flush=True); sys.stdin.readline(); lock.release()'
    worker = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert worker.stdout.readline().strip() == "ready"
        with pytest.raises(ThreadsError, match="already being handled"):
            asyncio.run(service.resume_publication("123", publication_id))
    finally:
        worker.communicate("release\n", timeout=10)


def test_other_account_cannot_resume_an_existing_preparation(preparation, monkeypatch):
    service = preparation
    saved_id = asyncio.run(service.publications.prepare("123", container_id="101"))
    forbidden = AsyncMock(side_effect=AssertionError("No credentials"))
    monkeypatch.setattr(service.tokens, "get_account", forbidden)
    with pytest.raises(ThreadsError, match="selected account"):
        asyncio.run(service.resume_publication("999", saved_id))
    forbidden.assert_not_awaited()
