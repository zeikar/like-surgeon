"""Thin wrapper around ``ytmusicapi`` for testability and clear errors.

MVP 0.1 supports the **browser-header** auth flow only. OAuth is deferred —
ytmusicapi >= 1.7 requires user-supplied Google Cloud credentials wrapped in
``OAuthCredentials``, which is more UX surface than this MVP wants to expose.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable
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


class YTMusicWriteError(RuntimeError):
    """Raised when a write call (e.g. ``rate_song``) fails."""

    def __init__(self, video_id: str, message: str) -> None:
        super().__init__(f"YT Music write failed for {video_id}: {message}")
        self.video_id = video_id
        self.message = message


class CookieExtractionError(RuntimeError):
    """Raised when we can't pull usable YouTube cookies from a browser.

    Covers both "browser DB unreadable" (sandboxed, locked, decrypt failure)
    and "DB was readable but the user wasn't actually logged in" (no
    ``__Secure-3PAPISID`` cookie). Recovery is identical: log into
    music.youtube.com in the named browser and retry.
    """


# Browsers `browser_cookie3` 0.20.1 exposes as lowercase top-level functions.
# Listed explicitly so an unsupported value gives a friendly error instead
# of a generic AttributeError on the underlying lookup. Verified via probe;
# includes text-mode browsers (`w3m`, `lynx`) for completeness even though
# they're unlikely to have a usable music.youtube.com session.
SUPPORTED_BROWSERS = (
    "chrome",
    "chromium",
    "firefox",
    "edge",
    "brave",
    "safari",
    "opera",
    "opera_gx",
    "librewolf",
    "vivaldi",
    "arc",
    "w3m",
    "lynx",
)


_YTM_ORIGIN = "https://music.youtube.com"
_YTM_HOST = "music.youtube.com"


def _cookie_valid_for_music_youtube(cookie: Any) -> bool:
    """Whether a cookie's domain attribute would be sent on a request to
    https://music.youtube.com.

    `browser_cookie3` filters by a SQL `LIKE '%youtube.com%'` substring, so
    cookies from `notyoutube.com`, `youtube.com.evil`, or sibling
    subdomains (`accounts.youtube.com`, `studio.youtube.com`) can sneak
    through. Real browsers use full domain-matching: domain D matches host
    H iff `H == D` or `H` endswith `"." + D` (after stripping the leading
    dot from D, which only signals "include subdomains"). Apply that rule
    here so an unrelated `__Secure-3PAPISID` from a typosquatted host
    can't satisfy our validator.
    """
    domain = (getattr(cookie, "domain", "") or "").lstrip(".")
    if not domain:
        return False
    return domain == _YTM_HOST or _YTM_HOST.endswith("." + domain)


def _cookies_to_browser_json(cookies: Iterable[Any]) -> dict[str, str]:
    """Build a ytmusicapi-compatible browser.json header dict from cookies.

    Pure function — no I/O. Each cookie object must expose ``.name`` and
    ``.value`` (the public surface of both ``browser_cookie3``'s Cookie and
    the test stub). Validates that ``__Secure-3PAPISID`` is present, since
    ytmusicapi's ``sapisid_from_cookie`` reads exactly that name to build
    the per-request SAPISIDHASH.

    Also embeds an ``Authorization: SAPISIDHASH …`` header. ytmusicapi's
    ``determine_auth_type`` reads this *at YTMusic init time* to classify
    the file as ``AuthType.BROWSER`` — without it the file is treated as
    OAuth and the constructor errors out for missing ``oauth_credentials``.
    The hash is regenerated dynamically on every real request, so the
    value persisted to disk is only used for type detection.
    """
    from ytmusicapi.helpers import get_authorization

    raw = list(cookies)
    cookie_list = [c for c in raw if _cookie_valid_for_music_youtube(c)]
    if not cookie_list:
        if raw:
            raise CookieExtractionError(
                "Cookies were found in the browser, but none are valid for "
                "music.youtube.com (e.g. only `accounts.youtube.com` or "
                "third-party cookies). Sign into music.youtube.com in that "
                "browser, then retry."
            )
        raise CookieExtractionError(
            "No youtube.com cookies found in the browser. "
            "Make sure you're logged into music.youtube.com in that browser, "
            "then retry."
        )
    by_name = {c.name: c.value for c in cookie_list}
    if "__Secure-3PAPISID" not in by_name:
        raise CookieExtractionError(
            "Missing `__Secure-3PAPISID` cookie — that's what ytmusicapi "
            "hashes for the Authorization header. Are you actually signed in "
            "on that browser? (Sign-out wipes this cookie.)"
        )
    cookie_header = "; ".join(f"{name}={value}" for name, value in by_name.items())
    authorization = get_authorization(f"{by_name['__Secure-3PAPISID']} {_YTM_ORIGIN}")
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:88.0) Gecko/20100101 Firefox/88.0"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Content-Type": "application/json",
        "X-Goog-AuthUser": "0",
        "x-origin": _YTM_ORIGIN,
        "Authorization": authorization,
        "Cookie": cookie_header,
    }


def extract_youtube_cookies(browser: str) -> list[Any]:
    """Read youtube.com cookies from the user's named browser DB.

    Wraps ``browser_cookie3.<browser>(domain_name="youtube.com")``. The
    ``domain_name`` filter substring-matches both ``.youtube.com`` and
    ``.music.youtube.com``, which is what ytmusicapi needs.

    Raises ``CookieExtractionError`` for unsupported browser names or any
    failure pulling the cookies. ``browser_cookie3`` calls into
    platform-specific decrypt code (DPAPI on Windows, libsecret/Keychain on
    Linux/macOS) and not all failures consolidate into ``BrowserCookieError``
    — we've seen ``OSError``, ``RuntimeError``, ``sqlite3.OperationalError``
    leak through in the wild. Catch broadly at this system boundary so the
    CLI gets one named exception regardless of which OS path failed.
    """
    if browser not in SUPPORTED_BROWSERS:
        raise CookieExtractionError(
            f"Unsupported browser: {browser!r}. Choose from: " + ", ".join(SUPPORTED_BROWSERS)
        )
    import browser_cookie3

    extractor = getattr(browser_cookie3, browser)
    try:
        jar = extractor(domain_name="youtube.com")
    except Exception as exc:  # noqa: BLE001 — system-boundary catch (see docstring)
        raise CookieExtractionError(
            f"Failed to read {browser} cookies: {type(exc).__name__}: {exc}. "
            f"Make sure {browser} is installed, you're logged into "
            "music.youtube.com, and the browser DB isn't locked by a running "
            "instance (especially on Windows)."
        ) from exc
    return list(jar)


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

    def like_song(self, video_id: str) -> None:
        """Like a song on YT Music via ``rate_song(video_id, "LIKE")``.

        Wraps any error from ytmusicapi as ``YTMusicWriteError`` so the
        dispatcher can attribute failures without leaking ytmusicapi
        internals.
        """
        client = self._build()
        try:
            client.rate_song(video_id, "LIKE")
        except Exception as exc:  # noqa: BLE001 — system-boundary catch
            raise YTMusicWriteError(video_id, str(exc)) from exc

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


def write_browser_json_from_browser(browser: str, target: Path) -> None:
    """Read youtube.com cookies from ``browser``, build a ytmusicapi-compatible
    ``browser.json``, and write it to ``target`` atomically (mode ``0o600`` on
    POSIX from creation; falls back to ``Path.write_text`` on Windows where
    POSIX mode bits don't apply).

    On POSIX we write to a sibling temp file in ``target.parent`` and then
    ``os.replace`` it into place. ``tempfile.mkstemp`` creates the temp file
    with mode ``0o600`` from inception, and rename(2) preserves that mode
    onto ``target`` — so the fresh secret never lives on disk at a more
    permissive mode, even when ``target`` already existed at e.g. ``0o644``
    from an older version of this code. Same-directory rename is required
    for atomicity (cross-filesystem rename isn't atomic).
    """
    cookies = extract_youtube_cookies(browser)
    headers = _cookies_to_browser_json(cookies)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(headers, indent=2)
    if sys.platform == "win32":
        target.write_text(payload, encoding="utf-8")
        return
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".browser.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
