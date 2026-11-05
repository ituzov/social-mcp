"""Threads API insights operations."""

from pydantic import ValidationError

from social_mcp.threads.client import http as http_client
from social_mcp.threads.client.fields import POST_INSIGHT_METRICS
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import (
    AccountInsights,
    AccountInsightsQuery,
    PostInsights,
)

ACCOUNT_INSIGHTS_FORMAT_ERROR = (
    "Threads returned an unexpected account insights format."
)


async def fetch_post_insights(access_token: str, post_id: str) -> PostInsights:
    """Read own post counters; requires threads_manage_insights."""
    metrics = set(POST_INSIGHT_METRICS)
    data = await http_client.read_request(
        f"{post_id}/insights",
        access_token,
        {"metric": ",".join(sorted(metrics))},
        invalid_request_message=(
            "The post was not found or is unavailable to this account. "
            "Use a post_id from threads_list_posts for the selected account."
        ),
        permission_message=(
            "Insights are only available for your own posts. "
            "Check the selected account and the threads_manage_insights permission."
        ),
    )
    malformed = "Threads returned an unexpected post insights format."
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ThreadsError(malformed)
    counters = {}
    for item in data["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ThreadsError(malformed)
        name = item["name"]
        if name not in metrics:
            continue
        values = item.get("values")
        if values == []:
            continue
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], dict)
            or "value" not in values[0]
            or name in counters
        ):
            raise ThreadsError(malformed)
        counters[name] = values[0]["value"]
    try:
        return PostInsights(post_id=post_id, **counters)
    except ValidationError:
        raise ThreadsError(malformed) from None


async def fetch_account_insights(
    access_token: str, account_id: str, query: AccountInsightsQuery
) -> AccountInsights:
    """Read period metrics and current followers without sending dates for followers."""
    metrics = {"views", "likes", "replies", "reposts", "quotes", "clicks"}
    fields = await _fetch_account_metrics(
        access_token, account_id, metrics, query.model_dump(exclude_none=True)
    )
    fields.update(
        await _fetch_account_metrics(access_token, account_id, {"followers_count"}, {})
    )
    try:
        return AccountInsights.model_validate(
            {
                "account_id": account_id,
                "since": query.since,
                "until": query.until,
                **fields,
            }
        )
    except ValidationError:
        raise ThreadsError(ACCOUNT_INSIGHTS_FORMAT_ERROR) from None


async def _fetch_account_metrics(
    access_token: str, account_id: str, metrics: set[str], dates: dict[str, int]
) -> dict[str, object]:
    data = await http_client.read_request(
        f"{account_id}/threads_insights",
        access_token,
        {"metric": ",".join(sorted(metrics)), **dates},
        invalid_request_message=(
            "Account insights are unavailable. Check the selected account and date range."
        ),
        permission_message=(
            "Account insights require threads_manage_insights for the selected account."
        ),
    )
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ThreadsError(ACCOUNT_INSIGHTS_FORMAT_ERROR)
    fields = {}
    seen = set()
    for item in data["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ThreadsError(ACCOUNT_INSIGHTS_FORMAT_ERROR)
        name = item["name"]
        if name not in metrics:
            continue
        if name in seen:
            raise ThreadsError(ACCOUNT_INSIGHTS_FORMAT_ERROR)
        seen.add(name)
        fields[name] = _account_metric_value(name, item)
    return fields


def _account_metric_value(name: str, item: dict[str, object]) -> object:
    """Keep daily views, sum link clicks, and extract other metric totals."""
    if name == "views":
        values = item.get("values")
        if not isinstance(values, list):
            raise ThreadsError(ACCOUNT_INSIGHTS_FORMAT_ERROR)
        return values or None
    if name == "clicks":
        links = item.get("link_total_values")
        if not isinstance(links, list):
            raise ThreadsError(ACCOUNT_INSIGHTS_FORMAT_ERROR)
        counts = []
        for link in links:
            if (
                not isinstance(link, dict)
                or type(link.get("value")) is not int
                or link["value"] < 0
            ):
                raise ThreadsError(ACCOUNT_INSIGHTS_FORMAT_ERROR)
            counts.append(link["value"])
        return sum(counts) if counts else None
    total = item.get("total_value")
    if not isinstance(total, dict) or "value" not in total:
        raise ThreadsError(ACCOUNT_INSIGHTS_FORMAT_ERROR)
    return total["value"]
