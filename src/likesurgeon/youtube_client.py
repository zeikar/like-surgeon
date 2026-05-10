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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

LIKED_VIDEOS_FALLBACK_PLAYLIST_ID = "LL"
WRITE_SCOPE = "https://www.googleapis.com/auth/youtube"
SCOPES = [WRITE_SCOPE]


class ClientSecretsMissingError(FileNotFoundError):
    """Raised when the user hasn't provided a client_secrets JSON yet."""


class AuthorizationRequiredError(RuntimeError):
    """Raised when no token exists (or a stale one cannot refresh)."""


class YouTubeWriteError(RuntimeError):
    """Raised when a write call (e.g. ``videos.rate``) fails."""

    def __init__(self, video_id: str, rating: str, message: str) -> None:
        super().__init__(f"YouTube write failed for {video_id} ({rating}): {message}")
        self.video_id = video_id
        self.rating = rating
        self.message = message


@dataclass(frozen=True)
class VideoStatus:
    """Result of a single video's availability check.

    ``is_available`` is tri-state to mirror ``SnapshotItem.is_available``:
    ``True`` (playable), ``False`` (unavailable), ``None`` (unknown — usually
    a status-check failure).
    """

    is_available: bool | None
    reason: str | None


def _token_has_write_scope(token_path: Path | None) -> bool:
    """Whether the persisted token JSON's stored ``scopes`` includes the write scope.

    Reads the file directly because ``Credentials.from_authorized_user_file``
    overrides the file's stored scopes with whatever you pass it as the
    ``scopes`` argument — so ``creds.scopes`` always reflects what the
    *caller asked for*, not what the user actually consented to. To know
    what was granted, we must inspect the JSON's persisted ``scopes`` field.

    Missing file / unreadable / malformed JSON / no ``scopes`` field all
    return False — the safe default ("not granted") matches the behavior
    of "no token at all".
    """
    import json

    if token_path is None or not token_path.exists():
        return False
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    scopes = data.get("scopes") or []
    # google-auth accepts string scopes (space-separated) too, so a token
    # JSON could legitimately store ``scopes`` as a string. Naive ``in``
    # would then do substring matching — and the readonly scope contains
    # the write scope as a prefix, so the check would falsely report
    # write access. Normalize to a list before membership testing.
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, list):
        return False
    return WRITE_SCOPE in scopes


