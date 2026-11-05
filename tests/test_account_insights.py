import asyncio
import traceback
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import SecretStr, ValidationError

from social_mcp.accounts import Account, save_account
from social_mcp.threads import ThreadsConnection, client, register_threads_tools
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import AccountInsights, AccountInsightsQuery
from social_mcp.threads.service import ThreadsService
from tests.support import mock_http

TOKEN = "synthetic-account-insights-secret"
RANGE = {"since": 1712991600, "until": 1713078000}


def account_response(request):
    if request.url.params["metric"] == "followers_count":
        return httpx.Response(
            200,
            json={"data": [{"name": "followers_count", "total_value": {"value": 12}}]},
        )
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "name": "views",
                    "values": [
                        {
                            "value": 10,
                            "end_time": "2024-04-14T08:00:00+0000",
                            "secret": TOKEN,
                        },
                        {"value": 0, "end_time": "2024-04-15T08:00:00+0000"},
                    ],
                },
                {"name": "likes", "total_value": {"value": 0}},
                {"name": "replies", "total_value": {"value": 3}},
                {"name": "reposts", "total_value": {"value": 2}},
                {"name": "quotes", "total_value": {"value": 1}},
                {
                    "name": "clicks",
                    "link_total_values": [
                        {"value": 2, "link_url": TOKEN},
                        {"value": 3, "link_url": TOKEN},
                    ],
                },
                {"name": "future_metric", "future_shape": TOKEN},
            ],
            "paging": {"next": TOKEN},
        },
    )


def test_default_range_preserves_meta_default_instead_of_inventing_dates():
    assert AccountInsightsQuery().model_dump(exclude_none=True) == {}


def test_explicit_range_preserves_exact_unix_timestamps():
    values = {"since": 1712991600, "until": 1713078000}
    assert AccountInsightsQuery.model_validate(values).model_dump() == values


@pytest.mark.parametrize(
    "values",
    [
        {"since": 1712991600},
        {"until": 1713078000},
        {"since": 1713078000, "until": 1712991600},
        {"since": 1712991600, "until": 1712991600},
        {"since": 1712991599, "until": 1713078000},
        {"since": 1712991598, "until": 1712991599},
        {"since": "1712991600", "until": 1713078000},
        {"since": 1712991600.0, "until": 1713078000},
        {"since": True, "until": 1713078000},
        {"since": 1712991600, "until": "1713078000"},
        {"since": 1712991600, "until": 1713078000.0},
        {"since": 1712991600, "until": True},
    ],
)
def test_invalid_range_is_rejected_without_coercion(values):
    with pytest.raises(ValidationError):
        AccountInsightsQuery.model_validate(values)


@pytest.mark.parametrize("dates", [{}, RANGE])
def test_account_requests_separate_period_metrics_from_current_followers(
    monkeypatch, caplog, dates
):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/v1.0/123/threads_insights"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        if request.url.params["metric"] == "followers_count":
            assert dict(request.url.params) == {"metric": "followers_count"}
        else:
            assert set(request.url.params["metric"].split(",")) == {
                "views",
                "likes",
                "replies",
                "reposts",
                "quotes",
                "clicks",
            }
            assert {k: request.url.params[k] for k in dates} == {
                k: str(v) for k, v in dates.items()
            }
            assert set(request.url.params) == {"metric", *dates}
        return account_response(request)

    caplog.set_level(5)
    mock_http(monkeypatch, respond)
    result = asyncio.run(
        client.fetch_account_insights(
            TOKEN, "123", AccountInsightsQuery.model_validate(dates)
        )
    )
    assert len(requests) == 2
    assert result.account_id == "123"
    assert result.since == dates.get("since")
    assert result.until == dates.get("until")
    assert result.views is not None
    assert [day.value for day in result.views] == [10, 0]
    assert result.views[0].end_time.isoformat() == "2024-04-14T08:00:00+00:00"
    assert (result.likes, result.replies, result.reposts, result.quotes) == (0, 3, 2, 1)
    assert result.clicks == 5
    assert result.followers_count == 12
    assert TOKEN not in result.model_dump_json() + caplog.text


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"name": "likes", "total_value": {"value": None}}],
        [{"name": "views", "values": []}, {"name": "clicks", "link_total_values": []}],
    ],
)
def test_missing_or_empty_metrics_remain_unknown(monkeypatch, entries):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={"data": entries}))
    result = asyncio.run(
        client.fetch_account_insights(TOKEN, "123", AccountInsightsQuery())
    )
    assert all(v is None for k, v in result.model_dump().items() if k != "account_id")


@pytest.mark.parametrize(
    "entries",
    [
        None,
        {},
        [None],
        [{"name": 1}],
        [{"name": "likes"}],
        [{"name": "likes", "total_value": {}}],
        [{"name": "likes", "total_value": {"value": -1}}],
        [{"name": "likes", "total_value": {"value": "1"}}],
        [{"name": "likes", "total_value": {"value": True}}],
        [{"name": "likes", "total_value": {"value": 1}}] * 2,
        [{"name": "views", "values": None}],
        [{"name": "views", "values": [None]}],
        [
            {
                "name": "views",
                "values": [{"value": True, "end_time": "2024-04-14T08:00:00Z"}],
            }
        ],
        [{"name": "views", "values": [{"value": 1, "end_time": TOKEN}]}],
        [
            {
                "name": "views",
                "values": [{"value": 1, "end_time": "2024-04-14T08:00:00"}],
            }
        ],
        [{"name": "views", "values": [{"value": 1}]}],
        [{"name": "clicks", "link_total_values": None}],
        [{"name": "clicks", "link_total_values": [None]}],
        [{"name": "clicks", "link_total_values": [{}]}],
        [{"name": "clicks", "link_total_values": [{"value": True}]}],
        [{"name": "clicks", "link_total_values": [{"value": "1"}]}],
        [{"name": "clicks", "link_total_values": [{"value": -1}]}],
    ],
)
def test_malformed_account_metrics_fail_without_exposing_response(monkeypatch, entries):
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": entries, "secret": TOKEN}),
    )
    with pytest.raises(ThreadsError, match="account insights format") as caught:
        asyncio.run(client.fetch_account_insights(TOKEN, "123", AccountInsightsQuery()))
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("value", [True, -1, "12"])
def test_malformed_follower_count_fails_safely(monkeypatch, value):
    def respond(request):
        if request.url.params["metric"] == "followers_count":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"name": "followers_count", "total_value": {"value": value}}
                    ]
                },
            )
        return account_response(request)

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError, match="account insights format"):
        asyncio.run(client.fetch_account_insights(TOKEN, "123", AccountInsightsQuery()))


