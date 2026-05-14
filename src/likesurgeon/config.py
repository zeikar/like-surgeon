"""Local app configuration: paths, env overrides, on-disk layout."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .compare import DEFAULT_FUZZY_THRESHOLD

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


class InvalidFuzzyThresholdError(ValueError):
    """Raised when a fuzzy_threshold value is not an int in [0, 100].

    Values outside this range are rejected fail-fast at config load.
    Note: lower thresholds increase false-positive drift candidates
    (pointer-drift noise); raising above 95 risks missing real drifts.
    Only integers are accepted — bool, float, str, None are rejected.
    """


def _validate_fuzzy_threshold(value: object) -> int:
    """Validate a raw config value as a fuzzy threshold int in [0, 100].

    Checks bool before int because ``isinstance(True, int)`` is True in Python.
    Raises ``InvalidFuzzyThresholdError`` with ``repr(value)`` on any failure.
    """
    if isinstance(value, bool):
        raise InvalidFuzzyThresholdError(
            f"fuzzy_threshold must be an int in [0, 100], got {value!r}"
        )
    if not isinstance(value, int):
        raise InvalidFuzzyThresholdError(
            f"fuzzy_threshold must be an int in [0, 100], got {value!r}"
        )
    if not (0 <= value <= 100):
        raise InvalidFuzzyThresholdError(
            f"fuzzy_threshold must be an int in [0, 100], got {value!r}"
        )
    return value


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


def _load_fuzzy_threshold(app_dir: Path) -> int | None:
    """Read ``fuzzy_threshold`` from ``app_dir/config.json``.

    Returns None for: missing file, malformed JSON, non-dict payload, or
    missing/null ``fuzzy_threshold`` key. For every other case (key present
    and non-null), passes the raw value to ``_validate_fuzzy_threshold``
    which raises ``InvalidFuzzyThresholdError`` on invalid types or range.
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
    value = data.get("fuzzy_threshold")
    if value is None:
        return None
    return _validate_fuzzy_threshold(value)


@dataclass(frozen=True)
class Config:
    """Resolved file-system layout for this install."""

    app_dir: Path
    db_path: Path
    ytmusic_browser_path: Path
    youtube_oauth_client_path: Path
    youtube_token_path: Path
    region: str | None  # ISO 3166-1 alpha-2, or None when not configured
    fuzzy_threshold: int  # RapidFuzz score 0–100; >= threshold counts as match

    @classmethod
    def load(cls) -> Config:
        env = os.environ.get(ENV_HOME)
        app_dir = Path(env).expanduser() if env else Path.home() / DEFAULT_APP_DIR_NAME
        loaded = _load_fuzzy_threshold(app_dir)
        return cls(
            app_dir=app_dir,
            db_path=app_dir / DEFAULT_DB_FILENAME,
            ytmusic_browser_path=app_dir / YTMUSIC_BROWSER_FILENAME,
            youtube_oauth_client_path=app_dir / YOUTUBE_OAUTH_CLIENT_FILENAME,
            youtube_token_path=app_dir / YOUTUBE_TOKEN_FILENAME,
            region=_load_region(app_dir),
            fuzzy_threshold=loaded if loaded is not None else DEFAULT_FUZZY_THRESHOLD,
        )

    def ensure_app_dir(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
