"""Threads use cases: connected accounts, token expiry, and profile identity."""

import asyncio
from pathlib import Path

from pydantic import ValidationError

from social_mcp.accounts import AccountStorageError, AccountSummary, list_accounts
from social_mcp.threads.client import (
    create_carousel_container,
    create_image_container,
    create_long_text_container,
    create_quote_container,
    create_text_container,
    delete_post,
    fetch_account_insights,
    fetch_container_status,
    fetch_conversation,
    fetch_pending_replies,
    fetch_post_insights,
    fetch_posts,
    fetch_profile,
    fetch_replies,
    moderate_reply,
    publication_unknown,
    publish_container,
    repost_post,
)
from social_mcp.threads.diagnostics import record_publication
from social_mcp.threads.errors import ThreadsError
from social_mcp.threads.schemas import (
    AccountInsights,
    AccountInsightsQuery,
    CarouselPostInput,
    DeletedPost,
    ImageInput,
    ImagePostInput,
    LongTextPostInput,
    PendingRepliesQuery,
    PostActionInput,
    PostInsights,
    PostsPage,
    PostsQuery,
    PublishedPost,
    QuotePostInput,
    RepliesPage,
    ReplyApprovalStatus,
    ReplyControl,
    ReplyModerationAction,
    ReplyModerationInput,
    ReplyModerationResult,
    TextPostInput,
    TextPublicationInput,
    TextReplyInput,
    ThreadsProfile,
)
from social_mcp.threads.tokens import TokenLifecycle


