"""Local app configuration: paths, env overrides, on-disk layout."""

from __future__ import annotations

import json
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


def _load_region(app_dir: Path) -> str | None:
    """Read ``region`` from ``app_dir/config.json``.

    Returns None for: missing file, malformed JSON, non-dict payload, or
    missing/null/non-string ``region`` key. These are "no region
    configured" cases — fall through to the unset-region warning at the
    CLI layer.

    Raises ``InvalidRegionError`` when the key IS present and IS a
    non-empty string but does NOT match ``^[A-Z]{2}$`` — that's a config
    typo, not "unset", so we fail-fast rather than silently disabling
    region detection.
    """
    cfg_path = app_dir / CONFIG_FILENAME
    if not cfg_path.exists():
        return None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    region = data.get("region")
    if region is None:
        return None
    if not isinstance(region, str) or not region.strip():
        return None
    return _validate_region(region)


@dataclass(frozen=True)
class Config:
    """Resolved file-system layout for this install."""

    app_dir: Path
    db_path: Path
    ytmusic_browser_path: Path
    youtube_oauth_client_path: Path
    youtube_token_path: Path
    region: str | None  # ISO 3166-1 alpha-2, or None when not configured

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
            region=_load_region(app_dir),
        )

    def ensure_app_dir(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
