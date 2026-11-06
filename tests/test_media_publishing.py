import asyncio
import traceback
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import SecretStr

from social_mcp.accounts import Account, save_account
from social_mcp.mcp_server import SocialMCPServer
from social_mcp.threads import ThreadsConnection, ThreadsService, register_threads_tools
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ContainerStatus, ImageInput
from tests.support import mock_http

TOKEN = "synthetic-media-secret"
SIGNED_URL = (
    "https://CDN.example.com:443/photo%2F1.jpg?signature=synthetic-url-secret&a=1&a=2"
)
CAPTION = "  Exact caption & link\nhttps://example.com/?a=1&b=2  "


def images(count):
    return [
        {
            "image_url": SIGNED_URL if i == 0 else f"https://cdn.example.com/{i}.jpg",
            "alt_text": f"Image {i + 1}",
        }
        for i in range(count)
    ]


@pytest.fixture
def media_server(db_path, monkeypatch):
    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    pause = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.asyncio.sleep", pause)
    service = ThreadsService(db_path)
    server = SocialMCPServer("test")
    register_threads_tools(server, service, ThreadsConnection(service))
    return server, service, pause


@pytest.mark.parametrize("count", [1, 2, 20])
@pytest.mark.parametrize("caption", [None, "", CAPTION])
def test_complete_mcp_media_flow_preserves_order_content_and_privacy(
    media_server, monkeypatch, caplog, count, caption
):
    server, _, pause = media_server
    approved = images(count)
    child_ids = []
    checked = []
    published = []
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url) and "synthetic-url-secret" not in str(
            request.url
        )
        if request.method == "GET":
            container_id = request.url.path.rsplit("/", 1)[-1]
            assert dict(request.url.params) == {"fields": "status"}
            checked.append(container_id)
            return httpx.Response(200, json={"status": "FINISHED"})
        assert not request.url.query
        assert request.headers["Content-Type"].startswith(
            "application/x-www-form-urlencoded"
        )
        fields = parse_qs(request.content.decode(), keep_blank_values=True)
        if request.url.path == "/v1.0/123/threads_publish":
            parent = "101" if count == 1 else "900"
            assert fields == {"creation_id": [parent]}
            assert parent in checked
            published.append(parent)
            assert len(published) == 1
            return httpx.Response(200, json={"id": "999", "access_token": TOKEN})
        assert request.url.path == "/v1.0/123/threads"
        if fields["media_type"] == ["CAROUSEL"]:
            assert checked == child_ids
            expected = {"media_type": ["CAROUSEL"], "children": [",".join(child_ids)]}
            if caption is not None:
                expected["text"] = [caption]
            assert fields == expected
            return httpx.Response(200, json={"id": "900"})
        image = approved[len(child_ids)]
        expected = {
            "media_type": ["IMAGE"],
            "image_url": [image["image_url"]],
            "alt_text": [image["alt_text"]],
        }
        if count > 1:
            expected["is_carousel_item"] = ["true"]
        elif caption is not None:
            expected["text"] = [caption]
        assert fields == expected
        child_id = str(101 + len(child_ids))
        child_ids.append(child_id)
        return httpx.Response(200, json={"id": child_id, "secret": TOKEN})

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    arguments: dict[str, object] = {"account_id": "123", "text": caption}
    if count == 1:
        name = "threads_publish_image"
        arguments.update(approved[0])
    else:
        name = "threads_publish_carousel"
        arguments["images"] = approved
    result = asyncio.run(server.call_tool(name, arguments))
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    assert result.structured_content == {"post_id": "999"}
    assert len(requests) == (3 if count == 1 else 2 * count + 3)
    assert pause.await_count == (1 if count == 1 else 2)
    assert all(call.args == (30,) for call in pause.await_args_list)
    for secret in (TOKEN, SIGNED_URL, "synthetic-url-secret", CAPTION):
        assert secret not in result.model_dump_json() + caplog.text


