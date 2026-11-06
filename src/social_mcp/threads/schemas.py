"""Request and response schemas for the Threads API and MCP."""

from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

type ThreadsID = Annotated[str, Field(strict=True, pattern=r"^[0-9]+$")]
type ThreadsText = Annotated[
    str, Field(strict=True, min_length=1, max_length=500, pattern=r"\S")
]
type ReplyControl = Literal[
    "everyone",
    "accounts_you_follow",
    "mentioned_only",
    "parent_post_author_only",
    "followers_only",
]
type ReplyModerationAction = Literal["hide", "unhide", "approve", "ignore"]
type ReplyApprovalStatus = Literal["pending", "ignored"]
type ThreadsLongText = Annotated[
    str, Field(strict=True, min_length=1, max_length=10000, pattern=r"\S")
]


def _validate_http_url(value: str) -> str:
    url = AnyHttpUrl(value)
    if url.host is None or any(character.isspace() for character in value):
        raise ValueError("Provide an HTTP(S) URL without whitespace.")
    return value


class TextPostInput(BaseModel):
    """Validate text without trimming or otherwise changing the approved content."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    text: ThreadsText = Field(repr=False)


class TextPublicationInput(TextPostInput):
    """Text and explicitly selected reply settings; omitted settings use Meta defaults."""

    reply_control: ReplyControl | None = None
    enable_reply_approvals: bool = False


class LongTextPostInput(TextPublicationInput):
    """The exact preview and long text attachment, optionally published as a reply."""

    attachment_text: ThreadsLongText = Field(repr=False)
    link_url: str | None = Field(default=None, repr=False)
    reply_to_id: ThreadsID | None = None

    @field_validator("link_url")
    @classmethod
    def validate_link_url(cls, value: str | None) -> str | None:
        return _validate_http_url(value) if value is not None else None


class TextAttachment(BaseModel):
    """Public attachment fields; styling and unknown future metadata are omitted."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    plaintext: str | None = Field(default=None, repr=False)
    link_attachment_url: str | None = Field(default=None, repr=False)


