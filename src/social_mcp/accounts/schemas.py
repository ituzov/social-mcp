"""Account schemas for internal logic and public MCP responses."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Account(BaseModel):
    """Internal account record; never include the entire record in MCP responses."""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    platform: str
    account_id: str
    username: str | None
    access_token: SecretStr = Field(repr=False, min_length=1)
    obtained_at: int
    expires_at: int | None


class AccountSummary(BaseModel):
    """Public account summary without a token field."""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    platform: str
    account_id: str
    username: str | None
    obtained_at: int
    expires_at: int | None