def test_optional_image_fields_are_omitted_instead_of_sending_null(
    media_server, monkeypatch
):
    _, service, _ = media_server
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            assert parse_qs(request.content.decode()) == {
                "media_type": ["IMAGE"],
                "image_url": [SIGNED_URL],
            }
            return httpx.Response(200, json={"id": "101"})
        if request.method == "GET":
            return httpx.Response(200, json={"status": "FINISHED"})
        return httpx.Response(200, json={"id": "999"})

    mock_http(monkeypatch, respond)
    assert asyncio.run(service.publish_image("123", SIGNED_URL)).post_id == "999"


FAILURE_STAGES = [
    (1, "image_create"),
    (1, "image_status"),
    (1, "publish"),
    (2, "child_create_1"),
    (2, "child_create_2"),
    (2, "child_status_101"),
    (2, "child_status_102"),
    (2, "parent_create"),
    (2, "parent_status"),
    (2, "publish"),
]


@pytest.mark.parametrize("count, failure_stage", FAILURE_STAGES)
@pytest.mark.parametrize(
    "failure",
    ["timeout", "network", "invalid_json", "invalid_shape", 401, 403, 429, 503],
)
def test_each_failed_stage_aborts_without_retry_or_partial_publication(
    media_server, monkeypatch, caplog, count, failure_stage, failure
):
    _, service, _ = media_server
    events = []
    created = 0

    def respond(request):
        nonlocal created
        fields = parse_qs(request.content.decode()) if request.method == "POST" else {}
        if request.url.path.endswith("/threads_publish"):
            stage = "publish"
            normal = {"id": "999"}
        elif request.method == "GET":
            container_id = request.url.path.rsplit("/", 1)[-1]
            stage = (
                "image_status"
                if count == 1
                else (
                    "parent_status"
                    if container_id == "900"
                    else "child_status_" + container_id
                )
            )
            normal = {"status": "FINISHED"}
        elif fields["media_type"] == ["CAROUSEL"]:
            stage = "parent_create"
            normal = {"id": "900"}
        else:
            created += 1
            stage = "image_create" if count == 1 else f"child_create_{created}"
            normal = {"id": str(100 + created)}
        events.append(stage)
        if stage != failure_stage:
            return httpx.Response(200, json=normal)
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN + SIGNED_URL, request=request)
        if failure == "network":
            raise httpx.ConnectError(TOKEN + SIGNED_URL, request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text=TOKEN + SIGNED_URL)
        if failure == "invalid_shape":
            return httpx.Response(200, json={"id": TOKEN, "status": TOKEN})
        assert isinstance(failure, int)
        return httpx.Response(failure, json={"error": {"message": TOKEN + SIGNED_URL}})

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError) as caught:
        if count == 1:
            asyncio.run(service.publish_image("123", SIGNED_URL, text=CAPTION))
        else:
            asyncio.run(
                service.publish_carousel(
                    "123",
                    [ImageInput.model_validate(image) for image in images(count)],
                    text=CAPTION,
                )
            )
    assert events[-1] == failure_stage
    assert events.count(failure_stage) == 1
    assert events.count("publish") == (1 if failure_stage == "publish" else 0)
    if failure_stage == "publish" and failure in (
        "timeout",
        "network",
        "invalid_json",
        "invalid_shape",
        429,
        503,
    ):
        assert "threads_list_posts" in str(caught.value)
        assert "unknown" in str(caught.value)
    error_text = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert TOKEN not in error_text and SIGNED_URL not in error_text
    assert "synthetic-url-secret" not in error_text and CAPTION not in error_text


