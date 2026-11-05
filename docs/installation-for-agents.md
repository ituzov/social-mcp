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
7. **Guide account setup when requested.** Follow [Guided Threads setup](#guided-threads-setup).
   Check what the user already has before assuming they can paste a token.
   Continue through account verification; do not stop at an unexplained form URL.
8. **Report the result.** State the installed release tag, target client and scope,
   configuration location, and checks actually completed. Distinguish server
   installation from account connection; report any blocked or pending step.

The configured launcher must start without interactive installation or credential
prompts. Account connection happens separately through the tool and local form.

## Guided Threads setup

Use this flow when the user wants to connect an account and no suitable account
is connected. An installed server and a connected Threads account are separate
outcomes.

For Meta login and app setup, recommend the user's regular browser, where they
may already be signed in. If an embedded preview shows a login screen or a
page-unavailable error, suggest opening the same URL externally in Chrome or
Safari, using the client's "Open in" menu or copying the URL. Do not infer that
the app or account is missing from a preview error alone. Let the user complete
login privately; do not copy cookies or credentials between browsers.

1. **Find the starting point.** If the conversation does not already establish it,
   ask: “Do you already have a Threads access token, a Meta developer app, or are
   you starting from scratch?” Skip any steps the user has completed. With a ready
   token, go directly to the local form below.
2. **Developer access.** If needed, link [Meta for Developers](https://developers.facebook.com/)
   and guide the user through Meta's current developer registration screens.
   Let the user complete login, identity checks, and verification privately.
   If Meta blocks registration, explain the actual blocker and pause that step;
   do not promise that registration always succeeds or invent a workaround.
3. **Threads app.** Link the [app dashboard](https://developers.facebook.com/apps/).
   Help the user create an app with the Threads API use case, or use their existing
   Threads app. Guide one step at a time, confirm progress, and adapt to the actual
   screen rather than assuming button names or repeating completed setup.
4. **Tester and token.** Help add the target Threads account as a Threads Tester
   and accept the invitation in [Threads settings](https://www.threads.com/settings/account).
   Then guide the user to the app's user token generator. Request only permissions
   needed for the intended features; see the [permission table](../README.md#get-a-threads-token).
   App ID, App Secret, an OAuth callback, and a tunnel are not required by Social
   MCP's token-import form. If the dashboard differs, consult the current
   [Threads documentation](https://developers.facebook.com/docs/threads/) and ask
   for a description of the screen without secrets.
5. **Import privately.** Call `threads_connect` and show the returned local URL;
   its page also contains setup instructions. Ask the user to paste the token
   only there, on the machine running MCP. Never request tokens, passwords,
   verification codes, or App Secret in chat or put them in MCP configuration.
6. **Confirm the result.** Once the user submits the form, call `threads_connect`
   again. If still pending, help with the current step instead of claiming success.
   When connected, call `threads_get_profile` with the returned account ID and
   report the verified username. Read profile access does not prove permission
   for publishing or insights. Do not publish a test post.

## Updating an existing installation

Update only when the user requests it. A version notice is an invitation to
explain the available release, not permission to modify configuration.

1. Inspect the target client's pinned tag, scope, environment, and database path.
   Check the public [release list](https://github.com/ituzov/social-mcp/releases)
   or call `social_mcp_check_update`. Its default `force=true` bypasses the cache;
   `unavailable` means unknown and `disabled` means checks were opted out.
2. Verify the chosen stable release and prepare its launch command with `--help`.
   Preserve other settings and clients. Do not switch to `main` or move old tags.
3. Make a consistent SQLite backup, using SQLite's backup API if the database is
   open. Keep it private; never print or upload the database or its tokens.
   Change only the target client's pinned release tag. Do not reset the database
   or require account reconnection just to update the package.
4. Reload the MCP connection. Verify tool discovery and, when available, the
   installed version from `social_mcp_check_update`. A network failure does not
   invalidate the reported installed version. Report any verification pending.

The checker does not exist in `v0.1.0`; its first upgrade requires changing the
pinned tag directly. A different client's version remains unchanged even when
both installations share the default database.
