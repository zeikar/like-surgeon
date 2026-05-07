"""Tests for YTMusicClient — fake ytmusicapi via subclass."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from likesurgeon.ytmusic_client import (
    AuthFileMissingError,
    UnexpectedResponseError,
    YTMusicClient,
)


class _FakeYTMusic:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.last_limit: int | None = None

    def get_liked_songs(self, limit: int) -> Any:
        self.last_limit = limit
        return self._payload


class _FakeClient(YTMusicClient):
    """YTMusicClient that bypasses auth and returns a canned payload."""

    def __init__(self, payload: Any) -> None:
        super().__init__(browser_path=None)
        self._payload = payload
        self.fake: _FakeYTMusic | None = None

    def _build(self) -> Any:
        self.fake = _FakeYTMusic(self._payload)
        return self.fake


def test_fetch_liked_songs_returns_track_list():
    payload = {"tracks": [{"videoId": "v1", "title": "A", "artists": []}]}
    client = _FakeClient(payload)
    result = client.fetch_liked_songs(limit=42)
    assert len(result) == 1
    assert result[0]["videoId"] == "v1"
    assert client.fake is not None
    assert client.fake.last_limit == 42


def test_fetch_liked_songs_returns_empty_list_for_well_formed_empty_response():
    """A well-formed dict with an empty list of tracks is fine — that genuinely
    means 'no liked songs' and is not the same as a malformed response."""
    client = _FakeClient({"tracks": []})
    assert client.fetch_liked_songs() == []


def test_fetch_liked_songs_raises_when_tracks_key_missing():
    client = _FakeClient({})
    with pytest.raises(UnexpectedResponseError, match="missing 'tracks'"):
        client.fetch_liked_songs()


def test_fetch_liked_songs_raises_when_response_not_dict():
    client = _FakeClient([])
    with pytest.raises(UnexpectedResponseError, match="expected a dict"):
        client.fetch_liked_songs()


def test_fetch_liked_songs_raises_when_tracks_not_list():
    client = _FakeClient({"tracks": "not-a-list"})
    with pytest.raises(UnexpectedResponseError, match="'tracks' is a str"):
        client.fetch_liked_songs()


def test_missing_auth_raises():
    client = YTMusicClient(browser_path=Path("/nope/browser.json"))
    with pytest.raises(AuthFileMissingError):
        client.fetch_liked_songs()


def test_fetch_liked_songs_wraps_parse_keyerror_as_unexpected_response() -> None:
    """ytmusicapi raises a deeply-nested KeyError when the response is a
    logged-out page. Surface as our own error with re-auth guidance."""

    class _RaisingFake:
        def get_liked_songs(self, limit: int) -> Any:
            raise KeyError(
                "Unable to find 'twoColumnBrowseResultsRenderer' using path [...] on {...}"
            )

    class _Client(YTMusicClient):
        def __init__(self) -> None:
            super().__init__(browser_path=None)

        def _build(self) -> Any:
            return _RaisingFake()

    with pytest.raises(UnexpectedResponseError, match="auth ytmusic") as exc_info:
        _Client().fetch_liked_songs()
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_fetch_liked_songs_wraps_parse_indexerror_as_unexpected_response() -> None:
    """ytmusicapi's navigation.nav re-raises both KeyError and IndexError
    depending on whether the missing path element is a key or a list index.
    A logged-out response can trigger either. The boundary must catch both."""

    class _RaisingFake:
        def get_liked_songs(self, limit: int) -> Any:
            raise IndexError("Unable to find '0' using path [..., 0] on {...}")

    class _Client(YTMusicClient):
        def __init__(self) -> None:
            super().__init__(browser_path=None)

        def _build(self) -> Any:
            return _RaisingFake()

    with pytest.raises(UnexpectedResponseError, match="auth ytmusic") as exc_info:
        _Client().fetch_liked_songs()
    assert isinstance(exc_info.value.__cause__, IndexError)


class _StubCookie:
    """Minimal duck-typed stand-in for browser_cookie3's Cookie objects.

    Only ``.name`` and ``.value`` are read by ``_cookies_to_browser_json``.
    Keeping the stub here means tests don't depend on importing
    ``browser_cookie3`` (which would prompt for keychain access on macOS
    when CI runs against a real Chrome profile).
    """

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


def test_cookies_to_browser_json_builds_expected_headers() -> None:
    from likesurgeon.ytmusic_client import _cookies_to_browser_json

    cookies = [
        _StubCookie("__Secure-3PAPISID", "abc"),
        _StubCookie("SAPISID", "abc"),
        _StubCookie("__Secure-3PSID", "xyz"),
    ]
    headers = _cookies_to_browser_json(cookies)
    # Cookie header has all three names.
    cookie = headers["Cookie"]
    assert "__Secure-3PAPISID=abc" in cookie
    assert "SAPISID=abc" in cookie
    assert "__Secure-3PSID=xyz" in cookie
    # Required ytmusicapi headers present.
    assert headers["x-origin"] == "https://music.youtube.com"
    assert "User-Agent" in headers
    assert headers["Content-Type"] == "application/json"
    # ytmusicapi's `determine_auth_type` reads the Authorization header at
    # YTMusic init time — without "SAPISIDHASH" in the value it falls through
    # to OAUTH_CUSTOM_CLIENT and demands `oauth_credentials`. The hash is
    # regenerated on every actual request from the cookie, so the persisted
    # value just needs the right *prefix* for type detection.
    assert headers["Authorization"].startswith("SAPISIDHASH ")


def test_cookies_to_browser_json_rejects_missing_3papisid() -> None:
    """ytmusicapi's `sapisid_from_cookie` reads `__Secure-3PAPISID` to compute
    the SAPISIDHASH on every request. Without it the auth file is unusable —
    surface that early with a clear message."""
    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        _cookies_to_browser_json,
    )

    cookies = [_StubCookie("SAPISID", "abc"), _StubCookie("__Secure-3PSID", "xyz")]
    with pytest.raises(CookieExtractionError, match="__Secure-3PAPISID"):
        _cookies_to_browser_json(cookies)


def test_cookies_to_browser_json_rejects_empty() -> None:
    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        _cookies_to_browser_json,
    )

    with pytest.raises(CookieExtractionError, match="No youtube"):
        _cookies_to_browser_json([])


def test_extract_youtube_cookies_rejects_unsupported_browser() -> None:
    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        extract_youtube_cookies,
    )

    with pytest.raises(CookieExtractionError, match="Unsupported browser"):
        extract_youtube_cookies("netscape")


def test_extract_youtube_cookies_dispatches_to_browser_cookie3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper should call ``browser_cookie3.<name>(domain_name="youtube.com")``
    for the named browser and return the resulting jar as a list."""
    import browser_cookie3

    from likesurgeon.ytmusic_client import extract_youtube_cookies

    fake_jar = [_StubCookie("__Secure-3PAPISID", "x")]
    captured: dict[str, Any] = {}

    def fake_chrome(**kwargs: Any) -> list[Any]:
        captured["kwargs"] = kwargs
        return fake_jar

    monkeypatch.setattr(browser_cookie3, "chrome", fake_chrome)
    cookies = extract_youtube_cookies("chrome")
    assert cookies == fake_jar
    assert captured["kwargs"] == {"domain_name": "youtube.com"}


