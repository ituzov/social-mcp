import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from social_mcp.accounts import Account
from social_mcp.threads import ThreadsService, client
from social_mcp.threads.diagnostics import record_publication
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ContainerStatus
from tests.support import mock_http

TOKEN = "synthetic-diagnostic-token"
TEXT = "synthetic-private-post-text"
DIAGNOSTIC_LOGGER = "social_mcp.threads.diagnostics"


def diagnostic_records(caplog):
    return [record for record in caplog.records if record.name == DIAGNOSTIC_LOGGER]


def assert_safe_records(caplog):
    assert TOKEN not in caplog.text
    assert TEXT not in caplog.text
    for record in diagnostic_records(caplog):
        assert record.exc_info is None
        assert record.stack_info is None
        assert TOKEN not in repr(record.__dict__)
        assert TEXT not in repr(record.__dict__)
        assert "https://" not in record.getMessage()


def invoke(stage):
    if stage == "create":
        return client.create_text_container(TOKEN, "123", TEXT)
    if stage == "status":
        return client.fetch_container_status(TOKEN, "456")
    return client.publish_container(TOKEN, "123", "456", reply_to_id="789")


@pytest.mark.parametrize("stage", ["create", "status", "publish"])
@pytest.mark.parametrize(
    "failure, reason, status",
    [
        ("timeout", "timeout", None),
        ("network", "network_error", None),
        ("server", "api_error", 503),
        ("permission", "api_error", 403),
        ("json", "invalid_json", 200),
        ("schema", "invalid_schema", 200),
    ],
)
def test_failure_stage_reason_and_http_status_are_safe(
    monkeypatch, caplog, stage, failure, reason, status
):
    requests = []

    def respond(request):
        requests.append(request)
        if failure in {"timeout", "network"}:
            error_type = (
                httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            )
            sensitive_request = httpx.Request("POST", f"https://example.com/{TOKEN}")
            raise error_type(f"{TOKEN} {TEXT}", request=sensitive_request)
        if failure == "json":
            return httpx.Response(200, text=f"<html>{TOKEN} {TEXT}</html>")
        if failure == "schema":
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "status": {"private": TOKEN},
                    "text": TEXT,
                    "access_token": TOKEN,
                },
            )
        assert status is not None
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": 2 if status == 503 else 10,
                    "message": TOKEN,
                    "error_user_msg": TEXT,
                }
            },
        )

    mock_http(monkeypatch, respond)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(invoke(stage))
    assert TOKEN not in str(caught.value) and TEXT not in str(caught.value)
    messages = [record.getMessage() for record in diagnostic_records(caplog)]
    assert any(
        f"stage={stage} reason={reason} http_status={status}" in message
        for message in messages
    )
    if failure == "schema":
        assert any(
            "reason=invalid_schema" in message and "id_type=integer" in message
            for message in messages
        )
    if stage == "publish" and failure != "permission":
        assert "outcome is unknown" in str(caught.value)
        assert "threads_list_replies" in str(caught.value)
    if failure == "permission":
        assert "threads_content_publish" in str(caught.value)
        assert "outcome is unknown" not in str(caught.value)
    assert len(requests) == 1
    assert_safe_records(caplog)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, "missing"),
        ({"id": None}, "null"),
        ({"id": TOKEN}, "string"),
        ({"id": 123}, "integer"),
        ({"id": True}, "boolean"),
        ({"id": [TOKEN]}, "array"),
        ({"id": {"secret": TOKEN}}, "object"),
        ({"id": 1.5}, "number"),
        ([TOKEN], "missing"),
    ],
)
def test_only_id_type_is_logged_never_its_value(caplog, payload, expected):
    caplog.set_level(logging.INFO)
    record_publication("publish", "invalid_schema", http_status=200, payload=payload)
    assert f"id_type={expected}" in caplog.text
    assert_safe_records(caplog)


