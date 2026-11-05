"""Local HTTP server lifecycle without intercepting MCP signals."""

import asyncio
import socket
from collections.abc import Callable
from contextlib import contextmanager

import uvicorn
from starlette.types import ASGIApp


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


class LocalHTTPServer:
    def __init__(self, build_app: Callable[[str], ASGIApp]) -> None:
        self._build_app = build_app
        self._socket: socket.socket | None = None
        self._server: _Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._used = False
        self.origin: str
        self.app: ASGIApp

    async def start(self) -> None:
        if self._used:
            raise RuntimeError("The local server cannot be started twice")
        self._used = True
        sock = socket.socket()
        self._socket = sock
        try:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            self.origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            self.app = self._build_app(self.origin)
            server = _Server(
                uvicorn.Config(
                    self.app,
                    log_config=None,
                    log_level="warning",
                    access_log=False,
                    lifespan="off",
                    proxy_headers=False,
                    server_header=False,
                    ws="none",
                    timeout_graceful_shutdown=5,
                )
            )
            self._server = server
            self._task = asyncio.create_task(server.serve(sockets=[sock]))
            await self._wait_started()
        except BaseException:
            await self.close()
            raise

    async def _wait_started(self) -> None:
        assert self._server is not None and self._task is not None
        async with asyncio.timeout(5):
            while not self._server.started:
                if self._task.done():
                    await self._task
                    raise RuntimeError("Could not start the connection page")
                await asyncio.sleep(0.01)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

    async def wait_closed(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def close(self) -> None:
        self.stop()
        try:
            await self.wait_closed()
        finally:
            if self._socket is not None:
                self._socket.close()
