"""Thin wrapper around ``ytmusicapi`` for testability and clear errors.

MVP 0.1 supports the **browser-header** auth flow only. OAuth is deferred —
ytmusicapi >= 1.7 requires user-supplied Google Cloud credentials wrapped in
``OAuthCredentials``, which is more UX surface than this MVP wants to expose.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError

from .fileio import write_private_text


class AuthFileMissingError(FileNotFoundError):
    """Raised when no usable auth file exists yet."""


class UnexpectedResponseError(RuntimeError):
    """Raised when a ytmusicapi read fails or returns a payload we can't
    safely interpret (server error, transport error, reshaped response).

    Surfacing this as an error (rather than silently returning ``[]``) keeps
    a 0-track snapshot from masquerading as a successful scan — that would
    make the next diff report every prior song as removed. It is also the
    single read-side failure type the ``sync`` dispatcher catches, so every
    ytmusicapi read failure must arrive wrapped as this (or as
    ``AuthFileMissingError``) to keep the continue-on-error contract.
    """


class StaleAuthError(UnexpectedResponseError):
    """A Liked songs read failed the way stale cookies fail: a response
    ytmusicapi couldn't parse (logged-out page) or a server error such as 401.
    Transport errors stay plain ``UnexpectedResponseError``: new cookies
    wouldn't fix them, so they never trigger a refresh."""


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


class MissingSongsShelfError(UnexpectedResponseError):
    """A page other than a plain channel came back without its songs shelf.
    YT Music omits the shelf from real artist pages intermittently (ytmusicapi:
    "API sometimes does not return songs"), so this is an unreadable artist,
    not a page that has no songs list."""


@dataclass(frozen=True)
class ArtistSongs:
    """One artist's songs: Top songs ids, and the raw track dicts of the
    "Show all" songs playlist (empty when the shelf has no such link)."""

    top_video_ids: tuple[str, ...]
    songs: tuple[dict[str, Any], ...]


def parse_artist_page(response: dict[str, Any]) -> tuple[list[str], str | None] | None:
    """Read ``(top song ids, Show-all browse id)`` from a raw artist ``browse``
    response, or ``None`` when a plain channel (``musicVisualHeaderRenderer``)
    has no songs shelf.

    Keys on the first ``musicShelfRenderer`` anywhere in the section list
    rather than using ``get_artist``, which raises on plain channels and on
    headerless promo carousels. The Show-all id is ``None`` when the shelf has
    no browse endpoint. Raises ``MissingSongsShelfError`` for any other page
    without the shelf, ``UnexpectedResponseError`` when the tabs path is
    missing.
    """
    try:
        sections = response["contents"]["singleColumnBrowseResultsRenderer"]["tabs"][0][
            "tabRenderer"
        ]["content"]["sectionListRenderer"]["contents"]
    except (KeyError, IndexError, TypeError) as exc:
        raise UnexpectedResponseError(
            f"Unexpected artist page response: no section list found ({type(exc).__name__}: {exc})."
        ) from exc
    for section in sections:
        shelf = section.get("musicShelfRenderer")
        if shelf is None:
            continue
        top_ids = [
            video_id
            for row in shelf.get("contents", [])
            if (
                video_id := row.get("musicResponsiveListItemRenderer", {})
                .get("playlistItemData", {})
                .get("videoId")
            )
        ]
        runs = shelf.get("title", {}).get("runs") or [{}]
        browse_id = runs[0].get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId")
        return top_ids, browse_id
    # Allow-list the plain-channel header: an unknown or missing header could
    # be an artist whose list was dropped, and must not read as "no songs".
    if "musicVisualHeaderRenderer" in (response.get("header") or {}):
        return None
    raise MissingSongsShelfError("the artist page came back without its songs list")


def _account_identity(client: YTMusic) -> str:
    """See ``YTMusicClient.account_identity``.

    The channel handle is the only unique id ytmusicapi exposes. Without one,
    a display name alone would let another account with the same name pass,
    so the name is paired with the profile photo URL (``name|photo_url``);
    a changed photo then reads as another account, which only turns the
    refresh off. Neither is guaranteed unique, so two handle-less accounts
    with the same name and photo URL still pass; accepted by the maintainer
    as a known limit over turning the refresh off for such accounts.
    """
    try:
        info = client.get_account_info()
    except Exception as exc:  # noqa: BLE001 — system-boundary catch
        raise UnexpectedResponseError(
            f"couldn't confirm the signed-in YT Music account ({type(exc).__name__}: {exc})"
        ) from exc
    if not isinstance(info, dict):
        raise UnexpectedResponseError(
            f"couldn't confirm the signed-in YT Music account (got a {type(info).__name__})"
        )
    handle, name, photo = (info.get(k) for k in ("channelHandle", "accountName", "accountPhotoUrl"))
    if isinstance(handle, str) and handle:
        return handle
    if isinstance(name, str) and name and isinstance(photo, str) and photo:
        return f"{name}|{photo}"
    raise UnexpectedResponseError(
        "couldn't confirm the signed-in YT Music account (no channel handle, "
        "and no name and profile photo to use instead)"
    )


