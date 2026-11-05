"""Cached public-release checks; never access accounts or install updates."""

import asyncio
import re
import time
from typing import Literal

import httpx
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

LATEST_RELEASE_URL = "https://api.github.com/repos/ituzov/social-mcp/releases/latest"
RELEASES_URL = "https://github.com/ituzov/social-mcp/releases/tag/"
CACHE_SECONDS = 6 * 60 * 60
REQUEST_TIMEOUT = 2.0


class UpdateStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    current_version: str
    status: Literal["up_to_date", "update_available", "unavailable", "disabled"]
    latest_version: str | None = None
    update_available: bool | None = None
    release_url: str | None = None
    checked_at: int | None = None


class _Release(BaseModel):
    model_config = ConfigDict(strict=True)

    tag_name: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+$", max_length=64)
    draft: Literal[False]
    prerelease: Literal[False]


class UpdateChecker:
    def __init__(self, current_version: str, *, enabled: bool = True) -> None:
        self.current_version = str(Version(current_version))
        self.enabled = enabled
        self._lock = asyncio.Lock()
        self._cached: UpdateStatus | None = None
        self._deadline = 0.0
        self._generation = 0

    async def check(self, *, force: bool = False) -> UpdateStatus:
        """Coalesce requests, including concurrent forced checks; cache failures too."""
        if not self.enabled:
            return UpdateStatus(current_version=self.current_version, status="disabled")
        generation = self._generation
        async with self._lock:
            cached = self._cached
            if (
                cached is not None
                and time.monotonic() < self._deadline
                and (not force or generation != self._generation)
            ):
                return cached
            result = await self._fetch()
            self._cached = result
            self._deadline = time.monotonic() + CACHE_SECONDS
            self._generation += 1
            return result

    async def _fetch(self) -> UpdateStatus:
        checked_at = int(time.time())
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with httpx.AsyncClient(
                    timeout=REQUEST_TIMEOUT, follow_redirects=False
                ) as client:
                    response = await client.get(
                        LATEST_RELEASE_URL,
                        headers={
                            "Accept": "application/vnd.github+json",
                            "User-Agent": "social-mcp-update-check",
                        },
                    )
                response.raise_for_status()
                release = _Release.model_validate(response.json())
                # Use only a validated tag. Never expose release notes or a supplied URL.
                tag = release.tag_name
                if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) is None:
                    raise ValueError("Unsupported release tag")
                latest = Version(tag)
                newer = latest > Version(self.current_version)
                return UpdateStatus(
                    current_version=self.current_version,
                    latest_version=str(latest),
                    update_available=newer,
                    status="update_available" if newer else "up_to_date",
                    release_url=RELEASES_URL + tag,
                    checked_at=checked_at,
                )
        except httpx.HTTPError, ValueError, TimeoutError:
            return UpdateStatus(
                current_version=self.current_version,
                status="unavailable",
                checked_at=checked_at,
            )
