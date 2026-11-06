"""Durable container preparation and the single-attempt publication boundary."""

from sqlalchemy import JSON, Text
from sqlalchemy.orm import Mapped, mapped_column

from social_mcp.database import Base


class PublicationRecord(Base):
    __tablename__ = "threads_publications"

    publication_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, index=True)
    container_id: Mapped[str | None] = mapped_column(Text)
    children: Mapped[list[str]] = mapped_column(JSON)
    caption: Mapped[str | None] = mapped_column(Text)
    reply_to_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    post_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int]