@pytest.mark.parametrize(
    "status, message",
    [
        (401, "token"),
        (403, "threads_manage_insights"),
        (429, "rate limit"),
        (503, "Temporary"),
    ],
)
@pytest.mark.parametrize("follower_request", [False, True])
def test_api_errors_on_either_request_are_safe_and_not_retried(
    monkeypatch, status, message, follower_request
):
    requests = []

    def respond(request):
        requests.append(request)
        if (request.url.params["metric"] == "followers_count") == follower_request:
            return httpx.Response(status, json={"error": {"message": TOKEN}})
        return account_response(request)

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError, match=message) as caught:
        asyncio.run(client.fetch_account_insights(TOKEN, "123", AccountInsightsQuery()))
    assert len(requests) == (2 if follower_request else 1)
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("failure", ["timeout", "network", "json", "not_found"])
def test_transport_and_invalid_response_errors_are_safe(monkeypatch, failure):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "network":
            raise httpx.ConnectError(TOKEN, request=request)
        if failure == "json":
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(404, json={"error": {"code": 100, "message": TOKEN}})

    mock_http(monkeypatch, respond)
    with pytest.raises(ThreadsError) as caught:
        asyncio.run(client.fetch_account_insights(TOKEN, "123", AccountInsightsQuery()))
    assert len(requests) == 1
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("account_id", ["123", "987"])
def test_service_uses_selected_accounts_current_token(db_path, monkeypatch, account_id):
    service = ThreadsService(db_path)
    account = Account(
        platform="threads",
        account_id=account_id,
        username="example",
        access_token=SecretStr(TOKEN),
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    current = AsyncMock(return_value=account)
    fetch = AsyncMock(return_value=AccountInsights(account_id=account_id))
    monkeypatch.setattr(service.tokens, "get_account", current)
    monkeypatch.setattr("social_mcp.threads.service.fetch_account_insights", fetch)
    assert (
        asyncio.run(service.get_account_insights(account_id, **RANGE))
        is fetch.return_value
    )
    current.assert_awaited_once_with(account_id)
    fetch.assert_awaited_once_with(TOKEN, account_id, AccountInsightsQuery(**RANGE))


def test_invalid_service_range_does_not_read_credentials_or_call_api(
    db_path, monkeypatch
):
    service = ThreadsService(db_path)
    current = AsyncMock()
    fetch = AsyncMock()
    monkeypatch.setattr(service.tokens, "get_account", current)
    monkeypatch.setattr("social_mcp.threads.service.fetch_account_insights", fetch)
    with pytest.raises(ThreadsError, match="both since and until"):
        asyncio.run(service.get_account_insights("123", since=1712991600))
    current.assert_not_awaited()
    fetch.assert_not_awaited()


@pytest.mark.parametrize("expired", [False, True])
def test_service_blocks_missing_or_expired_account(db_path, monkeypatch, expired):
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
    fetch = AsyncMock()
    monkeypatch.setattr("social_mcp.threads.service.fetch_account_insights", fetch)
    with pytest.raises(ThreadsError, match="is connected|expired"):
        asyncio.run(ThreadsService(db_path).get_account_insights("123"))
    fetch.assert_not_awaited()


def test_account_insights_through_mcp_has_public_output_and_read_only_annotation(
    db_path, monkeypatch
):
    save_account(
        db_path,
        platform="threads",
        account_id="123",
        username="example",
        access_token=TOKEN,
        obtained_at=1,
        expires_at=4_000_000_000,
    )
    mock_http(monkeypatch, account_response)
    service = ThreadsService(db_path)
    server = MCPServer("test")
    register_threads_tools(server, service, ThreadsConnection(service))
    result = asyncio.run(
        server.call_tool("threads_get_account_insights", {"account_id": "123", **RANGE})
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["likes"] == 0
    assert result.structured_content["followers_count"] == 12
    assert result.structured_content["since"] == RANGE["since"]
    assert len(result.structured_content["views"]) == 2
    assert TOKEN not in result.model_dump_json()
    tool = next(
        t
        for t in asyncio.run(server.list_tools())
        if t.name == "threads_get_account_insights"
    )
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True


def test_mcp_translates_account_permission_error(db_path, monkeypatch):
    service = ThreadsService(db_path)
    fetch = AsyncMock(
        side_effect=ThreadsError("Account insights require threads_manage_insights.")
    )
    monkeypatch.setattr(service, "get_account_insights", fetch)
    server = MCPServer("test")
    register_threads_tools(server, service, ThreadsConnection(service))
    with pytest.raises(ToolError, match="threads_manage_insights"):
        asyncio.run(
            server.call_tool("threads_get_account_insights", {"account_id": "123"})
        )
