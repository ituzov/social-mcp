"""Bounded container preparation and durable single-attempt publication."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from filelock import AsyncFileLock, Timeout

from social_mcp.threads.async_utils import finish_before_cancel
from social_mcp.threads.client import (
    create_carousel_container,
    fetch_container_status,
    publication_unknown,
    publish_container,
)
from social_mcp.threads.diagnostics import record_publication
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.publications import PublicationStore
from social_mcp.threads.schemas import PreparedPublication, PublishedPost

# A single budget covers children, parent processing and their status requests.
PREPARATION_TIMEOUT = 180
POLL_INTERVAL = 60
MAX_STATUS_CHECKS = 3


class PublicationManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.store = PublicationStore(db_path)

    async def prepare(
        self,
        account_id: str,
        *,
        container_id: str | None = None,
        children: list[str] | None = None,
        caption: str | None = None,
        reply_to_id: str | None = None,
    ) -> str:
        return await finish_before_cancel(
            asyncio.to_thread(
                self.store.create,
                account_id,
                container_id=container_id,
                children=children,
                caption=caption,
                reply_to_id=reply_to_id,
            )
        )

    @asynccontextmanager
    async def _lock(self, publication_id: str):
        # IDs are validated before use as paths; locks serialize all MCP processes.
        path = self.db_path.with_name(
            f"{self.db_path.name}.publish-{publication_id}.lock"
        )
        try:
            async with AsyncFileLock(path, timeout=0, mode=0o600):
                yield
        except Timeout:
            raise ThreadsError(
                "This prepared publication is already being handled. Check saved publications before continuing."
            ) from None

    async def _change(self, publication_id: str, **values: str | None) -> None:
        await finish_before_cancel(
            asyncio.to_thread(self.store.change, publication_id, **values)
        )

    async def resume(
        self,
        token: str,
        account_id: str,
        publication_id: str,
        *,
        initial: bool = False,
    ) -> PublishedPost:
        async with self._lock(publication_id):
            job = await asyncio.to_thread(self.store.get, account_id, publication_id)
            if job.state == "published" and job.post_id is not None:
                return PublishedPost(post_id=job.post_id)
            if job.state == "publishing":
                raise ThreadsError(self._unknown(job))
            if job.state != "preparing":
                raise ThreadsError(
                    "This prepared publication cannot be resumed. Check the original request and Meta container status."
                )
            try:
                async with asyncio.timeout(PREPARATION_TIMEOUT):
                    container_id = job.container_id
                    if container_id is None:
                        await self._wait(token, job.children, job, initial=initial)
                        container_id = await create_carousel_container(
                            token,
                            account_id,
                            job.children,
                            text=job.caption,
                        )
                        await self._change(
                            publication_id, container_id=container_id, caption=None
                        )
                        initial = True
                    await self._wait(token, [container_id], job, initial=initial)
            except TimeoutError:
                raise ThreadsError(self._pending(publication_id)) from None
            except ThreadsError as error:
                raise ThreadsError(
                    f"{error.user_message} Saved publication_id={publication_id}; use threads_list_publications before continuing."
                ) from None
            # Persist the irreversible boundary BEFORE sending. A crash/cancellation or
            # even an explicit API rejection leaves it blocked, never automatically retried.
            await self._change(publication_id, state="publishing")
            post_id = await publish_container(
                token,
                account_id,
                container_id,
                reply_to_id=job.reply_to_id,
            )
            await self._change(publication_id, state="published", post_id=post_id)
            record_publication("publish", "confirmed")
            return PublishedPost(post_id=post_id)

    async def _wait(
        self,
        token: str,
        containers: list[str],
        job: PreparedPublication,
        *,
        initial: bool,
    ) -> None:
        if initial:
            await asyncio.sleep(30)
        pending = list(containers)
        for attempt in range(MAX_STATUS_CHECKS):
            remaining = []
            for container_id in pending:
                status = await fetch_container_status(token, container_id)
                if status.status == "FINISHED":
                    continue
                if status.status == "IN_PROGRESS":
                    record_publication(
                        "status", "not_ready", payload={"status": status.status}
                    )
                    remaining.append(container_id)
                    continue
                if status.status == "PUBLISHED":
                    await self._change(job.publication_id, state="publishing")
                    record_publication(
                        "status", "already_published", payload={"status": status.status}
                    )
                    raise ThreadsError(
                        publication_unknown(container_id, reply_to_id=job.reply_to_id)
                    )
                await self._change(job.publication_id, state="failed", caption=None)
                record_publication(
                    "status", "not_ready", payload={"status": status.status}
                )
                raise ThreadsError(
                    f"Container {container_id} is not ready or could not be processed. No publication was attempted."
                )
            if not remaining:
                return
            if attempt + 1 < MAX_STATUS_CHECKS:
                await asyncio.sleep(POLL_INTERVAL)
            pending = remaining
        raise ThreadsError(self._pending(job.publication_id))

    @staticmethod
    def _pending(publication_id: str) -> str:
        return (
            f"Threads is still processing the prepared publication (publication_id={publication_id}). "
            "No publication was attempted. Use threads_resume_publication with this ID "
            "to continue the same approved content; do not create it again."
        )

    @staticmethod
    def _unknown(job: PreparedPublication) -> str:
        if job.container_id is None:
            return "Publication outcome is unknown. Check posts or replies before continuing; do not send again."
        return publication_unknown(job.container_id, reply_to_id=job.reply_to_id)
