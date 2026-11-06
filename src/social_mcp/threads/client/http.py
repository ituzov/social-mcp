"""Shared HTTP requests, error translation, and safe diagnostic metadata."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import Literal

import httpx

from social_mcp.threads.diagnostics import PublicationStage, record_publication
from social_mcp.threads.errors import ThreadsError, ThreadsTransientError
from social_mcp.threads.transport import RedactingTransport

# Lifespan scope propagates to request tasks, without sharing clients across event loops.
_clients: ContextVar[tuple[httpx.AsyncClient, httpx.AsyncClient] | None] = ContextVar(
    "threads_http_clients", default=None
)


@asynccontextmanager
async def client_lifespan() -> AsyncIterator[None]:
    async with AsyncExitStack() as stack:
        regular = await stack.enter_async_context(
            httpx.AsyncClient(timeout=10.0, follow_redirects=False)
        )
        redacted = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=RedactingTransport(httpx.AsyncHTTPTransport()),
                timeout=10.0,
                follow_redirects=False,
            )
        )
        scope = _clients.set((regular, redacted))
        try:
            yield
        finally:
            _clients.reset(scope)


@asynccontextmanager
async def _client(*, query_secrets: bool = False) -> AsyncIterator[httpx.AsyncClient]:
    clients = _clients.get()
    if clients is None:
        # Standalone client calls own a short scope; the application owns a long one.
        transport = (
            RedactingTransport(httpx.AsyncHTTPTransport()) if query_secrets else None
        )
        async with httpx.AsyncClient(
            transport=transport, timeout=10.0, follow_redirects=False
        ) as client:
            yield client
    else:
        yield clients[1 if query_secrets else 0]


AUTH_ERROR = "Threads rejected the token. Check the account connection."
PERMISSION_ERROR = (
    "Insufficient Threads permissions for this action. Check the token permissions."
)
RATE_LIMIT_ERROR = "Threads rate limit reached. Try again later."
META_ERRORS = {
    4: RATE_LIMIT_ERROR,
    17: RATE_LIMIT_ERROR,
    32: RATE_LIMIT_ERROR,
    613: RATE_LIMIT_ERROR,
    102: AUTH_ERROR,
    190: AUTH_ERROR,
    10: PERMISSION_ERROR,
    200: PERMISSION_ERROR,
}
HTTP_ERRORS = {401: AUTH_ERROR, 403: PERMISSION_ERROR, 429: RATE_LIMIT_ERROR}
PUBLISH_PERMISSION_ERROR = (
    "Publishing is unavailable to this account. "
    "Check threads_content_publish and, for replies, "
    "threads_manage_replies and access to the parent post."
)


async def publication_request(
    path: str,
    access_token: str,
    fields: dict[str, str],
    *,
    method: Literal["GET", "POST", "DELETE"],
    stage: PublicationStage,
    permission_message: str = PUBLISH_PERMISSION_ERROR,
    uncertain_message: str | None = None,
) -> tuple[object, int]:
    """Send one publishing request and retain only safe diagnostic metadata."""
    record_publication(stage, "request_started")
    try:
        async with _client() as client:
            url = f"https://graph.threads.com/v1.0/{path}"
            headers = {"Authorization": f"Bearer {access_token}"}
            if method == "GET":
                response = await client.send(
                    httpx.Request("GET", url, headers=headers, params=fields)
                )
            elif method == "DELETE":
                response = await client.send(
                    httpx.Request("DELETE", url, headers=headers)
                )
            else:
                response = await client.send(
                    httpx.Request("POST", url, headers=headers, data=fields)
                )
    except asyncio.CancelledError:
        record_publication(stage, "cancelled")
        raise
    except httpx.TimeoutException:
        record_publication(stage, "timeout")
        if method == "GET":
            raise ThreadsTransientError(
                "Threads did not respond in time. Try again later."
            ) from None
        raise ThreadsError(
            uncertain_message
            or "Could not confirm container creation. No publication was attempted."
        ) from None
    except httpx.RequestError:
        record_publication(stage, "network_error")
        if method == "GET":
            raise ThreadsTransientError("Could not reach the Threads API.") from None
        raise ThreadsError(
            uncertain_message
            or "Could not confirm container creation. No publication was attempted."
        ) from None
    try:
        data = response.json()
    except ValueError:
        record_publication(stage, "invalid_json", http_status=response.status_code)
        data = None
    record_publication(
        stage, "response_received", http_status=response.status_code, payload=data
    )
    try:
        _check_api_error(
            response,
            data,
            permission_message=permission_message,
        )
    except ThreadsError as error:
        record_publication(
            stage, "api_error", http_status=response.status_code, payload=data
        )
        if uncertain_message is not None and isinstance(error, ThreadsTransientError):
            raise ThreadsError(uncertain_message) from None
        raise
    return data, response.status_code


def _check_api_error(
    response: httpx.Response,
    data: object,
    *,
    invalid_request_message: str | None = None,
    permission_message: str = PERMISSION_ERROR,
) -> None:
    error = data.get("error") if isinstance(data, dict) else None
    if response.is_success and error is None:
        return
    code = error.get("code") if isinstance(error, dict) else None
    if code == 100 and isinstance(error, dict):
        detail = error.get("message")
        if isinstance(detail, str) and "reply approvals must be enabled" in " ".join(
            detail.casefold().split()
        ):
            raise ThreadsError(
                "Reply approvals are not enabled for this post. "
                "Choose a post with reply approvals enabled. For a new, explicitly "
                "approved publication, set enable_reply_approvals=true. "
                "Retrying the unchanged request will not resolve this condition."
            )
    message = META_ERRORS.get(code) if isinstance(code, int) else None
    message = message or HTTP_ERRORS.get(response.status_code)
    if message == PERMISSION_ERROR:
        message = permission_message
    transient = (
        code in (1, 2, 4, 17, 32, 613)
        or response.status_code == 429
        or response.status_code >= 500
    )
    if message is None:
        if transient:
            message = f"Temporary Threads API failure (HTTP {response.status_code}). Try again later."
        elif invalid_request_message and (code == 100 or response.status_code == 404):
            message = invalid_request_message
        else:
            message = (
                f"Threads rejected the request (HTTP {response.status_code}). "
                "Check the request parameters and token permissions."
            )
    error_type = ThreadsTransientError if transient else ThreadsError
    raise error_type(message)


async def read_request(
    path: str,
    access_token: str,
    params: dict[str, str | int],
    *,
    invalid_request_message: str | None = None,
    permission_message: str = PERMISSION_ERROR,
) -> object:
    try:
        async with _client() as client:
            response = await client.send(
                httpx.Request(
                    "GET",
                    f"https://graph.threads.com/v1.0/{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
            )
    except httpx.TimeoutException:
        raise ThreadsTransientError(
            "Threads did not respond in time. Try again later."
        ) from None
    except httpx.RequestError:
        raise ThreadsTransientError("Could not reach the Threads API.") from None

    try:
        data = response.json()
    except ValueError:
        data = None
    _check_api_error(
        response,
        data,
        invalid_request_message=invalid_request_message,
        permission_message=permission_message,
    )
    return data


async def token_request(path: str, params: dict[str, str]) -> object:
    try:
        async with _client(query_secrets=True) as client:
            response = await client.send(
                httpx.Request("GET", f"https://graph.threads.com/{path}", params=params)
            )
    except httpx.TimeoutException:
        raise ThreadsTransientError(
            "Threads did not respond in time while checking the token."
        ) from None
    except httpx.RequestError:
        raise ThreadsTransientError(
            "Could not reach Threads while checking the token."
        ) from None
    try:
        data = response.json()
    except ValueError:
        data = None
    _check_api_error(response, data)
    return data
