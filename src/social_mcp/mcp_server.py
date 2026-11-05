"""MCP boundary: concise argument errors without exposing submitted values."""

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult
from pydantic import ValidationError


class SocialMCPServer(MCPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._argument_hints: dict[str, dict[str, str]] = {}

    def register_argument_hints(self, hints: dict[str, dict[str, str]]) -> None:
        """Register platform-owned hints, isolated to this server instance."""
        self._argument_hints.update(
            {name: fields.copy() for name, fields in hints.items()}
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context | None = None,
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as error:
            if isinstance(error, UnexpectedToolError) or not isinstance(
                error.__cause__, ValidationError
            ):
                raise
            issues = error.__cause__.errors(
                include_input=False, include_context=False, include_url=False
            )
            fields = {issue["loc"][0] for issue in issues if issue["loc"]}
            tool_hints = self._argument_hints.get(name, {})
            hints = [hint for field, hint in tool_hints.items() if field in fields]
            raise ToolError(
                " ".join(hints) or "Invalid tool arguments. Check the tool schema."
            ) from None
