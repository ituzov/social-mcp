# Social MCP

Local MCP server for Threads: own posts, available insights, and publishing on
request. Instagram, X, and Telegram are planned. Released under the MIT license.

## Pair programming

Work as two developers. The developer makes the decisions. Before coding,
explain the proposed change and its purpose in a few lines. When there is a
tradeoff, describe the options and their cost, then give a recommendation.

Show code one step at a time: one complete method, handler, tool, or small file.
Finish the agreed piece, show it, and wait for confirmation. Approving a plan
means agreeing with the direction, not authorizing every file at once.
Exception: small, reversible edits that can be described in one sentence.
Do not silently rewrite approved work.

If the scope grows or new constraints emerge, explain before making further
changes. Distinguish implemented code, local checks, and verified API access.

Use English for project instructions, documentation, comments, tool descriptions,
CLI output, and MCP error messages. Maintain English and Russian READMEs and
localize connection pages through JSON catalogs in `connection/locales/`. New
languages must use the same catalog keys and shared templates. Match the
developer's language in conversation.

## Documentation and tools

**This is not the stack you already know.** Installed dependencies may be newer
than your knowledge: APIs, conventions, and recommended approaches change, and
familiar patterns become outdated. Before coding in an unfamiliar area, check
the installed version and read its documentation. Do not reuse old solutions
from memory: confidently writing outdated code costs more than checking first.

Verify APIs, OAuth, permissions, limits, and pricing against current primary
sources. For libraries, use documentation for the installed version.
Meta documentation is available as Markdown by appending `.md` to the URL.

Browser use, including headless browsers and Playwright, requires permission.
Prefer curl for reading documentation. Add dependencies through the package
manager rather than editing manifests manually.

## Architecture

Use a modular monolith organized by domain. `accounts` owns connected accounts
and token storage; platform modules own their APIs and use cases. MCP handlers
call services, which use repositories and API clients.

Access other modules through their public `__init__.py` interface.
For example: `from social_mcp.accounts import get_account`.

import-linter enforces boundaries through contracts in `pyproject.toml`.
`protected` hides the internals of `accounts` and `threads`. Exception:
`migration_models.py` aggregates ORM tables for Alembic only.
`layers` defines dependency directions between modules and inside Threads:
`tools -> service -> client -> schemas/errors`. Lower layers do not depend on MCP.
The client package exposes its operations through `client/__init__.py`.
Capability files use shared HTTP helpers and explicit Meta request fields;
response schemas do not determine outgoing fields or metrics. HTTP methods and
permission hints are explicit; diagnostic stages never select request behavior.
Tool-specific argument hints belong to the platform and are registered with the
generic MCP server by its handlers.
ORM classes live in `accounts/models.py`, Pydantic account schemas in
`accounts/schemas.py`, and Threads API and MCP schemas in `threads/schemas.py`.
`mcp_server.py` turns SDK input validation failures into concise hints without
exposing submitted values. It does not misclassify execution or output schema
failures as argument errors. The application uses `SocialMCPServer`.
Account schemas are immutable; internal `Account` stores the token in
`SecretStr`, while public `AccountSummary` has no token field. Validate updated
schemas; do not bypass validation with `model_copy(update=...)`.
When adding modules or layers, extend contracts within the agreed scope rather
than disabling them to bypass a violation. Passing import-linter does not
replace checking module responsibilities or access to another module's tables.

The local connection form lives in `threads/connection/`. Other Threads code
imports `ConnectionPage` and `ThreadsConnection` through
`social_mcp.threads.connection`. `manager.py` owns the active page and its result;
its lifespan closes the page when MCP stops. Within the submodule, `page.py`
manages the session, `http.py` handles requests, `server.py` manages the HTTP
server, and `views.py` renders HTML from `templates/`.
HTML templates use Jinja2 with autoescaping and `StrictUndefined`.
The `safe` filter is allowed only for CSS and JavaScript from package files;
never apply it to user or API data.

Account tools return public summaries without tokens. Paged reads expose
`next_cursor`, never Meta's internal `paging.next` URL. An empty list with a
cursor is valid.
Obtain credentials through `TokenLifecycle`, which owns expiry inspection,
refresh, and temporary-failure backoff. There is no background scheduler.
Preserve the interprocess lock and conditional token/expiry write: a late
refresh must not overwrite a newer connection, and temporary failures must
preserve a valid token. Detailed timing and API behavior belong in code and tests.
Requests with secrets in query parameters use `RedactingTransport`: secrets are
removed from URLs after sending and before HTTPX logging or error reporting.

## Database and migrations

SQLite is accessed through SQLAlchemy. Connection setup and migration execution
live in `database.py`; the account table model belongs to `accounts`.
Do not create the schema with ORM `create_all`: Alembic manages schema changes.

MCP startup applies pending migrations bundled with the package under an
interprocess lock and in a transaction. First installation creates the schema
from scratch; legacy databases without Alembic history are not supported yet.
An unknown newer database revision prevents startup.

Generate migrations from ORM models with `alembic revision --autogenerate`,
then review the result. Do not write them manually without separate agreement.
When ORM models change, add a new migration; never rewrite applied migrations.
Register new tables in `migration_models.py`.
Developer commands from the project root:

```bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
uv run alembic check
```

Before running these commands, verify that `SOCIAL_MCP_DB_PATH` points to the
intended local database. Always review autogenerated migrations manually.
Test upgrades on a temporary copy and verify existing data is preserved.

## Credentials and publishing

Real publishing, including test posts, requires an explicit command with the
content and platform agreed in advance. Granting API permissions or developing
a publishing tool does not authorize publication.

Secrets must never reach Git, documentation, logs, or tool responses.
Never ask for tokens in chat. Agree separately on credential storage.

Do not retry publishing after a timeout without checking the result: the post
may already have been published. Confirm access by actually reading the account,
posts, and insights rather than relying on Dashboard permissions.

## Checks and Git

Run checks for the current piece during development. For APIs, account for token
expiry, pagination, rate limits, missing metrics, and repeated requests.
Verify protective tests by introducing controlled failures one at a time in an
isolated copy. Automated tests must not publish real posts.

After Python changes, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run lint-imports
uv run pytest
```

Commit and push only on request; show the proposed message first.
Keep commits atomic: one complete idea with its checks.
Format: `type(module): description`, with an English description,
without `Co-Authored-By` or other AI attribution.
Before committing, inspect the diff for secrets and compliance with project rules.
