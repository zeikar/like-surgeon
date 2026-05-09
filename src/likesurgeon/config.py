"""Local app configuration: paths, env overrides, on-disk layout."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APP_DIR_NAME = ".like-surgeon"
DEFAULT_DB_FILENAME = "like-surgeon.sqlite"
YTMUSIC_BROWSER_FILENAME = "browser.json"
YOUTUBE_OAUTH_CLIENT_FILENAME = "youtube-oauth-client.json"
YOUTUBE_TOKEN_FILENAME = "youtube-token.json"
ENV_HOME = "LIKE_SURGEON_HOME"
CONFIG_FILENAME = "config.json"

_REGION_PATTERN = re.compile(r"^[A-Z]{2}$")


class InvalidRegionError(ValueError):
    """Raised when a region value is not ISO 3166-1 alpha-2 (^[A-Z]{2}$).

    Surfaced fail-fast at config load and CLI flag parse so a typo (e.g.
    ``KOREA``, ``kr\\nx``, an empty string after trim) can't silently
    disable region detection — that would let a user think they were
    checking region-blocks while every video skipped the check.
    """


def _validate_region(value: str) -> str:
    """Normalize-then-validate. Strips, uppercases, then enforces the
    strict alpha-2 shape. Raises ``InvalidRegionError`` on any failure.
    """
    norm = value.strip().upper()
    if not _REGION_PATTERN.match(norm):
        raise InvalidRegionError(
            f"region must be an ISO 3166-1 alpha-2 country code (e.g. 'KR'), got {value!r}"
        )
    return norm


@dataclass(frozen=True)
class Config:
    """Resolved file-system layout for this install."""

    app_dir: Path
    db_path: Path
    ytmusic_browser_path: Path
    youtube_oauth_client_path: Path
    youtube_token_path: Path

    @classmethod
    def load(cls) -> Config:
        env = os.environ.get(ENV_HOME)
        app_dir = Path(env).expanduser() if env else Path.home() / DEFAULT_APP_DIR_NAME
        return cls(
            app_dir=app_dir,
            db_path=app_dir / DEFAULT_DB_FILENAME,
            ytmusic_browser_path=app_dir / YTMUSIC_BROWSER_FILENAME,
            youtube_oauth_client_path=app_dir / YOUTUBE_OAUTH_CLIENT_FILENAME,
            youtube_token_path=app_dir / YOUTUBE_TOKEN_FILENAME,
        )

    def ensure_app_dir(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
