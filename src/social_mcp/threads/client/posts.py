"""Threads API posts operations."""

import json

from pydantic import ValidationError

from social_mcp.threads.client import http as http_client
from social_mcp.threads.client.fields import POST_FIELDS
from social_mcp.threads.diagnostics import record_publication
from social_mcp.threads.errors import ThreadsError, ThreadsTransientError
from social_mcp.threads.schemas import (
    ContainerStatus,
    DeletedPost,
    ImageInput,
    LongTextPostInput,
    MediaCreationResponse,
    PostsPage,
    PostsQuery,
    PostsResponse,
    ReplyControl,
)


def publication_unknown(container_id: str, *, reply_to_id: str | None = None) -> str:
    """Give recovery instructions without claiming the write failed."""
    recovery = (
        f"threads_list_replies under reply_to_id={reply_to_id}"
        if reply_to_id is not None
        else "threads_list_posts"
    )
    return (
        f"Publication outcome is unknown (container_id={container_id}). "
        "Do not retry publishing or create another post. First check "
        f"{recovery} for the selected account to avoid duplicates."
    )


async def create_text_container(
    access_token: str,
    account_id: str,
    text: str,
    *,
    reply_to_id: str | None = None,
    reply_control: ReplyControl | None = None,
    enable_reply_approvals: bool = False,
) -> str:
    """Create a text container without publishing it."""
    fields = {"media_type": "TEXT", "text": text, "auto_publish_text": "false"}
    if reply_to_id is not None:
        fields["reply_to_id"] = reply_to_id
    if reply_control is not None:
        fields["reply_control"] = reply_control
    if enable_reply_approvals:
        fields["enable_reply_approvals"] = "true"
    return await _create_media_container(access_token, account_id, fields)


async def create_long_text_container(
    access_token: str, account_id: str, post: LongTextPostInput
) -> str:
    """Send an exact JSON text attachment inside the form body, never in the URL."""
    attachment = {"plaintext": post.attachment_text}
    if post.link_url is not None:
        attachment["link_attachment_url"] = post.link_url
    fields = {
        "media_type": "TEXT",
        "text": post.text,
        "auto_publish_text": "false",
        "text_attachment": json.dumps(attachment, ensure_ascii=False),
    }
    if post.reply_to_id is not None:
        fields["reply_to_id"] = post.reply_to_id
    if post.reply_control is not None:
        fields["reply_control"] = post.reply_control
    if post.enable_reply_approvals:
        fields["enable_reply_approvals"] = "true"
    return await _create_media_container(access_token, account_id, fields)


async def create_image_container(
    access_token: str,
    account_id: str,
    image: ImageInput,
    *,
    text: str | None = None,
    is_carousel_item: bool = False,
) -> str:
    """Create an image container; Meta fetches the original URL from the POST body."""
    fields = {"media_type": "IMAGE", "image_url": image.image_url}
    if image.alt_text is not None:
        fields["alt_text"] = image.alt_text
    if text is not None:
        fields["text"] = text
    if is_carousel_item:
        fields["is_carousel_item"] = "true"
    return await _create_media_container(access_token, account_id, fields)


async def create_quote_container(
    access_token: str, account_id: str, text: str, quote_post_id: str
) -> str:
    """Create a text quote container without publishing automatically."""
    return await _create_media_container(
        access_token,
        account_id,
        {
            "media_type": "TEXT",
            "text": text,
            "quote_post_id": quote_post_id,
            "auto_publish_text": "false",
        },
    )


async def repost_post(access_token: str, post_id: str) -> str:
    """Repost once; an ambiguous response does not authorize a retry."""
    uncertain = (
        f"Repost outcome is unknown (original_post_id={post_id}). Do not retry. "
        "First check threads_list_posts for REPOST_FACADE with a matching "
        "reposted_post.id, or inspect the account's Reposts tab in Threads."
    )
    data, http_status = await http_client.publication_request(
        f"{post_id}/repost",
        access_token,
        {},
        method="POST",
        stage="repost",
        uncertain_message=uncertain,
    )
    try:
        result = MediaCreationResponse.model_validate(data)
    except ValidationError:
        record_publication(
            "repost", "invalid_schema", http_status=http_status, payload=data
        )
        raise ThreadsError(uncertain) from None
    record_publication("repost", "confirmed")
    return result.id


