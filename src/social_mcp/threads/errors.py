"""User-facing Threads errors independent of MCP."""


class ThreadsError(Exception):
    """A safe message without the token or raw Meta response."""

    @property
    def user_message(self) -> str:
        return str(self)


class ThreadsTransientError(ThreadsError):
    """A temporary network, server, or rate-limit failure."""