@pytest.mark.parametrize("count, status_target", [(1, "101"), (2, "102"), (2, "900")])
@pytest.mark.parametrize(
    "state", ["IN_PROGRESS", "ERROR", "EXPIRED", "PUBLISHED", "UNKNOWN"]
)
def test_unready_or_published_child_or_parent_never_reaches_publish(
    media_server, monkeypatch, count, status_target, state
):
    _, service, _ = media_server
    created = 0
    publications = []

    def respond(request):
        nonlocal created
        if request.method == "GET":
            container_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={"status": state if container_id == status_target else "FINISHED"},
            )
        if request.url.path.endswith("/threads_publish"):
            publications.append(request)
            return httpx.Response(200, json={"id": "999"})
        fields = parse_qs(request.content.decode())
        if fields["media_type"] == ["CAROUSEL"]:
            return httpx.Response(200, json={"id": "900"})
        created += 1
        return httpx.Response(200, json={"id": str(100 + created)})

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError):
        if count == 1:
            asyncio.run(service.publish_image("123", SIGNED_URL))
        else:
            asyncio.run(
                service.publish_carousel(
                    "123", [ImageInput.model_validate(image) for image in images(count)]
                )
            )
    assert publications == []


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("publish_image", {"image_url": "file:///private/photo.jpg"}),
        ("publish_image", {"image_url": SIGNED_URL, "text": "x" * 501}),
        ("publish_image", {"image_url": SIGNED_URL, "alt_text": "x" * 1001}),
        ("publish_carousel", {"images": []}),
        ("publish_carousel", {"images": images(1)}),
        ("publish_carousel", {"images": images(21)}),
        (
            "publish_carousel",
            {"images": [images(1)[0], {"image_url": "ftp://example.com/1"}]},
        ),
        ("publish_carousel", {"images": images(2), "text": "x" * 501}),
        (
            "publish_carousel",
            {
                "images": [
                    {"image_url": SIGNED_URL, "text": "unexpected child caption"}
                ]
                * 2
            },
        ),
    ],
)
def test_invalid_input_is_rejected_before_credentials_and_network(
    media_server, monkeypatch, method, kwargs
):
    _, service, _ = media_server
    current = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    monkeypatch.setattr("social_mcp.threads.service.create_image_container", create)
    with pytest.raises(ThreadsError):
        asyncio.run(getattr(service, method)("123", **kwargs))
    current.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.parametrize(
    "name, kwargs",
    [
        ("threads_publish_image", {"image_url": TOKEN}),
        ("threads_publish_image", {"image_url": SIGNED_URL, "alt_text": True}),
        ("threads_publish_carousel", {"images": images(1)}),
        ("threads_publish_carousel", {"images": [{"image_url": TOKEN}] * 2}),
        ("threads_publish_carousel", {"images": images(2), "text": TOKEN * 100}),
    ],
)
def test_mcp_rejects_invalid_media_without_echoing_values(
    media_server, monkeypatch, name, kwargs
):
    server, service, _ = media_server
    current = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool(name, {"account_id": "123", **kwargs}))
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert SIGNED_URL not in str(caught.value)
    current.assert_not_awaited()


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("publish_image", {"image_url": SIGNED_URL}),
        ("publish_carousel", {"images": images(2)}),
    ],
)
@pytest.mark.parametrize("expired", [False, True])
def test_missing_or_expired_account_cannot_create_media(
    db_path, monkeypatch, method, kwargs, expired
):
    if expired:
        save_account(
            db_path,
            platform="threads",
            account_id="123",
            username="example",
            access_token=TOKEN,
            obtained_at=1,
            expires_at=2,
        )
    create = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.create_image_container", create)
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(getattr(ThreadsService(db_path), method)("123", **kwargs))
    create.assert_not_awaited()


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("publish_image", {"image_url": SIGNED_URL}),
        ("publish_carousel", {"images": images(2)}),
    ],
)
def test_selected_accounts_latest_token_is_used(
    media_server, monkeypatch, method, kwargs
):
    _, service, _ = media_server
    account = Account(
        platform="threads",
        account_id="987",
        username="selected",
        access_token=SecretStr("synthetic-current-token"),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    current = AsyncMock(return_value=account)
    monkeypatch.setattr(service.tokens, "get_account", current)
    create = AsyncMock(side_effect=ThreadsError("stop before creating media"))
    monkeypatch.setattr("social_mcp.threads.service.create_image_container", create)
    with pytest.raises(ThreadsError, match="stop before"):
        asyncio.run(getattr(service, method)("987", **kwargs))
    current.assert_awaited_once_with("987")
    assert create.await_count == 1
    assert create.await_args is not None
    assert create.await_args.args[:2] == ("synthetic-current-token", "987")


def test_media_tools_are_marked_as_non_idempotent_writes(media_server):
    server, _, _ = media_server
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in ("threads_publish_image", "threads_publish_carousel"):
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.idempotent_hint is False
    schema = tools["threads_publish_carousel"].input_schema
    assert schema["properties"]["images"]["minItems"] == 2
    assert schema["properties"]["images"]["maxItems"] == 20
    assert set(schema["required"]) == {"account_id", "images"}


@pytest.mark.parametrize(
    "count, stage",
    [
        (1, "image_create"),
        (1, "image_status"),
        (1, "publish"),
        (2, "second_child"),
        (2, "children_wait"),
        (2, "parent_create"),
        (2, "parent_status"),
        (2, "publish"),
    ],
)
def test_cancellation_propagates_without_retry_or_follow_up_writes(
    media_server, monkeypatch, count, stage
):
    _, service, pause = media_server
    create = AsyncMock(side_effect=["101", "102"])
    parent = AsyncMock(return_value="900")
    ready = ContainerStatus(status="FINISHED")
    status = AsyncMock(return_value=ready)
    publish = AsyncMock(return_value="999")
    if stage == "image_create":
        create.side_effect = asyncio.CancelledError()
    elif stage == "second_child":
        create.side_effect = ["101", asyncio.CancelledError()]
    elif stage == "children_wait":
        pause.side_effect = asyncio.CancelledError()
    elif stage == "image_status":
        status.side_effect = asyncio.CancelledError()
    elif stage == "parent_create":
        parent.side_effect = asyncio.CancelledError()
    elif stage == "parent_status":
        status.side_effect = [ready, ready, asyncio.CancelledError()]
    elif stage == "publish":
        publish.side_effect = asyncio.CancelledError()
    monkeypatch.setattr("social_mcp.threads.service.create_image_container", create)
    monkeypatch.setattr(
        "social_mcp.threads.publishing.create_carousel_container", parent
    )
    monkeypatch.setattr("social_mcp.threads.publishing.fetch_container_status", status)
    monkeypatch.setattr("social_mcp.threads.publishing.publish_container", publish)
    with pytest.raises(asyncio.CancelledError):
        if count == 1:
            asyncio.run(service.publish_image("123", SIGNED_URL))
        else:
            asyncio.run(
                service.publish_carousel(
                    "123", [ImageInput.model_validate(image) for image in images(count)]
                )
            )
    assert publish.await_count == (1 if stage == "publish" else 0)
    if stage in {"second_child", "children_wait"}:
        parent.assert_not_awaited()
    assert create.await_count <= count


def test_stale_container_id_is_never_returned_as_a_published_post(
    media_server, monkeypatch
):
    server, _, _ = media_server

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"status": "FINISHED"})
        if request.url.path.endswith("/threads_publish"):
            return httpx.Response(200, json={"unexpected": "shape", "secret": TOKEN})
        return httpx.Response(200, json={"id": "101"})

    mock_http(monkeypatch, respond)
    with pytest.raises(ToolError, match="unknown") as caught:
        asyncio.run(
            server.call_tool(
                "threads_publish_image", {"account_id": "123", "image_url": SIGNED_URL}
            )
        )
    assert "threads_list_posts" in str(caught.value)
    assert TOKEN not in str(caught.value)
