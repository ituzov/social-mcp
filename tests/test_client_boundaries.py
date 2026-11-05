"""Regression checks for request semantics independent of diagnostics and DTOs."""

import asyncio
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from social_mcp.threads import client
from social_mcp.threads.client import http as client_http
from social_mcp.threads.diagnostics import PublicationStage
from social_mcp.threads.errors import ThreadsError, ThreadsTransientError
from social_mcp.threads.schemas import (
    PendingRepliesQuery,
    PostInsights,
    PostsQuery,
    ThreadsPost,
    ThreadsProfile,
    ThreadsReply,
)
from tests.support import PUBLISHING_TOKEN as TOKEN
from tests.support import mock_http


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("stage", ["status", "delete", "publish"])
def test_diagnostic_stage_cannot_change_http_method(
    monkeypatch, method: Literal["GET", "POST", "DELETE"], stage: PublicationStage
):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == method
        assert TOKEN not in str(request.url)
        return httpx.Response(200, json={"id": "123"})

    mock_http(monkeypatch, respond)
    asyncio.run(
        client_http.publication_request("123", TOKEN, {}, method=method, stage=stage)
    )
    assert len(requests) == 1


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("stage", ["status", "publish"])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_retry_safety_depends_on_http_method_not_stage(
    monkeypatch,
    method: Literal["GET", "POST", "DELETE"],
    stage: PublicationStage,
    failure,
):
    requests = []

    def respond(request):
        requests.append(request)
        raise failure("synthetic failure", request=request)

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(
            client_http.publication_request(
                "123",
                TOKEN,
                {},
                method=method,
                stage=stage,
                uncertain_message="Check before retrying the write.",
            )
        )
    assert isinstance(caught.value, ThreadsTransientError) is (method == "GET")
    assert len(requests) == 1
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("stage", ["delete", "moderate", "publish"])
def test_permission_context_is_explicit_not_diagnostic(
    monkeypatch, stage: PublicationStage
):
    mock_http(
        monkeypatch, lambda request: httpx.Response(403, json={"error": {"code": 10}})
    )
    with pytest.raises(ThreadsError, match="Selected operation needs permission"):
        asyncio.run(
            client_http.publication_request(
                "123",
                TOKEN,
                {},
                method="POST",
                stage=stage,
                permission_message="Selected operation needs permission.",
            )
        )


@pytest.mark.parametrize(
    "operation", ["profile", "posts", "replies", "conversation", "pending", "insights"]
)
def test_response_schema_extensions_do_not_change_meta_requests(monkeypatch, operation):
    """A DTO-only field must not become an unsupported Meta query parameter."""
    schemas: tuple[type[BaseModel], ...] = (
        ThreadsProfile,
        ThreadsPost,
        ThreadsReply,
        PostInsights,
    )
    for schema in schemas:
        monkeypatch.setattr(
            schema,
            "model_fields",
            {
                "local_only_field": FieldInfo(annotation=str),
            },
        )
    requests = []

    def respond(request):
        requests.append(request)
        assert "local_only_field" not in str(request.url)
        return httpx.Response(
            200,
            json={"id": "123", "username": "example"}
            if operation == "profile"
            else {"data": []},
        )

    mock_http(monkeypatch, respond)

    async def read():
        match operation:
            case "profile":
                return await client.fetch_profile(TOKEN)
            case "posts":
                return await client.fetch_posts(TOKEN, "123", PostsQuery())
            case "replies":
                return await client.fetch_replies(TOKEN, "123", PostsQuery())
            case "conversation":
                return await client.fetch_conversation(TOKEN, "123", PostsQuery())
            case "pending":
                return await client.fetch_pending_replies(
                    TOKEN, "123", PendingRepliesQuery()
                )
            case "insights":
                return await client.fetch_post_insights(TOKEN, "123")
            case _:
                raise AssertionError(f"Unknown test operation: {operation}")

    asyncio.run(read())
    assert len(requests) == 1
