"""Alembic environment for application startup and developer commands."""

from alembic import context
from sqlalchemy import Connection

from social_mcp.config import Settings
from social_mcp.database import migration_connection
from social_mcp.migration_models import metadata


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


external_connection: Connection | None = context.config.attributes.get("connection")
if external_connection is not None:
    run_migrations(external_connection)
else:
    with migration_connection(Settings().db_path) as migration_conn:
        run_migrations(migration_conn)
