"""Explicit Meta request fields, independent of response schemas."""

PROFILE_FIELDS = (
    "id",
    "username",
    "name",
    "threads_biography",
    "threads_profile_picture_url",
    "is_verified",
)

POST_FIELDS = (
    "id",
    "text",
    "timestamp",
    "media_type",
    "permalink",
    "is_quote_post",
    "quoted_post",
    "reposted_post",
    "text_attachment",
)

REPLY_FIELDS = (
    "id",
    "text",
    "timestamp",
    "media_type",
    "permalink",
    "is_quote_post",
    "quoted_post",
    "reposted_post",
    "username",
    "has_replies",
    "is_reply_owned_by_me",
    "hide_status",
    "root_post",
    "replied_to",
)

POST_INSIGHT_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")

PENDING_REPLY_FIELDS = (*REPLY_FIELDS, "reply_approval_status")
