"""MCP boundary: concise argument errors without exposing submitted values."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent, ToolAnnotations
from pydantic import Field, ValidationError

from social_mcp.updates import UpdateChecker, UpdateStatus


class SocialMCPServer(MCPServer):
    def __init__(
        self, *args: Any, update_checker: UpdateChecker | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._argument_hints: dict[str, dict[str, str]] = {}
        self._update_checker = update_checker
        self._notified_version: str | None = None
        if update_checker is not None:
            self._register_update_tool(update_checker)

    def _register_update_tool(self, checker: UpdateChecker) -> None:
        @self.tool(annotations=ToolAnnotations(read_only_hint=True))
        async def social_mcp_check_update(
            force: Annotated[bool, Field(strict=True)] = True,
        ) -> UpdateStatus:
            """Check the installed Social MCP version against GitHub's stable release.

            force=true checks now; false uses the six-hour process cache.
            unavailable means the check failed, not that the installed version is latest.
            disabled means SOCIAL_MCP_CHECK_UPDATES=false; no request is sent.
            Offer an update when available; change client configuration only when asked.
            Never reinstall or remove the account database when updating the package.
            """
            return await checker.check(force=force)

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
        update = None
        if self._update_checker is not None and name != "social_mcp_check_update":
            update = await self._update_checker.check()
        try:
            result = await super().call_tool(name, arguments, context)
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
        if (
            isinstance(result, CallToolResult)
            and not result.is_error
            and update is not None
            and update.update_available is True
            and update.latest_version != self._notified_version
        ):
            result.content.append(
                TextContent(
                    type="text",
                    text=(
                        f"Social MCP update available: {update.latest_version} "
                        f"(installed: {update.current_version}). {update.release_url} "
                        "Tell the user and offer an update. Update the MCP client's "
                        "pinned release tag only if requested; preserve the account database."
                    ),
                )
            )
            self._notified_version = update.latest_version
        return result
