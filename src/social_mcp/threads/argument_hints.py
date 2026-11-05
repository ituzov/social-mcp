"""Tool-specific safe argument hints owned by Threads."""

ARGUMENT_HINTS = {
    "threads_moderate_reply": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "reply_id": "reply_id: provide a numeric ID from threads_list_replies or threads_list_pending_replies.",
        "operation": "operation: use hide, unhide, approve, or ignore.",
    },
    "threads_list_pending_replies": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "post_id": "post_id: provide a numeric post ID from threads_list_posts.",
        "limit": "limit: provide an integer from 1 to 100.",
        "after": "after: use next_cursor from the previous page or omit this parameter.",
        "approval_status": "approval_status: use pending or ignored, or omit it.",
    },
    "threads_publish_long_text": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "text": "text: provide non-blank text of at most 500 characters.",
        "attachment_text": "attachment_text: provide non-blank text of at most 10000 characters.",
        "link_url": "link_url: provide an HTTP(S) URL without whitespace or omit it.",
        "reply_to_id": "reply_to_id: provide a numeric ID from threads_list_posts or threads_list_replies.",
        "reply_control": "reply_control: use a supported value from the tool schema or omit it.",
        "enable_reply_approvals": "enable_reply_approvals: provide a boolean, true or false.",
    },
    "threads_quote_post": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "post_id": "post_id: provide the numeric ID of an accessible original Threads post.",
        "text": "text: provide non-blank text of at most 500 characters.",
    },
    "threads_repost": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "post_id": "post_id: provide the numeric ID of an accessible original Threads post.",
    },
    "threads_delete_post": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "post_id": "post_id: provide a numeric post ID from threads_list_posts.",
    },
    "threads_publish_image": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "image_url": "image_url: provide an HTTP(S) URL accessible to Meta, without whitespace.",
        "text": "text: provide a string of at most 500 characters or omit it.",
        "alt_text": "alt_text: provide a string of at most 1000 characters or omit it.",
    },
    "threads_publish_carousel": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "images": "images: provide 2 to 20 objects with image_url and optional alt_text.",
        "text": "text: provide a string of at most 500 characters or omit it.",
    },
    "threads_get_account_insights": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts."
    },
    "threads_get_conversation": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "post_id": "post_id: provide a numeric post ID from threads_list_posts.",
        "limit": "limit: provide an integer from 1 to 100.",
        "after": "after: use next_cursor from the previous page or omit this parameter.",
    },
    "threads_reply": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "reply_to_id": "reply_to_id: provide a numeric ID from threads_list_posts or threads_list_replies.",
        "text": "text: provide non-blank text of at most 500 characters.",
    },
    "threads_publish_text": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "text": "text: provide non-blank text of at most 500 characters.",
        "reply_control": "reply_control: use a supported value from the tool schema or omit it.",
        "enable_reply_approvals": "enable_reply_approvals: provide a boolean, true or false.",
    },
    "threads_list_replies": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "post_id": "post_id: provide a numeric post ID from threads_list_posts.",
        "limit": "limit: provide an integer from 1 to 100.",
        "after": "after: use next_cursor from the previous page or omit this parameter.",
    },
    "threads_get_post_insights": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "post_id": "post_id: provide a numeric post ID from threads_list_posts.",
    },
    "threads_list_posts": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts.",
        "limit": "limit: provide an integer from 1 to 100.",
        "after": "after: use next_cursor from the previous page or omit this parameter.",
    },
    "threads_list_accounts": {},
    "threads_connect": {"action": "action: use start, restart, or cancel."},
    "threads_get_profile": {
        "account_id": "account_id: provide a numeric ID from threads_list_accounts."
    },
}