class ReplyModerationInput(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    reply_id: ThreadsID
    operation: ReplyModerationAction


class ActionAcknowledgement(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    success: bool


class ReplyModerationResult(ActionAcknowledgement):
    reply_id: ThreadsID
    operation: ReplyModerationAction


class ImageInput(BaseModel):
    """An externally hosted image with its exact URL and accessibility text."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True, extra="forbid")

    image_url: str = Field(min_length=1, repr=False)
    alt_text: str | None = Field(default=None, max_length=1000, repr=False)

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str) -> str:
        return _validate_http_url(value)


class ImagePostInput(ImageInput):
    """A single image with an optional, unchanged caption."""

    text: str | None = Field(default=None, max_length=500, repr=False)


class CarouselPostInput(BaseModel):
    """Two to twenty images in the approved order, with one optional caption."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True, extra="forbid")

    images: list[ImageInput] = Field(min_length=2, max_length=20, repr=False)
    text: str | None = Field(default=None, max_length=500, repr=False)


class TextReplyInput(TextPostInput):
    """The exact reply text and the post or reply it addresses."""

    reply_to_id: ThreadsID


class QuotePostInput(TextPostInput):
    """The approved text and numeric ID of the quoted post."""

    quote_post_id: ThreadsID


class PostActionInput(BaseModel):
    """A validated target for a repost or deletion."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    post_id: ThreadsID


class DeletedPost(BaseModel):
    """Meta's deletion acknowledgement; the client also checks the target ID."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    success: bool
    deleted_id: ThreadsID


class MediaCreationResponse(BaseModel):
    """Meta returns a container ID or a published media ID, depending on the endpoint."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    id: ThreadsID


class ContainerStatus(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    status: str


class PublishedPost(BaseModel):
    """A confirmed published post; the container ID is not a post ID."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    post_id: ThreadsID


class PublicationListInput(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    account_id: ThreadsID
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20


class PublicationResumeInput(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    account_id: ThreadsID
    publication_id: Annotated[
        str, Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    ]


class PublicationSummary(BaseModel):
    """Public recovery metadata; excludes caption, token and media source URLs."""

    model_config = ConfigDict(strict=True, frozen=True, hide_input_in_errors=True)
    publication_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    account_id: ThreadsID
    container_id: ThreadsID | None
    reply_to_id: ThreadsID | None
    state: Literal["preparing", "publishing", "published", "failed"]
    post_id: ThreadsID | None
    created_at: int


class PreparedPublication(PublicationSummary):
    """Private preparation data, never return this schema from an MCP tool."""

    children: list[ThreadsID] = Field(repr=False, exclude=True)
    caption: str | None = Field(repr=False, exclude=True)

    @model_validator(mode="after")
    def require_recoverable_containers(self) -> PreparedPublication:
        if self.container_id is None and not 2 <= len(self.children) <= 20:
            raise ValueError("A prepared carousel needs 2 to 20 child containers.")
        if self.state == "published" and self.post_id is None:
            raise ValueError("A published preparation needs a confirmed post ID.")
        return self


class ThreadsProfile(BaseModel):
    """Allowed profile fields only, excluding internal connection data."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    id: str
    username: str
    name: str | None = None
    threads_biography: str | None = None
    threads_profile_picture_url: str | None = None
    is_verified: bool | None = None


class MediaReference(BaseModel):
    """A public reference to an original, parent, or root Threads post."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    id: ThreadsID


class ThreadsPost(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    id: str
    text: str | None = None
    timestamp: str | None = None
    media_type: str | None = None
    permalink: str | None = None
    is_quote_post: bool | None = None
    quoted_post: MediaReference | None = None
    reposted_post: MediaReference | None = None
    text_attachment: TextAttachment | None = None


class ThreadsReply(ThreadsPost):
    """Public reply fields; unavailable metadata remains unknown."""

    username: str | None = None
    has_replies: bool | None = None
    is_reply_owned_by_me: bool | None = None
    hide_status: str | None = None
    root_post: MediaReference | None = None
    replied_to: MediaReference | None = None
    reply_approval_status: str | None = None


class RepliesPage(BaseModel):
    """One page of replies, without Meta's internal pagination URLs."""

    items: list[ThreadsReply]
    next_cursor: str | None = None


class PostInsights(BaseModel):
    """Post insights: None means Meta did not return the metric."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    post_id: str
    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    quotes: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)


class AccountInsightDay(BaseModel):
    """A profile view count and the end of its daily interval, as reported by Meta."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    value: int = Field(ge=0)
    end_time: AwareDatetime = Field(strict=False)


class AccountInsights(BaseModel):
    """Period metrics and a separate current follower count; None means unavailable."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    account_id: ThreadsID
    since: int | None = None
    until: int | None = None
    views: list[AccountInsightDay] | None = None
    likes: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    quotes: int | None = Field(default=None, ge=0)
    clicks: int | None = Field(default=None, ge=0)
    followers_count: int | None = Field(default=None, ge=0)


class AccountInsightsQuery(BaseModel):
    """An explicit Unix timestamp range, or Meta's default yesterday and today."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    since: int | None = Field(default=None, ge=1712991600)
    until: int | None = Field(default=None, ge=1712991600)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (self.since is None) != (self.until is None):
            raise ValueError("Provide both since and until, or omit both.")
        if self.since is not None and self.until is not None:
            if self.since >= self.until:
                raise ValueError("since must be earlier than until.")
        return self


class PostsQuery(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    limit: int = Field(default=20, ge=1, le=100)
    after: str | None = Field(default=None, min_length=1, max_length=4096)


class PendingRepliesQuery(PostsQuery):
    approval_status: ReplyApprovalStatus | None = None


class PostsPage(BaseModel):
    items: list[ThreadsPost]
    next_cursor: str | None = None


class PostCursors(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    after: str | None = Field(default=None, min_length=1, max_length=4096)


class PostPaging(BaseModel):
    """Internal Meta response; never expose the next-page URL through MCP."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    cursors: PostCursors = Field(default_factory=PostCursors)
    next_url: str | None = Field(
        default=None, validation_alias="next", repr=False, exclude=True
    )


class PostsResponse(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    data: list[ThreadsPost]
    paging: PostPaging | None = None


class RepliesResponse(BaseModel):
    """Internal Meta reply response, parsed before building a public page."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    data: list[ThreadsReply]
    paging: PostPaging | None = None


class TokenInfo(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    user_id: str = Field(pattern=r"^[0-9]+$")
    is_valid: bool
    issued_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    scopes: list[str]
    data_access_expires_at: int = Field(default=0, ge=0)

    @property
    def usable_until(self) -> int:
        return min(self.expires_at, self.data_access_expires_at or self.expires_at)


class RefreshedToken(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    access_token: SecretStr = Field(min_length=1)
    expires_in: int = Field(gt=0)