@pytest.mark.parametrize(
    "state, expected",
    [
        ("FINISHED", "FINISHED"),
        ("PUBLISHED", "PUBLISHED"),
        (TOKEN, "unknown"),
        ({"secret": TOKEN}, "unknown"),
        (None, "unknown"),
    ],
)
def test_container_states_are_allowlisted(caplog, state, expected):
    caplog.set_level(logging.INFO)
    record_publication(
        "status",
        "response_received",
        http_status=200,
        payload={"status": state, "text": TEXT, "access_token": TOKEN},
    )
    assert f"container_status={expected}" in caplog.text
    assert_safe_records(caplog)


@pytest.mark.parametrize("stage", ["create", "status", "publish"])
def test_successful_response_metadata_never_contains_payload(
    monkeypatch, caplog, stage
):
    caplog.set_level(logging.DEBUG)
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "id": "456",
                "status": "FINISHED",
                "text": TEXT,
                "access_token": TOKEN,
            },
        ),
    )
    asyncio.run(invoke(stage))
    records = diagnostic_records(caplog)
    assert any(
        "reason=response_received http_status=200 id_type=string" in record.getMessage()
        for record in records
    )
    assert all(record.levelno == logging.INFO for record in records)
    assert_safe_records(caplog)


@pytest.mark.parametrize(
    "state, reason",
    [
        ("PUBLISHED", "already_published"),
        ("IN_PROGRESS", "not_ready"),
        (TOKEN, "not_ready"),
        ("FINISHED", "confirmed"),
    ],
)
def test_service_reports_already_published_separately_from_network_and_schema_errors(
    db_path, monkeypatch, caplog, state, reason
):
    caplog.set_level(logging.INFO)
    service = ThreadsService(db_path)
    account = Account(
        platform="threads",
        account_id="123",
        username="example",
        access_token=SecretStr(TOKEN),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    monkeypatch.setattr(service.tokens, "get_account", AsyncMock(return_value=account))
    monkeypatch.setattr(
        "social_mcp.threads.service.create_text_container",
        AsyncMock(return_value="456"),
    )
    monkeypatch.setattr("social_mcp.threads.service.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(
        "social_mcp.threads.publishing.fetch_container_status",
        AsyncMock(return_value=ContainerStatus(status=state)),
    )
    publish = AsyncMock(return_value="999")
    monkeypatch.setattr("social_mcp.threads.publishing.publish_container", publish)
    if state == "FINISHED":
        assert asyncio.run(service.reply("123", "789", TEXT)).post_id == "999"
        publish.assert_awaited_once()
    else:
        with pytest.raises(ThreadsError):
            asyncio.run(service.reply("123", "789", TEXT))
        publish.assert_not_awaited()
    assert any(
        f"reason={reason}" in record.getMessage()
        for record in diagnostic_records(caplog)
    )
    assert_safe_records(caplog)


@pytest.mark.parametrize("stage", ["create", "status", "publish"])
def test_cancelled_request_is_logged_without_retry(monkeypatch, caplog, stage):
    caplog.set_level(logging.INFO)
    requests = []

    def respond(request):
        requests.append(request)
        raise asyncio.CancelledError()

    mock_http(monkeypatch, respond)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(invoke(stage))
    assert len(requests) == 1
    assert f"stage={stage} reason=cancelled" in caplog.text
    assert_safe_records(caplog)


def test_diagnostics_use_stderr_and_leave_stdout_for_mcp():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from social_mcp.threads.diagnostics import record_publication; record_publication('publish', 'timeout')",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert "stage=publish reason=timeout" in result.stderr


@pytest.mark.parametrize("http_status", [TOKEN, True, 0, 999])
def test_http_status_metadata_is_restricted_to_actual_http_codes(caplog, http_status):
    record_publication("publish", "api_error", http_status=http_status)
    assert "http_status=None" in caplog.text
    assert_safe_records(caplog)