def test_extract_youtube_cookies_wraps_browser_cookie_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decrypt / DB-locked failures from browser_cookie3 should surface as
    our own CookieExtractionError so the CLI's `_fail` can handle them."""
    import browser_cookie3

    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        extract_youtube_cookies,
    )

    def boom(**kwargs: Any) -> list[Any]:
        raise browser_cookie3.BrowserCookieError("locked")

    monkeypatch.setattr(browser_cookie3, "firefox", boom)
    with pytest.raises(CookieExtractionError, match="firefox"):
        extract_youtube_cookies("firefox")


def test_extract_youtube_cookies_wraps_arbitrary_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browser_cookie3 calls into platform-specific decrypt code and not all
    failures consolidate into BrowserCookieError — verify we wrap a generic
    OSError so the user still sees one friendly error class."""
    import browser_cookie3

    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        extract_youtube_cookies,
    )

    def boom(**kwargs: Any) -> list[Any]:
        raise OSError("Permission denied")

    monkeypatch.setattr(browser_cookie3, "edge", boom)
    with pytest.raises(CookieExtractionError, match="OSError") as exc_info:
        extract_youtube_cookies("edge")
    # Original exception preserved via __cause__ for diagnostics.
    assert isinstance(exc_info.value.__cause__, OSError)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_write_browser_json_from_browser_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end test of the orchestrator: stubbed cookies → file on disk
    with chmod 0o600 → contents parse back to a valid headers dict."""
    import json as json_lib

    import browser_cookie3

    from likesurgeon.ytmusic_client import write_browser_json_from_browser

    monkeypatch.setattr(
        browser_cookie3,
        "chrome",
        lambda **kw: [
            _StubCookie("__Secure-3PAPISID", "abc"),
            _StubCookie("SAPISID", "abc"),
        ],
    )
    target = tmp_path / "browser.json"
    write_browser_json_from_browser("chrome", target)

    assert target.exists()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600
    headers = json_lib.loads(target.read_text(encoding="utf-8"))
    assert "__Secure-3PAPISID=abc" in headers["Cookie"]
    assert headers["x-origin"] == "https://music.youtube.com"
