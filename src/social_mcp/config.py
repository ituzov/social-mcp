"""Settings from the environment and .env in the current working directory."""

from pathlib import Path

from platformdirs import user_data_path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOCIAL_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    db_path: Path = Field(
        default_factory=lambda: (
            user_data_path("social-mcp", appauthor=False) / "social-mcp.sqlite3"
        ),
    )
    check_updates: bool = True

    @field_validator("db_path", mode="before")
    @classmethod
    def reject_empty_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("The database path must not be empty")
        return value

    @field_validator("db_path")
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()
