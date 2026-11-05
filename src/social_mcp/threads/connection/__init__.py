"""Public interface for local Threads connections."""

from social_mcp.threads.connection.manager import (
    ConnectionAction,
    ConnectionStatus,
    ThreadsConnection,
)
from social_mcp.threads.connection.page import ConnectionPage

__all__ = [
    "ConnectionAction",
    "ConnectionPage",
    "ConnectionStatus",
    "ThreadsConnection",
]
