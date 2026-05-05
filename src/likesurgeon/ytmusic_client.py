"""Thin wrapper around ``ytmusicapi`` for testability and clear errors.

MVP 0.1 supports the **browser-header** auth flow only. OAuth is deferred —
ytmusicapi >= 1.7 requires user-supplied Google Cloud credentials wrapped in
``OAuthCredentials``, which is more UX surface than this MVP wants to expose.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ytmusicapi import YTMusic


class AuthFileMissingError(FileNotFoundError):
    """Raised when no usable auth file exists yet."""


class YTMusicClient:
    """Resolves the browser-header auth file and proxies calls.

    The wrapper exists so that:
      * tests can subclass and override ``_build`` to inject a fake YTMusic.
      * the CLI sees one clear exception when auth isn't set up yet, instead
        of leaking ytmusicapi internals.
    """

    def __init__(self, browser_path: Path | None = None) -> None:
        self._browser_path = browser_path

    def _build(self) -> YTMusic:
        if self._browser_path is not None and self._browser_path.exists():
            return YTMusic(str(self._browser_path))
        raise AuthFileMissingError(
            "No YouTube Music auth file found. "
            "Run `likesurgeon auth ytmusic` and follow the printed instructions."
        )

    def fetch_liked_songs(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Fetch up to ``limit`` liked songs. Returns the raw track dicts."""
        client = self._build()
        result = client.get_liked_songs(limit=limit)
        if isinstance(result, dict):
            tracks = result.get("tracks", [])
            return list(tracks) if isinstance(tracks, list) else []
        return []
