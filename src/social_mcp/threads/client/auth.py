"""Threads API auth operations."""

from pydantic import ValidationError

from social_mcp.threads.client import http as http_client
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import (
    RefreshedToken,
    TokenInfo,
)


async def inspect_token(access_token: str) -> TokenInfo:
    data = await http_client.token_request(
        "v1.0/debug_token",
        {
            "access_token": access_token,
            "input_token": access_token,
        },
    )
    try:
        return TokenInfo.model_validate(
            data.get("data") if isinstance(data, dict) else None
        )
    except ValidationError:
        raise ThreadsError("Threads returned unexpected token data.") from None


async def refresh_token(access_token: str) -> RefreshedToken:
    data = await http_client.token_request(
        "refresh_access_token",
        {
            "grant_type": "th_refresh_token",
            "access_token": access_token,
        },
    )
    try:
        return RefreshedToken.model_validate(data)
    except ValidationError:
        raise ThreadsError(
            "Threads returned an unexpected token refresh response."
        ) from None
