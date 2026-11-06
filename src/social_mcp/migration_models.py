"""ORM table registry for Alembic; the only external aggregation point."""

from social_mcp.accounts.models import AccountRecord
from social_mcp.threads.publications.models import PublicationRecord

metadata = AccountRecord.metadata
assert PublicationRecord.metadata is metadata