def _is_quota_exceeded(exc: Any) -> bool:
    """Detect Google API quota exhaustion from the JSON error body.

    The HTTP-level `e.resp.reason` is just "Forbidden" for any 403, so we
    parse `e.content` (bytes) instead — the body's `error.errors[].reason`
    field is what carries `quotaExceeded` / `dailyLimitExceeded` /
    `rateLimitExceeded`.

    Defensive at every shape boundary: non-403 responses, missing bodies,
    non-UTF-8 bytes, malformed JSON, or any payload whose shape doesn't
    precisely match `{"error": {"errors": [{"reason": ...}, ...]}}` all
    return False rather than raising. We'd rather mis-classify an
    edge-case 403 as a non-quota failure (and let it fall through the
    retry/`failed` path) than abort the entire scan because Google
    returned a body we didn't predict.
    """
    import json

    if getattr(getattr(exc, "resp", None), "status", None) != 403:
        return False
    content = getattr(exc, "content", None)
    if not content:
        return False
    try:
        payload = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
    except (ValueError, UnicodeDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    if not isinstance(err, dict):
        return False
    errors = err.get("errors")
    if not isinstance(errors, list):
        return False
    quota_reasons = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
    return any(isinstance(e, dict) and e.get("reason") in quota_reasons for e in errors)


def _classify_status(
    status: dict[str, Any],
    content_details: dict[str, Any],
    user_region: str | None,
) -> VideoStatus:
    """Stage-1 status mapping for a present video resource.

    Region check runs only when ``user_region`` is provided; otherwise
    the function preserves 0.3 behavior (status-only). ``uploadStatus``
    'rejected'/'deleted' overrides region restriction because those are
    absolute, not user-region-relative. privacyStatus is deliberately
    NOT consulted: a returned private video means the caller is the
    owner (or has explicit access), so it's playable.
    """
    upload = status.get("uploadStatus")
    if upload == "rejected":
        return VideoStatus(is_available=False, reason="rejected")
    if upload == "deleted":
        return VideoStatus(is_available=False, reason="deleted")
    if user_region:
        rr = content_details.get("regionRestriction") or {}
        blocked = rr.get("blocked") or []
        allowed = rr.get("allowed") or []
        if user_region in blocked:
            return VideoStatus(is_available=False, reason="region_blocked")
        if allowed and user_region not in allowed:
            return VideoStatus(is_available=False, reason="region_blocked")
    return VideoStatus(is_available=True, reason=None)


def disambiguate_video_status(status: VideoStatus, *, snippet_title: str) -> VideoStatus:
    """Stage-2 disambiguator for `missing_from_videos_list` placeholders.

    Non-placeholder statuses pass through unchanged. The placeholder is
    rewritten using the `playlistItems.list` snippet title YouTube returns
    when the caller can't access a referenced video — `"Private video"` and
    `"Deleted video"` are documented placeholders; anything else falls
    through to the conservative `"unavailable"` reason.
    """
    if status.reason != "missing_from_videos_list":
        return status
    title = (snippet_title or "").strip()
    if title == "Private video":
        return VideoStatus(is_available=False, reason="private")
    if title == "Deleted video":
        return VideoStatus(is_available=False, reason="deleted")
    return VideoStatus(is_available=False, reason="unavailable")


def attach_video_statuses(items: list[dict[str, Any]], statuses: dict[str, VideoStatus]) -> None:
    """Mutate raw `playlistItems.list` entries in place: stage-2 disambiguate
    `missing_from_videos_list` placeholders against snippet titles, then
    inject the final status onto each item as `_likesurgeon_video_status`
    *primitive dict* (NOT the VideoStatus dataclass — see spec
    "Persistence" for why; the snapshot ingestion runs `json.dumps` on
    `rec["raw"]` and would crash on a dataclass instance).

    Items missing `video_id` are skipped (rare YT data quirk; nothing to
    look up in `statuses` for them). The function does not return; it
    mutates `items` so they can be passed straight to `create_snapshot`.

    Precondition: ``statuses`` MUST contain a key for every item whose
    ``video_id`` is non-empty. ``fetch_video_statuses`` guarantees this
    when it's fed the same video_ids the caller derived from ``items``.
    A missing key here will raise ``KeyError`` rather than silently drop
    the augmentation — that's intentional, since silent drops would let
    a partially-augmented snapshot reach the DB.
    """
    for it in items:
        snippet = it.get("snippet") or {}
        content = it.get("contentDetails") or {}
        vid = content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        if not vid:
            continue
        title = str(snippet.get("title") or "")
        final = disambiguate_video_status(statuses[vid], snippet_title=title)
        it["_likesurgeon_video_status"] = {
            "is_available": final.is_available,
            "reason": final.reason,
        }


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

        Idempotent: if a usable token already exists *with the required write
        scope*, this is a no-op. Existing readonly-only tokens (from 0.3.x)
        fall through to the consent flow so users get a fresh token with the
        write scope rather than dead-ending the loop. If a token exists but
        its refresh fails (revoked/expired), the method also falls through
        to the consent flow rather than raising — recovering from a broken
        token is exactly the user-facing purpose of ``auth youtube``.
        ``_service`` keeps the strict behavior because callers there have no
        consent flow to fall back on.
        """
        creds = self._load_token()
        token_has_write = _token_has_write_scope(self._token_path)
        if creds is not None and creds.valid and token_has_write:
            return
        if creds is not None and creds.expired and creds.refresh_token and token_has_write:
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

    def has_write_scope(self) -> bool:
        """Whether the persisted token grants the YouTube write scope.

        Used by ``sync`` as a pre-flight check — should always be True after
        the upgraded ``authorize()`` runs, but we still verify so a stale
        readonly token from 0.3.x can't surprise a user mid-write.
        """
        return _token_has_write_scope(self._token_path)

    def _load_token(self) -> Credentials | None:
        """Read the persisted token. Treats corrupt/partial JSON as "no token".

        A corrupt token file shouldn't crash the CLI with a stack trace —
        the natural recovery is to re-run ``auth youtube``, which is exactly
        what the surrounding callers do when this returns ``None``.

        **Important**: we deliberately don't pass ``SCOPES`` to
        ``from_authorized_user_file``. Doing so overrides ``creds.scopes``
        with our requested list — which then leaks into the next
        ``_save_token`` (after a refresh) and silently inflates the
        persisted ``scopes`` field. A 0.3.x readonly token would auto-
        upgrade to ``scopes: [youtube]`` in the file after one read-side
        refresh, breaking ``has_write_scope()`` even though no write
        consent was ever given. Letting google-auth load the file's
        stored scopes verbatim keeps the JSON's truth intact.
        """
        if self._token_path is None or not self._token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(self._token_path))
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

    def rate_video(self, video_id: str, rating: Literal["like", "none"]) -> None:
        """Apply a rating to a video via ``videos.rate``.

        Wraps any error from auth/build/execute (HttpError, RefreshError-
        wrapped ``AuthorizationRequiredError``, transport exceptions) as
        ``YouTubeWriteError`` so the dispatcher's continue-on-error loop
        records a per-item failed attempt instead of aborting the whole
        sync. ``_service()`` is inside the try because token refresh can
        fail at this point.
        """
        try:
            service = self._service()
            service.videos().rate(id=video_id, rating=rating).execute()
        except Exception as exc:  # noqa: BLE001 — system-boundary catch
            raise YouTubeWriteError(video_id, rating, str(exc)) from exc

    _STATUS_BATCH_SIZE = 50
    _RETRY_SLEEPS: tuple[float, ...] = (1.0, 3.0)  # delays before retry 1 and 2

    def _videos_list(self, *, ids: list[str]) -> dict[str, Any]:
        """Thin wrapper around `videos.list?part=status,contentDetails&id=<ids>`.

        Split out so tests can monkeypatch this single seam without faking
        the entire `googleapiclient` discovery surface. Production callers
        go through `fetch_video_statuses`.

        Note: `maxResults` is intentionally NOT passed. Per the official
        videos.list documentation, `maxResults` is only valid when filtering
        by `chart` or `myRating`; with the `id` filter, supplying it makes
        the live request fail. The API returns one item per requested ID
        anyway.
        """
        service = self._service()
        return service.videos().list(part="status,contentDetails", id=",".join(ids)).execute()

    def _videos_list_with_retry(self, *, ids: list[str]) -> dict[str, Any] | str:
        """Call `_videos_list` with retry on transport errors AND HTTP 5xx
        (the spec's "Network error / HTTP 5xx" bucket). Returns the
        response dict on success, or a string sentinel on failure:
        ``"quota"`` / ``"404"`` / ``"failed"``. The caller maps each
        sentinel to the right policy.

        Quota and 404 short-circuit (no retry — same condition would
        repeat). 5xx and arbitrary non-HttpError exceptions (timeouts,
        socket resets, urllib3 connection errors) are retried; the retry
        budget is bounded by ``_RETRY_SLEEPS``, so a true programming bug
        that raises a non-HTTP exception will eventually fall through to
        ``"failed"`` rather than loop forever.
        """
        import time

        from googleapiclient.errors import HttpError

        attempts: list[float] = [0.0, *self._RETRY_SLEEPS]
        for delay in attempts:
            if delay:
                time.sleep(delay)
            try:
                return self._videos_list(ids=ids)
            except HttpError as e:
                status = getattr(e.resp, "status", None)
                if _is_quota_exceeded(e):
                    return "quota"
                if status == 404:
                    return "404"
                if status is not None and 500 <= status < 600:
                    continue
                # Non-retryable HTTP error (e.g. 401 auth, 400 bad request).
                break
            except Exception:  # noqa: BLE001 — system-boundary catch
                # Transport-level error (timeout, socket reset, DNS failure,
                # etc.). Per spec, retry just like 5xx — bounded by the
                # `_RETRY_SLEEPS` budget so it can't spin indefinitely.
                continue
        # All retries exhausted (5xx/transport) or hit a non-retryable HTTP
        # error (e.g. 401 auth, 400 bad request). 404 is handled in-loop
        # with an immediate `return "404"`, so it never reaches this point.
        return "failed"

    def fetch_video_statuses(
        self, video_ids: list[str], *, user_region: str | None = None
    ) -> dict[str, VideoStatus]:
        """Stage-1 status check via batched `videos.list?part=status,contentDetails`.

        Returns one entry per input ID. See spec "Status mapping pipeline"
        and "Batch failure policy" for the full semantics.
        """
        out: dict[str, VideoStatus] = {}
        bail_remaining = False
        for start in range(0, len(video_ids), self._STATUS_BATCH_SIZE):
            chunk = video_ids[start : start + self._STATUS_BATCH_SIZE]
            if bail_remaining:
                for vid in chunk:
                    out[vid] = VideoStatus(is_available=None, reason="status_check_failed")
                continue

            resp = self._videos_list_with_retry(ids=chunk)
            if resp == "quota":
                bail_remaining = True
                for vid in chunk:
                    out[vid] = VideoStatus(is_available=None, reason="status_check_failed")
                continue
            if resp == "404":
                if len(chunk) == 1:
                    out[chunk[0]] = VideoStatus(is_available=False, reason="deleted")
                else:
                    for vid in chunk:
                        out[vid] = VideoStatus(is_available=None, reason="status_check_failed")
                continue
            if resp == "failed":
                for vid in chunk:
                    out[vid] = VideoStatus(is_available=None, reason="status_check_failed")
                continue
            # Success — `resp` is the dict.
            assert isinstance(resp, dict)
            seen: set[str] = set()
            for item in resp.get("items", []):
                vid = item["id"]
                seen.add(vid)
                out[vid] = _classify_status(
                    item.get("status") or {},
                    item.get("contentDetails") or {},
                    user_region,
                )
            for vid in chunk:
                if vid not in seen:
                    out[vid] = VideoStatus(is_available=False, reason="missing_from_videos_list")
        return out
