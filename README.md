# Social MCP

[English](README.md) · [Русский](README.ru.md)

Connect your Threads account to an MCP assistant: read posts and conversations,
inspect available insights, publish content, and moderate replies through the
Threads API. Runs locally over stdio. Instagram, X, and Telegram are planned,
not implemented.

**Runs on your computer. Stores your tokens locally. No Social MCP cloud.**

## Privacy and data flow

- Account records and access tokens stay in SQLite on the machine running Social
  MCP. There is no hosted Social MCP backend, cloud sync, or built-in telemetry.
- The server calls the Threads API directly. Tokens are sent to Meta to authorize
  requests; they are not included in MCP tool responses or sent to the AI model
  by Social MCP.
- Profile data, posts, replies, and insights returned by tools go to your MCP
  client. If you use a cloud AI model, the client may send that content to its
  provider according to its settings and policies. Social MCP does not make
  the AI client's processing local.
- Content you explicitly publish is sent to Meta for publication. The local
  database stores tokens as plaintext; see [Storage and limits](#storage-and-limits).

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if it is not
already available; it includes `uvx`. A coding agent can perform this setup for you.
For GitHub sources, ensure Git is available too. You do not need to clone the
project, create a virtual environment, or install Python separately: `uvx`
prepares an isolated environment and can download the requested Python version.
See [uv's tool guide](https://docs.astral.sh/uv/guides/tools/).

Repository: [ituzov/social-mcp](https://github.com/ituzov/social-mcp).

The commands below install release `v0.1.0`. Use the same release tag consistently
in every command. Published tags remain unchanged; fixes use a new tag, such as
`v0.1.1`.

The server's launch command is:

```bash
uvx --python 3.15.2 --from "git+https://github.com/ituzov/social-mcp.git@v0.1.0" social-mcp
```

The first launch downloads and prepares dependencies; allow it to finish. Later
launches reuse uv's cache. If your MCP client cannot find `uvx`, use its absolute
path. This project currently requires Python 3.15.2 or later.

Register this command with your MCP client. The client launches the stdio server
when it connects, so you do not need to run it in a separate terminal. The exact
connection timing and reconnect behavior depend on the client.

**Claude Code** ([MCP documentation](https://code.claude.com/docs/en/mcp)):

```bash
claude mcp add --transport stdio social-mcp -- uvx --python 3.15.2 --from "git+https://github.com/ituzov/social-mcp.git@v0.1.0" social-mcp
```

**Codex** ([MCP documentation](https://learn.chatgpt.com/docs/extend/mcp)):

```bash
codex mcp add social-mcp -- uvx --python 3.15.2 --from "git+https://github.com/ituzov/social-mcp.git@v0.1.0" social-mcp
```

For clients accepting an `mcpServers` JSON configuration:

```json
{
  "mcpServers": {
    "social-mcp": {
      "command": "uvx",
      "args": ["--python", "3.15.2", "--from", "git+https://github.com/ituzov/social-mcp.git@v0.1.0", "social-mcp"]
    }
  }
}
```

Reload the client's MCP connection if needed. Ask your assistant:

> Connect my Threads account.

The assistant calls `threads_connect` and gives you a local page URL. Open it on
the computer running the server and paste your token there. The page supports
English and Russian, follows your browser language, and remembers a manual choice.
The token is not passed through the tool call or returned to the model.
Ask the assistant to check the connection after submitting the form.

An alternative for a real terminal is hidden token input:

```bash
uvx --python 3.15.2 --from "git+https://github.com/ituzov/social-mcp.git@v0.1.0" social-mcp connect threads
```

No `.env` file is required for the default setup. Keep tokens out of chat and MCP
configuration files.

## Install with an agent

For automated setup, see the [installation instructions for agents](docs/installation-for-agents.md).

## Updating and removing

To update, select another published release, replace the pinned tag in the
client configuration, and reconnect MCP. Back up the account database first.
An unchanged release tag identifies the server source; `uvx` resolves package dependencies
from `pyproject.toml`. Development and CI use `uv.lock` through `uv sync --locked`.

To remove the MCP registration:

```bash
claude mcp remove social-mcp
# Or, for Codex:
codex mcp remove social-mcp
```

For JSON-based clients, remove the `social-mcp` entry. Stop the existing connection
as well. Removing the registration preserves the SQLite database and its tokens;
delete that database separately if you want to remove saved accounts. `uvx` uses
a cached environment, so there is no global `social-mcp` installation to uninstall.

## Get a Threads token

1. Open [Meta for Developers](https://developers.facebook.com/apps/) and create
   an app with the **Access the Threads API** use case.
2. Add your account as a **Threads Tester** under **App roles → Roles**, then
   accept the invitation in [Threads settings](https://www.threads.com/settings/account).
3. Open the app's Threads API settings and generate a user access token.
4. Paste it into the local connection page. Reconnecting the same account updates
   its stored token.

Use Meta's [Threads documentation](https://developers.facebook.com/docs/threads/)
for the current dashboard steps and permission requirements. The setup uses a
manually generated token; there is no OAuth callback, certificate, or tunnel to set up.

Tools need the matching permissions **on the token**, not only enabled in the app:

| Capability | Permissions used by these tools |
| --- | --- |
| Profile and own posts | `threads_basic` |
| Post and account insights | `threads_manage_insights` |
| Publishing, quotes, reposts | `threads_content_publish` |
| Reading replies and conversations | `threads_read_replies` |
| Replying | `threads_content_publish`, `threads_manage_replies` |
| Moderation | `threads_manage_replies` |
| Deleting own posts | `threads_delete` |

API access also depends on Meta's rules and access to the selected content. A
permission shown in the dashboard does not prove a request will succeed.

## Available tools

| Area | Tools |
| --- | --- |
| Connection | `threads_connect`, `threads_list_accounts`, `threads_get_profile` |
| Posts | `threads_list_posts` |
| Insights | `threads_get_post_insights`, `threads_get_account_insights` |
| Conversations | `threads_list_replies`, `threads_get_conversation` |
| Moderation | `threads_list_pending_replies`, `threads_moderate_reply` |
| Publishing | `threads_publish_text`, `threads_publish_long_text`, `threads_publish_image`, `threads_publish_carousel`, `threads_reply` |
| Post actions | `threads_quote_post`, `threads_repost`, `threads_delete_post` |

Try asking:

- “Show my latest 10 Threads posts.”
- “Show my account insights for the last week.”
- “Read the conversation under this post.”

Give an explicit instruction and exact content before asking for a publication.
Tools can perform real writes; client approval settings control their execution.

## Storage and limits

- Accounts and tokens are stored in local SQLite, in the OS user data directory
  for `social-mcp` by default. On POSIX systems the database has mode `0600`.
  Tokens are stored as plaintext in the database; this is not encrypted storage.
- To select a different database, pass `SOCIAL_MCP_DB_PATH` in your MCP client's
  environment configuration. `.env.example` documents the setting. Use an
  absolute path when sharing one database between launch commands.
- Bundled Alembic migrations run automatically at startup. Back up the database
  before upgrading the package; never commit it or its token contents.
- Token expiry is inspected when connecting. Eligible long-lived tokens are
  refreshed on use near expiry. There is no background scheduler; after inactivity
  beyond expiry, reconnect with a valid token.
- Reads return one page and a `next_cursor`. Ask for subsequent pages to read the
  rest; an empty page can still have a cursor.
- Image and carousel tools require public HTTP(S) image URLs that Meta can fetch.
  Local files and private attachment links are not uploaded by this server.
- Pending replies require reply approvals to be enabled on the original post.
- If a write times out or has an unknown outcome, check the actual posts or replies
  before retrying. A missing response does not mean the write failed.
- There is no recommendation feed, public keyword search, or video publishing tool.
- The connection page is loopback-only and stops after success or cancellation.
  Open it on the same machine as the MCP server. Remote hosting needs a separate design.

## Development

A modular monolith: `accounts` owns account storage; `threads` owns platform use
cases. MCP tools call services, which use API clients and the public account
interface. Import-linter enforces boundaries. See [AGENTS.md](AGENTS.md) for
contribution and architecture rules.

For development, clone the repository and run from its root:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run lint-imports
uv run pytest
uv run pytest --import-mode=importlib
uv build
```

[GitHub Actions](.github/workflows/ci.yml) runs these checks on pushes and pull
requests, installs the wheel in a clean environment, and verifies migrations,
localized page resources, and an actual MCP stdio connection. Automated checks do
not publish posts or call the real Meta API.

To add a page language, copy `connection/locales/en.json` under
`src/social_mcp/threads/`, keep its keys and `{username}` placeholder, translate the
values, and set `language_name` to the language's label. The picker discovers it
automatically. MCP tool descriptions and CLI output remain English.

## License

[MIT](LICENSE). Copyright (c) 2026 Social MCP contributors.