async def delete_post(access_token: str, post_id: str) -> DeletedPost:
    """Confirm deletion only when Meta acknowledges the exact requested post."""
    uncertain = (
        f"Deletion outcome is unknown (post_id={post_id}). Do not retry automatically. "
        "Check the selected account and post in Threads before another attempt. "
        "Absence from one page of threads_list_posts is not proof of deletion."
    )
    data, http_status = await http_client.publication_request(
        post_id,
        access_token,
        {},
        method="DELETE",
        stage="delete",
        permission_message="Deleting requires threads_delete and ownership of the selected post.",
        uncertain_message=uncertain,
    )
    try:
        result = DeletedPost.model_validate(data)
    except ValidationError:
        record_publication(
            "delete", "invalid_schema", http_status=http_status, payload=data
        )
        raise ThreadsError(uncertain) from None
    if not result.success or result.deleted_id != post_id:
        record_publication(
            "delete", "invalid_schema", http_status=http_status, payload=data
        )
        raise ThreadsError(uncertain)
    record_publication("delete", "confirmed")
    return result


async def create_carousel_container(
    access_token: str,
    account_id: str,
    children: list[str],
    *,
    text: str | None = None,
) -> str:
    """Create the parent container with child IDs in the approved order."""
    fields = {"media_type": "CAROUSEL", "children": ",".join(children)}
    if text is not None:
        fields["text"] = text
    return await _create_media_container(access_token, account_id, fields)


async def _create_media_container(
    access_token: str, account_id: str, fields: dict[str, str]
) -> str:
    """Share safe creation response validation across text, images, and carousels."""
    data, http_status = await http_client.publication_request(
        f"{account_id}/threads",
        access_token,
        fields,
        method="POST",
        stage="create",
    )
    try:
        return MediaCreationResponse.model_validate(data).id
    except ValidationError:
        record_publication(
            "create", "invalid_schema", http_status=http_status, payload=data
        )
        raise ThreadsError(
            "Threads returned an unexpected container format. No publication was attempted."
        ) from None


async def fetch_container_status(
    access_token: str, container_id: str
) -> ContainerStatus:
    data, http_status = await http_client.publication_request(
        container_id, access_token, {"fields": "status"}, method="GET", stage="status"
    )
    try:
        return ContainerStatus.model_validate(data)
    except ValidationError:
        record_publication(
            "status", "invalid_schema", http_status=http_status, payload=data
        )
        raise ThreadsError("Threads returned an unexpected container status.") from None


async def publish_container(
    access_token: str,
    account_id: str,
    container_id: str,
    *,
    reply_to_id: str | None = None,
) -> str:
    """Publish once; an ambiguous response must never prompt an automatic retry."""
    uncertain_message = publication_unknown(container_id, reply_to_id=reply_to_id)
    data, http_status = await http_client.publication_request(
        f"{account_id}/threads_publish",
        access_token,
        {"creation_id": container_id},
        method="POST",
        stage="publish",
        uncertain_message=uncertain_message,
    )
    try:
        return MediaCreationResponse.model_validate(data).id
    except ValidationError:
        record_publication(
            "publish", "invalid_schema", http_status=http_status, payload=data
        )
        raise ThreadsError(uncertain_message) from None


async def fetch_posts(
    access_token: str, account_id: str, query: PostsQuery
) -> PostsPage:
    params: dict[str, str | int] = {
        "fields": ",".join(POST_FIELDS),
        "limit": query.limit,
    }
    if query.after is not None:
        params["after"] = query.after
    cursor_hint = "Start again without after, then use the new next_cursor."
    try:
        data = await http_client.read_request(
            f"{account_id}/threads",
            access_token,
            params,
            invalid_request_message=(
                f"Threads rejected the page cursor. {cursor_hint}"
                if query.after is not None
                else None
            ),
        )
    except ThreadsTransientError as error:
        if query.after is None:
            raise
        raise ThreadsTransientError(
            f"{error.user_message} If the error persists with this cursor: {cursor_hint}"
        ) from None
    try:
        response = PostsResponse.model_validate(data)
    except ValidationError:
        raise ThreadsError("Threads returned an unexpected posts format.") from None
    cursor = None
    if response.paging is not None and response.paging.next_url:
        cursor = response.paging.cursors.after
        if cursor is None or cursor == query.after:
            raise ThreadsError("Threads returned invalid post pagination.")
    return PostsPage(items=response.data, next_cursor=cursor)
