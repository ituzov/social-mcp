"""Threads API profile operations."""

from pydantic import ValidationError

from social_mcp.threads.client import http as http_client
from social_mcp.threads.client.fields import PROFILE_FIELDS
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import (
    ThreadsProfile,
)


async def fetch_profile(access_token: str) -> ThreadsProfile:
    """Fetch the token owner profile without exposing secrets in URLs or errors."""
    data = await http_client.read_request(
        "me", access_token, {"fields": ",".join(PROFILE_FIELDS)}
    )
    try:
        return ThreadsProfile.model_validate(data)
    except ValidationError:
        raise ThreadsError("Threads returned an unexpected profile format.") from None
