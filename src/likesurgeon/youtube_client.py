"""Thin wrapper around google-api-python-client for YouTube Data API v3.

The wrapper exists so that:
  * tests can subclass and override ``_service`` / ``_load_token`` to inject
    a fake API client or canned credentials.
  * the CLI gets two clear exceptions (client-secrets missing vs. token
    missing/invalid/corrupt) instead of leaking google-auth internals.

OAuth flow: the user creates an OAuth 2.0 client of type "Desktop app" in
Google Cloud Console and downloads the JSON to
``~/.like-surgeon/youtube-oauth-client.json``. Calling ``authorize()`` opens
a browser, completes the consent screen, and persists the resulting refresh
token to ``~/.like-surgeon/youtube-token.json``. Subsequent calls reuse and
silently refresh that token.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

LIKED_VIDEOS_FALLBACK_PLAYLIST_ID = "LL"
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


class ClientSecretsMissingError(FileNotFoundError):
    """Raised when the user hasn't provided a client_secrets JSON yet."""


class AuthorizationRequiredError(RuntimeError):
    """Raised when no token exists (or a stale one cannot refresh)."""


class YouTubeClient:
    def __init__(
        self,
        client_secrets_path: Path | None = None,
        token_path: Path | None = None,
    ) -> None:
        self._client_secrets_path = client_secrets_path
        self._token_path = token_path

    def authorize(self) -> None:
        """Run the InstalledApp flow once, persist the resulting token JSON.

        Idempotent: if a usable token already exists, this is a no-op. If a
        token exists but its refresh fails (revoked/expired), the method
        falls through to the consent flow rather than raising — recovering
        from a broken token is exactly the user-facing purpose of
        ``auth youtube``, so making them re-run the command would be
        circular. ``_service`` keeps the strict behavior because callers
        there have no consent flow to fall back on.
        """
        creds = self._load_token()
        if creds is not None and creds.valid:
            return
        if creds is not None and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                pass
            else:
                self._save_token(creds)
                return
        if self._client_secrets_path is None or not self._client_secrets_path.exists():
            raise ClientSecretsMissingError(
                "No YouTube OAuth client_secrets file found. "
                "Run `likesurgeon auth youtube` and follow the printed instructions."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(self._client_secrets_path), SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)

    def _load_token(self) -> Credentials | None:
        """Read the persisted token. Treats corrupt/partial JSON as "no token".

        A corrupt token file shouldn't crash the CLI with a stack trace —
        the natural recovery is to re-run ``auth youtube``, which is exactly
        what the surrounding callers do when this returns ``None``.
        """
        if self._token_path is None or not self._token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(self._token_path), SCOPES)
        except (ValueError, OSError):
            return None

    def _save_token(self, creds: Credentials) -> None:
        if self._token_path is None:
            return
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(creds.to_json(), encoding="utf-8")
        # Refresh tokens are credentials — narrow file mode on POSIX so they
        # aren't accidentally readable by other users sharing this machine.
        if sys.platform != "win32":
            os.chmod(self._token_path, 0o600)

    def _service(self) -> Any:
        """Return an authorized Data API resource. Raises if not authorized.

        ``creds.refresh`` can raise ``RefreshError`` when the refresh token
        is revoked or expired. Re-raise as ``AuthorizationRequiredError``
        so the CLI prints a guidance message instead of a google-auth
        traceback; the user's recovery is to re-run ``auth youtube``.
        """
        creds = self._load_token()
        if creds is None:
            raise AuthorizationRequiredError(
                "Not authorized with YouTube. Run `likesurgeon auth youtube` "
                "first — that command walks you through creating the OAuth "
                "client_secrets file (if missing) and runs the consent flow."
            )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as exc:
                    raise AuthorizationRequiredError(
                        "YouTube token refresh failed (token may have been revoked). "
                        "Re-run `likesurgeon auth youtube` to re-consent."
                    ) from exc
                self._save_token(creds)
            else:
                raise AuthorizationRequiredError(
                    "YouTube token is no longer valid. "
                    "Re-run `likesurgeon auth youtube` to re-consent."
                )
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def _resolve_likes_playlist_id(self, service: Any) -> str:
        """Resolve the authenticated user's "liked videos" playlist id.

        Per the YouTube Data API docs, ``channels.list(part='contentDetails',
        mine=True)`` returns the channel's ``relatedPlaylists.likes`` id.
        Using this rather than the magic string ``"LL"`` is more robust if
        Google ever rebrands the well-known fallback. The extra call costs
        1 quota unit (negligible vs. the 100 a full like-list scan uses).

        Falls back to ``"LL"`` if the channel resource doesn't expose the
        id (e.g. brand-account edge cases).
        """
        resp = service.channels().list(part="contentDetails", mine=True).execute()
        for item in resp.get("items") or []:
            likes = (item.get("contentDetails") or {}).get("relatedPlaylists", {}).get("likes")
            if likes:
                return likes
        return LIKED_VIDEOS_FALLBACK_PLAYLIST_ID

    def fetch_liked_videos(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Fetch up to ``limit`` items from the user's Liked videos playlist.

        Resolves the playlist id from ``channels.relatedPlaylists.likes``
        first, falling back to ``"LL"`` only if the channel resource lacks
        it. Paginates ``playlistItems.list``. Returns the raw item dicts
        as returned by the API; the snapshot translator handles the rest.
        """
        service = self._service()
        playlist_id = self._resolve_likes_playlist_id(service)
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(items) < limit:
            page_size = min(50, limit - len(items))
            request = service.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=page_size,
                pageToken=page_token,
            )
            response = request.execute()
            page_items = response.get("items") or []
            items.extend(page_items)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return items[:limit]
