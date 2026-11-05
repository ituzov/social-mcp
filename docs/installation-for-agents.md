# Social MCP installation instructions for agents

[English](installation-for-agents.md) · [Русский](installation-for-agents.ru.md)

[Quick start](../README.md#quick-start)

If the user has asked you to install Social MCP, follow the steps below. These
instructions cover installing and connecting the released package. Repository
contribution rules are in [AGENTS.md](../AGENTS.md).

1. **Identify the target client.** Use the client and installation scope from the
   user's request or existing configuration. Ask only if they cannot be determined.
   Inspect the existing MCP configuration and preserve unrelated settings.
2. **Verify the release.** Use the release tag in [Quick start](../README.md#quick-start), or the version the
   user requested, from `ituzov/social-mcp`. Check that the tag is published before
   running installation commands. If it is missing, report the unavailable release;
   do not silently switch to `main` or invent a working download URL.
3. **Check prerequisites.** Run `uvx --version` and `git --version`. Install only
   missing prerequisites using the [official uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
   and the platform's Git installation method, within the user's authorized scope.
   Use an absolute path to `uvx` if the MCP client cannot resolve it. A project clone,
   manual virtual environment, and separately installed Python are not required.
4. **Prepare the package.** Use the pinned launch command from [Quick start](../README.md#quick-start) with
   `--help` appended. Let the first dependency installation finish and check for
   a successful exit. This checks the launcher, not the MCP connection.
5. **Register the server.** Use the matching client command or JSON configuration
   from [Quick start](../README.md#quick-start). Register the launch command **without `--help`**, with stdio
   transport. If a `social-mcp` entry already exists, compare it first: leave an
   identical entry unchanged and update only the requested fields if needed.
   Do not add a duplicate or overwrite other servers.
6. **Verify the MCP connection.** Reconnect or reload MCP using the client's
   supported mechanism. Confirm that the client discovers `threads_connect`,
   `threads_list_accounts`, and `threads_get_profile`, then call
   `threads_list_accounts`. An empty list is a successful initial setup. If this
   session cannot reload MCP or call its tools, give the exact reconnect step and
   report verification as pending rather than claiming a verified installation.
7. **Connect an account when requested.** Call `threads_connect`, show the returned
   local URL, and let the user enter the token in their browser. Call
   `threads_connect` again to check the result. Once connected, use the returned
   account ID to call `threads_get_profile`. Never request a token in chat or put
   it in the MCP configuration. Do not publish a test post.
8. **Report the result.** State the installed release tag, target client and scope,
   configuration location, and checks actually completed. Distinguish server
   installation from account connection; report any blocked or pending step.

The configured launcher must start without interactive installation or credential
prompts. Account connection happens separately through the tool and local form.
