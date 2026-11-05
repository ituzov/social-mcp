"""Form HTTP boundary: request checks, field parsing, and safe responses."""

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from social_mcp.threads.connection.localization import (
    CATALOGS,
    LANGUAGE_COOKIE,
    choose_language,
    translate,
)
from social_mcp.threads.connection.views import connected_page, form_page, message_page
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ThreadsProfile

MAX_BODY = 8192


def request_language(request: Request) -> str:
    return choose_language(
        request.cookies.get(LANGUAGE_COOKIE), request.headers.get("accept-language", "")
    )


def request_text(request: Request, key: str) -> str:
    return translate(request_language(request), key)


class ConnectionRequestGuard(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, origin: str, path: str) -> None:
        super().__init__(app)
        self._origin = origin
        self._host = urlsplit(origin).netloc
        self._path = path

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.headers.get("host") != self._host:
            return message_page(
                request_text(request, "invalid_host"),
                403,
                language=request_language(request),
            )
        if request.url.query:
            return message_page(
                request_text(request, "invalid_query"),
                400,
                language=request_language(request),
            )
        if request.method == "POST" and request.headers.get("origin") != self._origin:
            return message_page(
                request_text(request, "invalid_origin"),
                403,
                language=request_language(request),
            )
        try:
            response = await call_next(request)
            if response.status_code == 413:
                return message_page(
                    request_text(request, "too_large"),
                    413,
                    language=request_language(request),
                )
            return response
        except Exception:
            return form_page(
                self._path,
                request_text(request, "connection_failed"),
                500,
                language=request_language(request),
            )


async def read_token_form(request: Request) -> str:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(415)
    async with asyncio.timeout(10):
        async with request.form(max_fields=1, max_files=0, max_part_size=4096) as form:
            tokens = form.getlist("token")
            if set(form) != {"token"} or len(tokens) != 1:
                raise HTTPException(400)
            raw_token = tokens[0]
            if not isinstance(raw_token, str):
                raise HTTPException(400)
            token = raw_token.strip()
    if not token or len(token) > 4096:
        raise HTTPException(400)
    return token


class ConnectionEndpoints:
    def __init__(
        self,
        *,
        path: str,
        origin: str,
        connect: Callable[[str], Awaitable[ThreadsProfile]],
        closed: Callable[[], bool],
    ) -> None:
        self._path = path
        self._connect = connect
        self._closed = closed
        self.app = Starlette(
            routes=[
                Route(path, self.show, methods=["GET"]),
                Route(path, self.submit, methods=["POST"]),
                Route(f"{path}/language", self.change_language, methods=["POST"]),
            ],
            middleware=[
                Middleware(ConnectionRequestGuard, origin=origin, path=path),
                Middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY),
            ],
            exception_handlers={
                HTTPException: self.http_error,
                ThreadsError: self.connection_error,
                ClientDisconnect: self.invalid_form,
                TimeoutError: self.invalid_form,
                ValueError: self.invalid_form,
            },
        )

    async def show(self, request: Request) -> Response:
        if self._closed():
            raise HTTPException(410)
        return form_page(self._path, language=request_language(request))

    async def submit(self, request: Request) -> Response:
        token = await read_token_form(request)
        profile = await self._connect(token)
        return connected_page(profile, language=request_language(request))

    async def http_error(self, request: Request, error: HTTPException) -> Response:
        if error.status_code == 400:
            return form_page(
                self._path,
                request_text(request, "invalid_token"),
                400,
                language=request_language(request),
            )
        messages = {
            404: "not_found",
            405: "method_not_allowed",
            410: "expired",
            413: "too_large",
            415: "unsupported_format",
        }
        return message_page(
            request_text(request, messages.get(error.status_code, "invalid_request")),
            error.status_code,
            language=request_language(request),
        )

    async def invalid_form(self, request: Request, _error: Exception) -> Response:
        return form_page(
            self._path,
            request_text(request, "unreadable_form"),
            400,
            language=request_language(request),
        )

    async def connection_error(
        self, request: Request, _error: ThreadsError
    ) -> Response:
        return form_page(
            self._path,
            request_text(request, "invalid_connection"),
            400,
            language=request_language(request),
        )

    async def change_language(self, request: Request) -> Response:
        """Remember a supported language without submitting the token form."""
        if self._closed():
            raise HTTPException(410)
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415)
        async with asyncio.timeout(10):
            async with request.form(
                max_fields=1, max_files=0, max_part_size=32
            ) as form:
                choices = form.getlist("language")
                if set(form) != {"language"} or len(choices) != 1:
                    raise HTTPException(400)
                language = choices[0]
                if not isinstance(language, str) or language not in CATALOGS:
                    raise HTTPException(400)
        response = RedirectResponse(
            self._path, status_code=303, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            LANGUAGE_COOKIE,
            language,
            max_age=31536000,
            httponly=True,
            samesite="strict",
        )
        return response
