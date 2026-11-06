"""Store preparation metadata, never access tokens or image source URLs."""

import time
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from social_mcp.database import open_database
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.publications.models import PublicationRecord
from social_mcp.threads.schemas import PreparedPublication, PublicationSummary


class PublicationStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def create(
        self,
        account_id: str,
        *,
        container_id: str | None = None,
        children: list[str] | None = None,
        caption: str | None = None,
        reply_to_id: str | None = None,
    ) -> str:
        publication_id = uuid4().hex
        try:
            with (
                open_database(self.path) as engine,
                Session(engine) as session,
                session.begin(),
            ):
                session.add(
                    PublicationRecord(
                        publication_id=publication_id,
                        account_id=account_id,
                        container_id=container_id,
                        children=children or [],
                        caption=caption,
                        reply_to_id=reply_to_id,
                        state="preparing",
                        post_id=None,
                        created_at=int(time.time()),
                    )
                )
        except SQLAlchemyError:
            raise ThreadsError(
                "Could not save prepared publication. No publication was attempted."
            ) from None
        return publication_id

    def get(self, account_id: str, publication_id: str) -> PreparedPublication:
        try:
            with (
                open_database(self.path, mode="ro") as engine,
                Session(engine) as session,
            ):
                row = session.get(PublicationRecord, publication_id)
                if row is None or row.account_id != account_id:
                    raise ThreadsError(
                        "No saved publication with this ID exists for the selected account."
                    )
                return PreparedPublication.model_validate(row, from_attributes=True)
        except SQLAlchemyError, ValidationError:
            raise ThreadsError("Could not read prepared publication storage.") from None

    def change(self, publication_id: str, **values: str | None) -> None:
        try:
            with open_database(self.path) as engine, engine.begin() as connection:
                result = connection.execute(
                    update(PublicationRecord)
                    .where(PublicationRecord.publication_id == publication_id)
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise ThreadsError("Prepared publication no longer exists.")
        except SQLAlchemyError:
            raise ThreadsError(
                "Could not update prepared publication storage. Check saved publications before continuing."
            ) from None

    def list(self, account_id: str, *, limit: int = 20) -> list[PublicationSummary]:
        try:
            with (
                open_database(self.path, mode="ro") as engine,
                Session(engine) as session,
            ):
                rows = session.scalars(
                    select(PublicationRecord)
                    .where(PublicationRecord.account_id == account_id)
                    .order_by(
                        PublicationRecord.created_at.desc(),
                        PublicationRecord.publication_id,
                    )
                    .limit(limit)
                )
                return [
                    PublicationSummary.model_validate(row, from_attributes=True)
                    for row in rows
                ]
        except SQLAlchemyError, ValidationError:
            raise ThreadsError("Could not read prepared publication storage.") from None
