"""Public interface of the Threads module."""

from social_mcp.threads.connection import ThreadsConnection
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.service import ThreadsService
from social_mcp.threads.tools import register_threads_tools

__all__ = [
    "ThreadsConnection",
    "ThreadsError",
    "ThreadsService",
    "register_threads_tools",
]
