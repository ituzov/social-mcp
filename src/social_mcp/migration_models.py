"""ORM table registry for Alembic; the only external aggregation point."""

from social_mcp.accounts.models import AccountRecord

metadata = AccountRecord.metadata
