"""Threads API replies operations."""

from typing import Literal

from pydantic import ValidationError

from social_mcp.threads.client import http as http_client
from social_mcp.threads.client.fields import PENDING_REPLY_FIELDS, REPLY_FIELDS
from social_mcp.threads.diagnostics import record_publication
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import (
    ActionAcknowledgement,
    PendingRepliesQuery,
    PostsQuery,
    RepliesPage,
    RepliesResponse,
    ReplyApprovalStatus,
    ReplyModerationAction,
    ReplyModerationResult,
)


async def moderate_reply(
    access_token: str, reply_id: str, operation: ReplyModerationAction
) -> ReplyModerationResult:
    """Set visibility or review a pending reply once, requiring a true acknowledgement."""
    visibility = operation in {"hide", "unhide"}
    edge = "manage_reply" if visibility else "manage_pending_reply"
    fields = (
        {"hide": "true" if operation == "hide" else "false"}
        if visibility
        else {"approve": "true" if operation == "approve" else "false"}
    )
    recovery = (
        "Check hide_status with threads_list_replies on the parent post."
        if visibility
        else "Check threads_list_pending_replies and published replies on the parent post. "
        "Absence from one page does not prove approval."
    )
    uncertain = (
        f"Moderation outcome is unknown (reply_id={reply_id}, operation={operation}). "
        "Do not retry automatically. " + recovery
    )
    data, http_status = await http_client.publication_request(
        f"{reply_id}/{edge}",
        access_token,
        fields,
        method="POST",
        stage="moderate",
        permission_message="Reply moderation requires threads_manage_replies and access to the own post.",
        uncertain_message=uncertain,
    )
    try:
        result = ActionAcknowledgement.model_validate(data)
    except ValidationError:
        record_publication(
            "moderate", "invalid_schema", http_status=http_status, payload=data
        )
        raise ThreadsError(uncertain) from None
    if not result.success:
        record_publication(
            "moderate", "invalid_schema", http_status=http_status, payload=data
        )
        raise ThreadsError(uncertain)
    record_publication("moderate", "confirmed")
    return ReplyModerationResult(success=True, reply_id=reply_id, operation=operation)


async def fetch_replies(
    access_token: str, post_id: str, query: PostsQuery
) -> RepliesPage:
    """Read one page of direct replies; requires threads_read_replies."""
    return await _fetch_reply_page(access_token, post_id, query, edge="replies")


async def fetch_conversation(
    access_token: str, post_id: str, query: PostsQuery
) -> RepliesPage:
    """Read one page of replies at every depth under a root Threads post."""
    return await _fetch_reply_page(access_token, post_id, query, edge="conversation")


async def _fetch_reply_page(
    access_token: str,
    post_id: str,
    query: PostsQuery,
    *,
    edge: Literal["replies", "conversation", "pending_replies"],
    approval_status: ReplyApprovalStatus | None = None,
) -> RepliesPage:
    """Share reply parsing and pagination without following Meta's next URL."""
    params: dict[str, str | int] = {
        "fields": ",".join(
            PENDING_REPLY_FIELDS if edge == "pending_replies" else REPLY_FIELDS
        ),
        "limit": query.limit,
        "reverse": "true",
    }
    if query.after is not None:
        params["after"] = query.after
    if approval_status is not None:
        params["approval_status"] = approval_status
    data = await http_client.read_request(
        f"{post_id}/{edge}",
        access_token,
        params,
        invalid_request_message=(
            "The conversation is unavailable. Use the root post_id from "
            "threads_list_posts and check the selected account. "
            "Start again without after if using a page cursor."
            if edge == "conversation"
            else "The post or reply page is unavailable. Check the selected account "
            "and post_id. Start again without after if using a page cursor."
        ),
        permission_message=(
            "Replies are unavailable to this account. Check access to the post "
            "and the threads_read_replies permission."
        ),
    )
    try:
        response = RepliesResponse.model_validate(data)
    except ValidationError:
        raise ThreadsError("Threads returned an unexpected replies format.") from None
    cursor = None
    if response.paging is not None and response.paging.next_url:
        cursor = response.paging.cursors.after
        if cursor is None or cursor == query.after:
            raise ThreadsError("Threads returned invalid reply pagination.")
    return RepliesPage(items=response.data, next_cursor=cursor)


async def fetch_pending_replies(
    access_token: str, post_id: str, query: PendingRepliesQuery
) -> RepliesPage:
    """Read pending and ignored replies, preserving their approval state."""
    return await _fetch_reply_page(
        access_token,
        post_id,
        query,
        edge="pending_replies",
        approval_status=query.approval_status,
    )
