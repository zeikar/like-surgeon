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


class UnexpectedResponseError(RuntimeError):
    """Raised when ytmusicapi returns a payload we can't safely interpret.

    Surfacing this as an error (rather than silently returning ``[]``) keeps
    a 0-track snapshot from masquerading as a successful scan — that would
    make the next diff report every prior song as removed.
    """


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
        """Fetch up to ``limit`` liked songs. Returns the raw track dicts.

        Raises ``UnexpectedResponseError`` if ytmusicapi returns anything
        other than a ``dict`` containing a list under ``"tracks"``. We'd
        rather surface "ytmusicapi behavior changed, please check" than
        silently store an empty snapshot that wipes the user's diff history.
        An *empty but well-formed* response (``{"tracks": []}``) is fine —
        that genuinely means "no liked songs."
        """
        client = self._build()
        try:
            result = client.get_liked_songs(limit=limit)
        except (KeyError, IndexError) as exc:
            # ytmusicapi's response parser (`ytmusicapi.navigation.nav`) re-raises
            # whichever of KeyError / IndexError it caught — string keys vs. list
            # indices in the navigation path. Both surface from the same failure
            # mode (logged-out / shape-changed response), so we catch both rather
            # than try to fingerprint the cause at this boundary. Recovery is
            # identical for either.
            raise UnexpectedResponseError(
                "ytmusicapi failed to parse the liked-songs response. "
                "This usually means your ytmusicapi auth file is stale "
                "(browser-header cookies expired, or the YouTube session "
                "was signed out elsewhere), but it could also mean ytmusicapi's "
                "expected response shape has changed upstream. First try "
                "`likesurgeon auth ytmusic` to refresh; if that doesn't fix "
                "it, file an issue and include the original error."
            ) from exc
        if not isinstance(result, dict):
            raise UnexpectedResponseError(
                f"ytmusicapi.get_liked_songs returned a {type(result).__name__}, "
                "expected a dict. The library's response shape may have changed; "
                "see https://github.com/sigma67/ytmusicapi/issues."
            )
        if "tracks" not in result:
            raise UnexpectedResponseError(
                "ytmusicapi.get_liked_songs response missing 'tracks' key. "
                f"Got keys: {sorted(result)}."
            )
        tracks = result["tracks"]
        if not isinstance(tracks, list):
            raise UnexpectedResponseError(
                f"ytmusicapi.get_liked_songs 'tracks' is a "
                f"{type(tracks).__name__}, expected a list."
            )
        return list(tracks)
