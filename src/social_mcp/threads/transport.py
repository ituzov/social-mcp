"""Redact query secrets after sending the request and before HTTPX logging."""

import httpx

SENSITIVE_PARAMS = ("access_token", "input_token", "client_secret")


class RedactingTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._transport.handle_async_request(request)
        finally:
            for name in SENSITIVE_PARAMS:
                if name in request.url.params:
                    request.url = request.url.copy_set_param(name, "REDACTED")

    async def aclose(self) -> None:
        await self._transport.aclose()
