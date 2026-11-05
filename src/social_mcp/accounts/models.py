"""ORM table definition owned by the accounts module."""

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from social_mcp.database import Base


class AccountRecord(Base):
    __tablename__ = "accounts"

    platform: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str | None] = mapped_column(Text)
    access_token: Mapped[str] = mapped_column(Text)
    obtained_at: Mapped[int]
    expires_at: Mapped[int | None]
