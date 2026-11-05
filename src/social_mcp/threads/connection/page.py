"""One local connection session: state, single use, and shutdown."""

import asyncio
import secrets

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp

from social_mcp.threads.connection.http import ConnectionEndpoints
from social_mcp.threads.connection.server import LocalHTTPServer
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import ThreadsProfile
from social_mcp.threads.service import ThreadsService


class ConnectionPage:
    """Use with async with; the link remains valid until connection or close()."""

    def __init__(self, service: ThreadsService) -> None:
        self._service = service
        self._path = f"/connect/{secrets.token_urlsafe(32)}"
        self._done = asyncio.Event()
        self._lock = asyncio.Lock()
        self._profile: ThreadsProfile | None = None
        self._server = LocalHTTPServer(self._build_app)

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    @property
    def url(self) -> str:
        return self._server.origin + self._path

    @property
    def app(self) -> ASGIApp:
        return self._server.app

    async def __aenter__(self):
        await self._server.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self) -> None:
        try:
            await self._server.close()
        finally:
            self._done.set()

    async def wait_closed(self) -> None:
        await self._server.wait_closed()

    async def wait(self) -> ThreadsProfile:
        await self._done.wait()
        if self._profile is None:
            raise ThreadsError("The connection page is closed. Start a new connection.")
        return self._profile

    def _build_app(self, origin: str) -> ASGIApp:
        return ConnectionEndpoints(
            path=self._path,
            origin=origin,
            connect=self._connect,
            closed=self._done.is_set,
        ).app

    async def _connect(self, token: str) -> ThreadsProfile:
        async with self._lock:
            if self._done.is_set():
                raise HTTPException(410)
            profile = await self._service.connect(token)
            self._profile = profile
            self._done.set()
            self._server.stop()
            return profile