class ThreadsService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.tokens: TokenLifecycle = TokenLifecycle(db_path)

    async def publish_text(
        self,
        account_id: str,
        text: str,
        *,
        reply_control: ReplyControl | None = None,
        enable_reply_approvals: bool = False,
    ) -> PublishedPost:
        """Create, check readiness, and publish the exact approved text once."""
        try:
            post = TextPublicationInput(
                text=text,
                reply_control=reply_control,
                enable_reply_approvals=enable_reply_approvals,
            )
        except ValidationError:
            raise ThreadsError(
                "Provide non-blank text of at most 500 characters, a supported "
                "reply_control and a boolean enable_reply_approvals."
            ) from None
        return await self._publish_validated_text(account_id, post)

    async def publish_long_text(
        self,
        account_id: str,
        text: str,
        attachment_text: str,
        *,
        link_url: str | None = None,
        reply_to_id: str | None = None,
        reply_control: ReplyControl | None = None,
        enable_reply_approvals: bool = False,
    ) -> PublishedPost:
        """Publish the approved preview and attachment without trimming or splitting."""
        try:
            post = LongTextPostInput(
                text=text,
                attachment_text=attachment_text,
                link_url=link_url,
                reply_to_id=reply_to_id,
                reply_control=reply_control,
                enable_reply_approvals=enable_reply_approvals,
            )
        except ValidationError:
            raise ThreadsError(
                "Provide non-blank text up to 500 characters and attachment_text up to "
                "10000 characters, an optional HTTP(S) link_url without whitespace, "
                "a numeric reply_to_id, and valid reply settings."
            ) from None
        account = await self.tokens.get_account(account_id)
        token = account.access_token.get_secret_value()
        container_id = await create_long_text_container(token, account.account_id, post)
        return await self._publish_ready_container(
            token, account.account_id, container_id, reply_to_id=post.reply_to_id
        )

    async def moderate_reply(
        self, account_id: str, reply_id: str, operation: ReplyModerationAction
    ) -> ReplyModerationResult:
        """Validate the explicitly requested operation before loading credentials."""
        try:
            request = ReplyModerationInput(reply_id=reply_id, operation=operation)
        except ValidationError:
            raise ThreadsError(
                "Provide a numeric reply_id and operation hide, unhide, approve, or ignore."
            ) from None
        account = await self.tokens.get_account(account_id)
        return await moderate_reply(
            account.access_token.get_secret_value(), request.reply_id, request.operation
        )

    async def list_pending_replies(
        self,
        account_id: str,
        post_id: str,
        *,
        limit: int = 20,
        after: str | None = None,
        approval_status: ReplyApprovalStatus | None = None,
    ) -> RepliesPage:
        """Read one page of pending/ignored replies under the selected own post."""
        try:
            target = PostActionInput(post_id=post_id)
            query = PendingRepliesQuery(
                limit=limit, after=after, approval_status=approval_status
            )
        except ValidationError:
            raise ThreadsError(
                "Provide a numeric post_id, a limit from 1 to 100, a non-empty cursor "
                "up to 4096 characters, and approval_status pending or ignored, or omit it."
            ) from None
        account = await self.tokens.get_account(account_id)
        return await fetch_pending_replies(
            account.access_token.get_secret_value(), target.post_id, query
        )

    async def reply(
        self, account_id: str, reply_to_id: str, text: str
    ) -> PublishedPost:
        """Publish an approved reply to a specific post or reply."""
        try:
            reply = TextReplyInput(text=text, reply_to_id=reply_to_id)
        except ValidationError:
            raise ThreadsError(
                "Provide a numeric reply_to_id and non-blank text of at most 500 characters."
            ) from None
        return await self._publish_validated_text(
            account_id, reply, reply_to_id=reply.reply_to_id
        )

    async def quote_post(
        self, account_id: str, post_id: str, text: str
    ) -> PublishedPost:
        """Publish the exact approved text with a reference to the selected original."""
        try:
            post = QuotePostInput(quote_post_id=post_id, text=text)
        except ValidationError:
            raise ThreadsError(
                "Provide a numeric post_id and non-blank text of at most 500 characters."
            ) from None
        account = await self.tokens.get_account(account_id)
        token = account.access_token.get_secret_value()
        container_id = await create_quote_container(
            token, account.account_id, post.text, post.quote_post_id
        )
        return await self._publish_ready_container(
            token, account.account_id, container_id
        )

    async def repost(self, account_id: str, post_id: str) -> PublishedPost:
        """Repost the selected original once using the current account token."""
        try:
            target = PostActionInput(post_id=post_id)
        except ValidationError:
            raise ThreadsError(
                "Provide a numeric post_id for the original post."
            ) from None
        account = await self.tokens.get_account(account_id)
        result = await repost_post(
            account.access_token.get_secret_value(), target.post_id
        )
        return PublishedPost(post_id=result)

    async def delete_post(self, account_id: str, post_id: str) -> DeletedPost:
        """Delete the agreed own post once; Meta enforces ownership through the token."""
        try:
            target = PostActionInput(post_id=post_id)
        except ValidationError:
            raise ThreadsError(
                "Provide a numeric post_id for the post to delete."
            ) from None
        account = await self.tokens.get_account(account_id)
        return await delete_post(
            account.access_token.get_secret_value(), target.post_id
        )

    async def publish_image(
        self,
        account_id: str,
        image_url: str,
        *,
        text: str | None = None,
        alt_text: str | None = None,
    ) -> PublishedPost:
        """Validate the approved image and caption before accessing credentials."""
        try:
            post = ImagePostInput(image_url=image_url, text=text, alt_text=alt_text)
        except ValidationError:
            raise ThreadsError(
                "Provide an HTTP(S) image URL without whitespace, an optional caption "
                "of at most 500 characters, and alt_text of at most 1000 characters."
            ) from None
        account = await self.tokens.get_account(account_id)
        token = account.access_token.get_secret_value()
        container_id = await create_image_container(
            token, account.account_id, post, text=post.text
        )
        return await self._publish_ready_container(
            token, account.account_id, container_id
        )

    async def publish_carousel(
        self, account_id: str, images: list[ImageInput], *, text: str | None = None
    ) -> PublishedPost:
        """Create all children in order; publish only their ready parent container."""
        try:
            post = CarouselPostInput(images=images, text=text)
        except ValidationError:
            raise ThreadsError(
                "Provide 2 to 20 images with HTTP(S) URLs without whitespace, "
                "alt_text of at most 1000 characters per image, "
                "and an optional caption of at most 500 characters."
            ) from None
        account = await self.tokens.get_account(account_id)
        token = account.access_token.get_secret_value()
        children = []
        for image in post.images:
            children.append(
                await create_image_container(
                    token, account.account_id, image, is_carousel_item=True
                )
            )
        await asyncio.sleep(30)
        for child_id in children:
            await self._require_ready_container(token, child_id)
        container_id = await create_carousel_container(
            token, account.account_id, children, text=post.text
        )
        return await self._publish_ready_container(
            token, account.account_id, container_id
        )

    async def _publish_validated_text(
        self,
        account_id: str,
        post: TextPostInput,
        *,
        reply_to_id: str | None = None,
    ) -> PublishedPost:
        """Share container readiness and single-attempt publishing for posts and replies."""
        account = await self.tokens.get_account(account_id)
        token = account.access_token.get_secret_value()
        container_id = await create_text_container(
            token,
            account.account_id,
            post.text,
            reply_to_id=reply_to_id,
            **post.model_dump(
                include={"reply_control", "enable_reply_approvals"},
                exclude_defaults=True,
            ),
        )
        return await self._publish_ready_container(
            token, account.account_id, container_id, reply_to_id=reply_to_id
        )

    async def _publish_ready_container(
        self,
        token: str,
        account_id: str,
        container_id: str,
        *,
        reply_to_id: str | None = None,
    ) -> PublishedPost:
        """Wait for processing, require readiness, and attempt publication once."""
        # Meta recommends allowing processing time before publishing a container.
        await asyncio.sleep(30)
        await self._require_ready_container(
            token, container_id, reply_to_id=reply_to_id
        )
        post_id = await publish_container(
            token, account_id, container_id, reply_to_id=reply_to_id
        )
        record_publication("publish", "confirmed")
        return PublishedPost(post_id=post_id)

    @staticmethod
    async def _require_ready_container(
        token: str, container_id: str, *, reply_to_id: str | None = None
    ) -> None:
        """Fail closed on unknown states and avoid publishing a container twice."""
        status = await fetch_container_status(token, container_id)
        if status.status == "PUBLISHED":
            record_publication(
                "status", "already_published", payload={"status": status.status}
            )
            raise ThreadsError(
                publication_unknown(container_id, reply_to_id=reply_to_id)
            )
        if status.status != "FINISHED":
            record_publication("status", "not_ready", payload={"status": status.status})
            raise ThreadsError(
                f"Container {container_id} is not ready or could not be processed. "
                "No publication was attempted."
            )

    async def connect(self, access_token: str) -> ThreadsProfile:
        """Verify the token through /me before saving its owner in SQLite."""
        access_token = access_token.strip()
        if not access_token:
            raise ThreadsError("The Threads access token must not be empty.")
        info = await self.tokens.inspect(access_token)
        profile = await fetch_profile(access_token)
        if profile.id != info.user_id:
            raise ThreadsError("The token belongs to a different Threads account.")
        await self.tokens.save(profile, access_token, info)
        return profile

    async def get_profile(self, account_id: str) -> ThreadsProfile:
        account = await self.tokens.get_account(account_id)
        profile = await fetch_profile(account.access_token.get_secret_value())
        if profile.id != account.account_id:
            raise ThreadsError("The token belongs to a different Threads account.")
        return profile

    async def list_accounts(self) -> list[AccountSummary]:
        try:
            return await asyncio.to_thread(
                list_accounts, self.db_path, platform="threads"
            )
        except AccountStorageError:
            raise ThreadsError("Could not read the account list.") from None

    async def get_account_insights(
        self, account_id: str, *, since: int | None = None, until: int | None = None
    ) -> AccountInsights:
        """Validate the range before reading the selected account's current token."""
        try:
            query = AccountInsightsQuery(since=since, until=until)
        except ValidationError:
            raise ThreadsError(
                "Provide both since and until as Unix seconds, or omit both. "
                "Use timestamps at least 1712991600, with since earlier than until."
            ) from None
        account = await self.tokens.get_account(account_id)
        return await fetch_account_insights(
            account.access_token.get_secret_value(), account.account_id, query
        )

    async def get_post_insights(self, account_id: str, post_id: str) -> PostInsights:
        """Read post insights using the current account token."""
        account = await self.tokens.get_account(account_id)
        return await fetch_post_insights(
            account.access_token.get_secret_value(), post_id
        )

    async def list_posts(
        self, account_id: str, *, limit: int = 20, after: str | None = None
    ) -> PostsPage:
        try:
            query = PostsQuery(limit=limit, after=after)
        except ValidationError:
            raise ThreadsError(
                "Use a limit from 1 to 100 and a non-empty cursor of at most 4096 characters."
            ) from None
        account = await self.tokens.get_account(account_id)
        return await fetch_posts(
            account.access_token.get_secret_value(), account.account_id, query
        )

    async def list_replies(
        self,
        account_id: str,
        post_id: str,
        *,
        limit: int = 20,
        after: str | None = None,
    ) -> RepliesPage:
        """Read direct replies using the selected account's current token."""
        try:
            query = PostsQuery(limit=limit, after=after)
        except ValidationError:
            raise ThreadsError(
                "Use a limit from 1 to 100 and a non-empty cursor of at most 4096 characters."
            ) from None
        account = await self.tokens.get_account(account_id)
        return await fetch_replies(
            account.access_token.get_secret_value(), post_id, query
        )

    async def get_conversation(
        self,
        account_id: str,
        post_id: str,
        *,
        limit: int = 20,
        after: str | None = None,
    ) -> RepliesPage:
        """Read direct and nested replies using the selected account's current token."""
        try:
            query = PostsQuery(limit=limit, after=after)
        except ValidationError:
            raise ThreadsError(
                "Use a limit from 1 to 100 and a non-empty cursor of at most 4096 characters."
            ) from None
        account = await self.tokens.get_account(account_id)
        return await fetch_conversation(
            account.access_token.get_secret_value(), post_id, query
        )