class YTMusicClient:
    """Resolves the browser-header auth file and proxies calls.

    The wrapper exists so that:
      * tests can subclass and override ``_build`` to inject a fake YTMusic.
      * the CLI sees one clear exception when auth isn't set up yet, instead
        of leaking ytmusicapi internals.
    """

    def __init__(
        self,
        browser_path: Path | None = None,
        refresh_browser: str | None = None,
        refresh_account: str | None = None,
        on_refresh: Callable[[str], None] | None = None,
    ) -> None:
        """``refresh_browser`` / ``refresh_account``: when a Liked songs read
        fails like stale cookies do (they go stale within the hour), re-extract
        cookies from this browser once, provided it is still signed in to this
        account (``account_identity``). Both are needed; ``on_refresh(browser)``
        is told before the retry."""
        self._browser_path = browser_path
        self._refresh_browser = refresh_browser
        self._refresh_account = refresh_account
        self._on_refresh = on_refresh

    def _build(self, path: Path | None = None) -> YTMusic:
        """A YTMusic on the auth file, or on ``path`` (a refresh's candidate)."""
        path = path or self._browser_path
        if path is None or not path.exists():
            raise AuthFileMissingError(
                "No YouTube Music auth file found. "
                "Run `likesurgeon auth ytmusic` and follow the printed instructions."
            )
        try:
            return YTMusic(str(path))
        except Exception as exc:  # noqa: BLE001 — system-boundary catch
            # A corrupt/partial browser.json is "no usable auth file" too;
            # the recovery (re-run auth) is the same as the missing case.
            raise AuthFileMissingError(
                f"YouTube Music auth file {path} is unusable "
                f"({type(exc).__name__}: {exc}). Re-run `likesurgeon auth ytmusic`."
            ) from exc

    def fetch_liked_songs(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Fetch up to ``limit`` liked songs (``None``: all). Returns the raw track dicts.

        Raises ``UnexpectedResponseError`` if ytmusicapi returns anything
        other than a ``dict`` containing a list under ``"tracks"``. We'd
        rather surface "ytmusicapi behavior changed, please check" than
        silently store an empty snapshot that wipes the user's diff history.
        An *empty but well-formed* response (``{"tracks": []}``) is fine —
        that genuinely means "no liked songs."

        With ``refresh_browser`` and ``refresh_account``, a ``StaleAuthError``
        re-extracts the cookies into the auth file and the request is retried
        once; if the refresh or the retry fails, that raises
        ``UnexpectedResponseError``.
        """
        try:
            result = self._request_liked_songs(limit)
        except StaleAuthError as exc:
            browser, account = self._refresh_browser, self._refresh_account
            target = self._browser_path
            if browser is None or account is None or target is None:
                raise
            result = self._refresh_and_request(browser, account, target, limit, exc)
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

    def _request_liked_songs(self, limit: int | None) -> Any:
        """One ``get_liked_songs`` call on a fresh client, its failures wrapped
        as ``UnexpectedResponseError`` (``StaleAuthError`` when stale cookies
        are the likely cause); the payload is returned unchecked."""
        client = self._build()
        try:
            # Typed ``int`` upstream, but forwarded as-is to ``get_playlist``,
            # where ``None`` follows every continuation.
            return client.get_liked_songs(limit=limit)  # type: ignore[arg-type]
        except (KeyError, IndexError) as exc:
            # ytmusicapi's response parser (`ytmusicapi.navigation.nav`) re-raises
            # whichever of KeyError / IndexError it caught — string keys vs. list
            # indices in the navigation path. Both surface from the same failure
            # mode (logged-out / shape-changed response), so we catch both rather
            # than try to fingerprint the cause at this boundary. Recovery is
            # identical for either.
            raise StaleAuthError(
                "ytmusicapi failed to parse the liked-songs response. "
                "This usually means your ytmusicapi auth file is stale "
                "(browser-header cookies expired, or the YouTube session "
                "was signed out elsewhere), but it could also mean ytmusicapi's "
                "expected response shape has changed upstream. First try "
                "`likesurgeon auth ytmusic` to refresh; if that doesn't fix "
                "it, file an issue and include the original error."
            ) from exc
        except YTMusicServerError as exc:  # e.g. 401 on stale cookies
            raise StaleAuthError(
                f"ytmusicapi request failed ({type(exc).__name__}: {exc}). "
                "If this persists, refresh auth with `likesurgeon auth ytmusic`."
            ) from exc
        except Exception as exc:  # noqa: BLE001 — system-boundary catch
            # Transport errors, malformed JSON bodies, ...
            raise UnexpectedResponseError(
                f"ytmusicapi request failed ({type(exc).__name__}: {exc}). "
                "If this persists, refresh auth with `likesurgeon auth ytmusic`."
            ) from exc

    def account_identity(self) -> str:
        """The signed-in account's channel handle, or ``name|photo_url`` when it
        has none (see ``_account_identity``). Raises ``AuthFileMissingError``
        without a usable auth file and ``UnexpectedResponseError`` when the
        account can't be read."""
        return _account_identity(self._build())

    def _refresh_and_request(
        self, browser: str, account: str, target: Path, limit: int | None, failure: Exception
    ) -> Any:
        """Re-extract cookies from ``browser`` into the auth file ``target`` and
        repeat the request once.

        The new cookies are written to a candidate file beside ``target`` and
        moved over it only once they are confirmed to belong to ``account``: a
        browser now signed in to another account would make every later Liked
        songs read (and every sync LM check) describe that account. Any failure
        or interrupt before that leaves ``target`` untouched; a failure raises
        ``UnexpectedResponseError`` with ``failure`` as cause.
        """
        candidate: Path | None = None
        try:
            fd, name = tempfile.mkstemp(
                dir=str(target.parent), prefix=f".{target.stem}.", suffix=".candidate"
            )
            os.close(fd)
            candidate = Path(name)
            write_browser_json_from_browser(browser, candidate)
            identity = _account_identity(self._build(candidate))
            if identity != account:
                raise UnexpectedResponseError(
                    f"{browser} is now signed in to {identity}, not {account}, the account "
                    "`auth ytmusic --from-browser` recorded. If it is the same account (a new "
                    "profile photo, or an identity recorded by an older version), re-run "
                    "that command"
                )
            os.replace(candidate, target)
        # Extraction boundary: browser_cookie3 and the OS decrypt paths raise
        # anything, and the sync dispatcher only handles the two read-error types.
        except Exception as exc:  # noqa: BLE001
            raise UnexpectedResponseError(
                f"{failure} (Refreshing cookies from {browser} failed, {target} unchanged: {exc})"
            ) from failure
        finally:
            if candidate is not None:
                # Gone after the replace. Otherwise it's only a leftover temp
                # file, and an error removing it mustn't hide the real outcome.
                with contextlib.suppress(OSError):
                    candidate.unlink(missing_ok=True)
        try:
            # A failing callback fails the read rather than being ignored: the
            # dispatcher then restores and stops, which is always safe.
            if self._on_refresh is not None:
                self._on_refresh(browser)
            return self._request_liked_songs(limit)
        except Exception as retry_exc:  # noqa: BLE001 — incl. AuthFileMissingError on the new file
            raise UnexpectedResponseError(
                f"{retry_exc} (Retried once after refreshing cookies from {browser}.)"
            ) from retry_exc

    def fetch_artist_songs(
        self,
        channel_ids: Iterable[str],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[dict[str, ArtistSongs | None], dict[str, str]]:
        """Read each artist's songs; returns ``(results, errors)`` by channel id.

        ``results[id]`` is ``None`` for a plain channel without a songs shelf
        (not an error). Any other page missing its shelf is read once more
        after a pause, since the omission is intermittent. After three such
        retries in a row have failed (an outage, where retrying only slows the
        run), there are no more retries until a page returns its shelf. A
        failing artist lands in ``errors`` with its message and the rest
        continue; only a missing auth file stops the run.
        """
        ids = list(channel_ids)
        client = self._build()
        results: dict[str, ArtistSongs | None] = {}
        errors: dict[str, str] = {}
        failed_retries = 0
        for done, channel_id in enumerate(ids, start=1):
            body = {"browseId": channel_id}
            try:
                try:
                    parsed = parse_artist_page(client._send_request("browse", body))
                except MissingSongsShelfError:
                    if failed_retries >= 3:
                        raise
                    time.sleep(1.0)
                    failed_retries += 1
                    parsed = parse_artist_page(client._send_request("browse", body))
                if parsed is None:
                    results[channel_id] = None
                else:
                    failed_retries = 0
                    top_ids, browse_id = parsed
                    songs: list[dict[str, Any]] = []
                    if browse_id is not None:
                        songs = client.get_playlist(browse_id, limit=None)["tracks"]  # type: ignore[arg-type]
                    results[channel_id] = ArtistSongs(tuple(top_ids), tuple(songs))
            except Exception as exc:  # noqa: BLE001 — per-artist boundary: one bad page mustn't abort the run
                errors[channel_id] = f"{type(exc).__name__}: {exc}"
            if on_progress is not None:
                on_progress(done, len(ids))
            if done < len(ids):
                time.sleep(0.2)
        return results, errors


def write_browser_json_from_browser(browser: str, target: Path) -> None:
    """Read youtube.com cookies from ``browser``, build a ytmusicapi-compatible
    ``browser.json``, and write it to ``target`` via ``write_private_text``
    (atomic, ``0o600`` from creation on POSIX).
    """
    cookies = extract_youtube_cookies(browser)
    headers = _cookies_to_browser_json(cookies)
    write_private_text(target, json.dumps(headers, indent=2))
