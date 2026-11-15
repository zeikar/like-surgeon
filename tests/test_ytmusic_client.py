"""Tests for YTMusicClient — fake ytmusicapi via subclass."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from ytmusicapi.exceptions import YTMusicServerError

from likesurgeon.ytmusic_client import (
    ArtistSongs,
    AuthFileMissingError,
    CookieExtractionError,
    MissingSongsShelfError,
    StaleAuthError,
    UnexpectedResponseError,
    YTMusicClient,
    parse_account_menu,
    parse_artist_page,
)


class _FakeYTMusic:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.last_limit: Any = "<unset>"
        self.browse_calls: list[tuple[str, dict[str, Any]]] = []
        self.playlist_calls: list[tuple[str, Any]] = []

    def get_liked_songs(self, limit: int | None) -> Any:
        self.last_limit = limit
        return self._payload

    def get_library_playlists(self, limit: int | None) -> Any:
        self.last_limit = limit
        return self._payload

    def _send_request(self, endpoint: str, body: dict[str, Any]) -> Any:
        self.browse_calls.append((endpoint, body))
        result = self._payload[body["browseId"]]
        if isinstance(result, list):  # successive responses for one artist
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_playlist(self, playlist_id: str, limit: int | None = None) -> Any:
        """The payload's entry for ``playlist_id`` (raised if an exception),
        else a one-song playlist."""
        self.playlist_calls.append((playlist_id, limit))
        result = self._payload.get(playlist_id, {"tracks": [{"videoId": f"{playlist_id}-song"}]})
        if isinstance(result, Exception):
            raise result
        return result


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


def test_fetch_liked_songs_fetches_everything_by_default():
    """``None`` reaches ytmusicapi, whose playlist fetch then follows every
    continuation — a truncated LM can't be aligned."""
    client = _FakeClient({"tracks": []})
    client.fetch_liked_songs()
    assert client.fake is not None
    assert client.fake.last_limit is None


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


def test_build_with_corrupt_auth_file_raises_auth_file_missing(tmp_path: Path) -> None:
    """A truncated/garbled browser.json is "no usable auth file" — same
    exception (and recovery) as the missing case, not a raw JSONDecodeError."""
    bad = tmp_path / "browser.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(AuthFileMissingError, match="unusable"):
        YTMusicClient(browser_path=bad).fetch_liked_songs()


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("network down"),
        YTMusicServerError("Server returned HTTP 401: Unauthorized."),
    ],
)
def test_fetch_liked_songs_wraps_request_failures_as_unexpected_response(exc) -> None:
    """Transport errors and ytmusicapi server errors (e.g. 401 on stale
    cookies) must arrive as ``UnexpectedResponseError`` so a failed ``sync`` LM
    read stops the run cleanly instead of escaping the dispatcher loop."""

    class _RaisingFake:
        def get_liked_songs(self, limit: int) -> Any:
            raise exc

    class _Client(YTMusicClient):
        def __init__(self) -> None:
            super().__init__(browser_path=None)

        def _build(self) -> Any:
            return _RaisingFake()

    with pytest.raises(UnexpectedResponseError, match="request failed") as exc_info:
        _Client().fetch_liked_songs()
    assert exc_info.value.__cause__ is exc


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


def account_menu(account: dict[str, Any]) -> dict[str, Any]:
    """A raw ``account/account_menu`` response carrying ``account``'s
    ``accountName`` / ``channelHandle`` / ``email`` (absent keys left out)."""
    keys = {"accountName": "accountName", "channelHandle": "channelHandle", "email": "email"}
    header = {
        field: {"runs": [{"text": account[key]}]}
        for key, field in keys.items()
        if account.get(key) is not None
    }
    header["accountPhoto"] = {"thumbnails": [{"url": "https://yt3/re-signed-every-session"}]}
    return {
        "actions": [
            {
                "openPopupAction": {
                    "popup": {
                        "multiPageMenuRenderer": {"header": {"activeAccountHeaderRenderer": header}}
                    }
                }
            }
        ]
    }


class _SequenceYTMusic:
    """Fake YTMusic whose successive reads (``get_liked_songs``,
    ``get_library_playlists``, ``get_playlist``; shared across rebuilds) raise
    or return the next item of ``outcomes``; ``account`` is what the account
    menu reports (an exception is raised; a list gives successive reports)."""

    def __init__(self, outcomes: list[Any], account: Any = None) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.account = {"accountName": "Me", "channelHandle": "@me"} if account is None else account
        self.account_reads = 0

    def _next(self) -> Any:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_liked_songs(self, limit: int | None) -> Any:
        return self._next()

    def get_library_playlists(self, limit: int | None) -> Any:
        return self._next()

    def get_playlist(self, playlist_id: str, limit: int | None = None) -> Any:
        return self._next()

    def _send_request(self, endpoint: str, body: dict[str, Any]) -> Any:
        assert endpoint == "account/account_menu"
        self.account_reads += 1
        account = self.account.pop(0) if isinstance(self.account, list) else self.account
        if isinstance(account, BaseException):
            raise account
        return account_menu(account)


class _RefreshClient(YTMusicClient):
    """Every build hands out the same sequence fake; ``built_from`` records the
    auth file each build used (None: the client's own). The auth file is real:
    a refresh replaces it."""

    def __init__(self, fake: _SequenceYTMusic, auth_file: Path, **kwargs: Any) -> None:
        super().__init__(browser_path=auth_file, **kwargs)
        self.fake = fake
        self.built_from: list[Path | None] = []

    def _build(self, path: Path | None = None) -> Any:
        self.built_from.append(path)
        return self.fake


_STALE = KeyError("Unable to find 'twoColumnBrowseResultsRenderer' using path [...]")
_OLD_COOKIES = '{"Cookie": "old"}'


@pytest.fixture
def auth_file(tmp_path: Path) -> Path:
    from likesurgeon.fileio import write_private_text

    path = tmp_path / "browser.json"
    write_private_text(path, _OLD_COOKIES)
    return path


def _only_auth_file_left(auth_file: Path) -> bool:
    """No refresh candidate left behind beside the auth file."""
    return [p.name for p in auth_file.parent.iterdir()] == [auth_file.name]


@pytest.fixture
def extractions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """Fake cookie re-extraction: writes new cookies, records ``(browser, target)``."""
    calls: list[tuple[str, Path]] = []

    def extract(browser: str, target: Path) -> None:
        calls.append((browser, target))
        target.write_text('{"Cookie": "new"}', encoding="utf-8")

    monkeypatch.setattr("likesurgeon.ytmusic_client.write_browser_json_from_browser", extract)
    return calls


def _refresh_client(fake: _SequenceYTMusic, auth_file: Path, **kwargs: Any) -> _RefreshClient:
    return _RefreshClient(
        fake, auth_file, refresh_browser="chrome", refresh_account="@me", **kwargs
    )


@pytest.mark.parametrize(
    "first_failure", [_STALE, YTMusicServerError("Server returned HTTP 401: Unauthorized.")]
)
def test_fetch_liked_songs_refreshes_cookies_and_retries_once(
    extractions: list[tuple[str, Path]], auth_file: Path, first_failure: Exception
) -> None:
    fake = _SequenceYTMusic([first_failure, {"tracks": [{"videoId": "v1"}]}])
    refreshed: list[str] = []
    client = _refresh_client(fake, auth_file, on_refresh=refreshed.append)

    assert client.fetch_liked_songs() == [{"videoId": "v1"}]
    [(browser, candidate)] = extractions
    assert browser == "chrome"
    # Extracted beside browser.json and checked there before replacing it.
    assert candidate.parent == auth_file.parent and candidate != auth_file
    assert client.built_from == [None, candidate, None]
    assert refreshed == ["chrome"]
    assert fake.calls == 2
    assert auth_file.read_text(encoding="utf-8") == '{"Cookie": "new"}'
    if sys.platform != "win32":
        assert auth_file.stat().st_mode & 0o777 == 0o600
    assert _only_auth_file_left(auth_file)


_EMAIL_HASH = hashlib.sha256(b"me@example.com").hexdigest()
_NO_HANDLE = {"accountName": "Me", "channelHandle": None, "email": "Me@Example.com "}


def test_fetch_liked_songs_without_handle_matches_email_and_name(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """The email is hashed after trimming and casefolding; the photo URL
    (re-signed between sessions) plays no part."""
    fake = _SequenceYTMusic([_STALE, {"tracks": []}], account=_NO_HANDLE)
    client = _RefreshClient(
        fake, auth_file, refresh_browser="chrome", refresh_account=f"{_EMAIL_HASH}|Me"
    )
    assert client.fetch_liked_songs() == []
    assert auth_file.read_text(encoding="utf-8") == '{"Cookie": "new"}'


@pytest.mark.parametrize(
    "recorded",
    [
        f"{hashlib.sha256(b'other@example.com').hexdigest()}|Me",
        f"{_EMAIL_HASH}|Brand channel",
        "Me|https://yt3/me",
        "Me",
    ],
    ids=["same-name-other-email", "same-email-other-name", "name-photo-0.13.1", "name-0.13.0"],
)
def test_fetch_liked_songs_without_handle_needs_email_and_name(
    extractions: list[tuple[str, Path]], auth_file: Path, recorded: str
) -> None:
    """Another account with the same name, a brand account under the same
    Google login, or an identity recorded by an older version must not pass."""
    fake = _SequenceYTMusic([_STALE, {"tracks": []}], account=_NO_HANDLE)
    client = _RefreshClient(fake, auth_file, refresh_browser="chrome", refresh_account=recorded)
    with pytest.raises(UnexpectedResponseError, match="re-run that command"):
        client.fetch_liked_songs()
    assert auth_file.read_text(encoding="utf-8") == _OLD_COOKIES
    assert fake.calls == 1


def test_fetch_liked_songs_without_handle_or_email_is_not_refreshed(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    fake = _SequenceYTMusic(
        [_STALE, {"tracks": []}], account={"accountName": "Me", "channelHandle": None}
    )
    client = _RefreshClient(fake, auth_file, refresh_browser="chrome", refresh_account="Me")
    with pytest.raises(UnexpectedResponseError, match="no channel handle"):
        client.fetch_liked_songs()
    assert auth_file.read_text(encoding="utf-8") == _OLD_COOKIES
    assert _only_auth_file_left(auth_file)


def test_account_identity_without_handle_or_email_raises() -> None:
    """`auth ytmusic --from-browser` then records no browser: refresh is off."""
    client = _RefreshClient(
        _SequenceYTMusic([], account={"accountName": "Me", "channelHandle": None}),
        Path("/unused"),
    )
    with pytest.raises(UnexpectedResponseError, match="no channel handle"):
        client.account_identity()


def test_parse_account_menu_prefers_handle_and_never_stores_email() -> None:
    both = {"accountName": "Me", "channelHandle": "@me", "email": "me@example.com"}
    assert parse_account_menu(account_menu(both)) == "@me"
    identity = parse_account_menu(account_menu(_NO_HANDLE))
    assert identity == f"{_EMAIL_HASH}|Me"
    assert "example.com" not in identity


@pytest.mark.parametrize(
    "response",
    [
        {"actions": []},
        {
            "actions": [
                {
                    "openPopupAction": {
                        "popup": {
                            "multiPageMenuRenderer": {
                                "header": {"activeAccountHeaderRenderer": None}
                            }
                        }
                    }
                }
            ]
        },
    ],
    ids=["no-actions", "header-not-a-dict"],
)
def test_parse_account_menu_without_header_raises(response: dict[str, Any]) -> None:
    with pytest.raises(UnexpectedResponseError, match="no account header"):
        parse_account_menu(response)


def test_parse_account_menu_skips_malformed_runs_and_needs_an_email() -> None:
    menu = account_menu({"accountName": "Me", "email": "me@example.com"})
    header = menu["actions"][0]["openPopupAction"]["popup"]["multiPageMenuRenderer"]["header"]
    header["activeAccountHeaderRenderer"]["accountName"]["runs"].append({"text": None})
    assert parse_account_menu(menu) == f"{_EMAIL_HASH}|Me"
    with pytest.raises(UnexpectedResponseError, match="no channel handle"):
        parse_account_menu(account_menu({"accountName": "Me", "email": "not-an-email"}))


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"refresh_browser": "chrome"}, {"refresh_account": "@me"}],
    ids=["neither", "no-account", "no-browser"],
)
def test_fetch_liked_songs_without_browser_and_account_does_not_refresh(
    extractions: list[tuple[str, Path]], auth_file: Path, kwargs: dict[str, str]
) -> None:
    fake = _SequenceYTMusic([_STALE, {"tracks": []}])
    with pytest.raises(UnexpectedResponseError, match="auth file is stale"):
        _RefreshClient(fake, auth_file, **kwargs).fetch_liked_songs()
    assert extractions == []
    assert fake.calls == 1


def test_fetch_liked_songs_missing_auth_file_is_not_refreshed(
    extractions: list[tuple[str, Path]],
) -> None:
    client = YTMusicClient(
        browser_path=Path("/nope/browser.json"), refresh_browser="chrome", refresh_account="@me"
    )
    with pytest.raises(AuthFileMissingError):
        client.fetch_liked_songs()
    assert extractions == []


def test_fetch_liked_songs_transport_error_is_not_refreshed(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """New cookies don't fix a dropped connection."""
    fake = _SequenceYTMusic([ConnectionError("network down"), {"tracks": []}])
    with pytest.raises(UnexpectedResponseError, match="network down"):
        _refresh_client(fake, auth_file).fetch_liked_songs()
    assert extractions == []


def test_fetch_liked_songs_reshaped_response_is_not_refreshed(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """A well-read payload of the wrong shape is a ytmusicapi change, not stale
    cookies: re-extracting wouldn't help."""
    fake = _SequenceYTMusic([{"no_tracks": []}, {"tracks": []}])
    with pytest.raises(UnexpectedResponseError, match="missing 'tracks'"):
        _refresh_client(fake, auth_file).fetch_liked_songs()
    assert extractions == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_fetch_liked_songs_other_account_keeps_old_cookies(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """The browser is now signed in to someone else: every later read would
    describe that account's likes, so its cookies must not stay."""
    fake = _SequenceYTMusic([_STALE, {"tracks": []}], account={"channelHandle": "@other"})
    refreshed: list[str] = []
    client = _refresh_client(fake, auth_file, on_refresh=refreshed.append)

    with pytest.raises(UnexpectedResponseError) as exc_info:
        client.fetch_liked_songs()
    message = str(exc_info.value)
    assert "auth file is stale" in message
    assert "chrome is now signed in to @other, not @me" in message
    assert auth_file.read_text(encoding="utf-8") == _OLD_COOKIES
    assert auth_file.stat().st_mode & 0o777 == 0o600
    assert _only_auth_file_left(auth_file)
    assert refreshed == []
    assert fake.calls == 1


def test_fetch_liked_songs_interrupted_refresh_leaves_auth_file_untouched(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """Ctrl-C between extraction and the account check: the unverified
    cookies never reach browser.json."""
    before = auth_file.read_bytes()
    fake = _SequenceYTMusic([_STALE, {"tracks": []}], account=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _refresh_client(fake, auth_file).fetch_liked_songs()
    assert len(extractions) == 1
    assert auth_file.read_bytes() == before
    assert _only_auth_file_left(auth_file)


def test_fetch_liked_songs_unreadable_account_keeps_old_cookies(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    fake = _SequenceYTMusic([_STALE, {"tracks": []}], account=KeyError("accountName"))
    with pytest.raises(UnexpectedResponseError, match="couldn't confirm the signed-in"):
        _refresh_client(fake, auth_file).fetch_liked_songs()
    assert auth_file.read_text(encoding="utf-8") == _OLD_COOKIES
    assert fake.calls == 1


@pytest.mark.parametrize(
    "refresh_error",
    [
        CookieExtractionError("Missing `__Secure-3PAPISID` cookie"),
        # Anything from the extraction boundary must arrive as a read error,
        # the one type the sync dispatcher's LM check handles.
        PermissionError("Permission denied: 'browser.json'"),
        RuntimeError("keychain locked"),
        AttributeError("module 'browser_cookie3' has no attribute 'chrome'"),
    ],
)
def test_fetch_liked_songs_refresh_failure_raises_read_error(
    monkeypatch: pytest.MonkeyPatch, auth_file: Path, refresh_error: Exception
) -> None:
    def fail(browser: str, target: Path) -> None:
        raise refresh_error

    monkeypatch.setattr("likesurgeon.ytmusic_client.write_browser_json_from_browser", fail)
    fake = _SequenceYTMusic([_STALE, {"tracks": []}])
    refreshed: list[str] = []
    client = _refresh_client(fake, auth_file, on_refresh=refreshed.append)

    with pytest.raises(UnexpectedResponseError) as exc_info:
        client.fetch_liked_songs()
    message = str(exc_info.value)
    assert "auth file is stale" in message
    assert "Refreshing cookies from chrome failed" in message
    assert str(refresh_error) in message
    assert isinstance(exc_info.value.__cause__, UnexpectedResponseError)
    assert auth_file.read_text(encoding="utf-8") == _OLD_COOKIES
    assert _only_auth_file_left(auth_file)
    assert refreshed == []
    assert fake.calls == 1


def test_fetch_liked_songs_retry_failure_raises_after_one_refresh(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    retry_failure = YTMusicServerError("Server returned HTTP 401: Unauthorized.")
    fake = _SequenceYTMusic([_STALE, retry_failure, {"tracks": []}])
    client = _refresh_client(fake, auth_file)

    with pytest.raises(UnexpectedResponseError, match="HTTP 401") as exc_info:
        client.fetch_liked_songs()
    assert "Retried once after refreshing cookies from chrome" in str(exc_info.value)
    assert exc_info.value.__cause__.__cause__ is retry_failure
    assert len(extractions) == 1
    assert fake.calls == 2


def test_fetch_liked_songs_retry_build_failure_is_a_read_error(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """An unusable rewritten auth file fails the read with the retry note."""
    fake = _SequenceYTMusic([_STALE, {"tracks": []}])
    client = _refresh_client(fake, auth_file)
    builds = iter([fake, fake])  # first read, account check; the retry build fails

    def build(path: Path | None = None) -> Any:
        try:
            return next(builds)
        except StopIteration:
            raise AuthFileMissingError("browser.json is unusable") from None

    client._build = build  # type: ignore[method-assign]
    with pytest.raises(UnexpectedResponseError, match="unusable.*Retried once"):
        client.fetch_liked_songs()


def test_fetch_liked_songs_failing_on_refresh_fails_the_read(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """Not swallowed: a read error makes the dispatcher restore and stop."""

    def boom(browser: str) -> None:
        raise OSError("stderr closed")

    fake = _SequenceYTMusic([_STALE, {"tracks": []}])
    with pytest.raises(UnexpectedResponseError, match="stderr closed.*Retried once"):
        _refresh_client(fake, auth_file, on_refresh=boom).fetch_liked_songs()


def test_fetch_library_playlists_returns_every_playlist() -> None:
    payload = [{"playlistId": "PL1", "title": "Road trip"}, {"playlistId": "LM", "title": "Liked"}]
    client = _FakeClient(payload)
    assert client.fetch_library_playlists() == payload
    assert client.fake is not None
    assert client.fake.last_limit is None


@pytest.mark.parametrize(
    "first_failure", [_STALE, YTMusicServerError("Server returned HTTP 401: Unauthorized.")]
)
def test_fetch_library_playlists_refreshes_cookies_and_retries_once(
    extractions: list[tuple[str, Path]], auth_file: Path, first_failure: Exception
) -> None:
    fake = _SequenceYTMusic([first_failure, [{"playlistId": "PL1"}]])
    assert _refresh_client(fake, auth_file).fetch_library_playlists() == [{"playlistId": "PL1"}]
    assert len(extractions) == 1
    assert auth_file.read_text(encoding="utf-8") == '{"Cookie": "new"}'
    assert fake.calls == 2


def test_fetch_library_playlists_transport_error_is_not_refreshed(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    fake = _SequenceYTMusic([ConnectionError("network down"), [{"playlistId": "PL1"}]])
    with pytest.raises(UnexpectedResponseError, match="network down"):
        _refresh_client(fake, auth_file).fetch_library_playlists()
    assert extractions == []


def test_fetch_playlist_returns_raw_dict(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    playlist = {"id": "PL1", "tracks": [{"videoId": "v1"}]}
    fake = _SequenceYTMusic([playlist])
    assert _refresh_client(fake, auth_file).fetch_playlist("PL1") == playlist
    assert extractions == []


@pytest.mark.parametrize("result", [["tracks"], {"id": "PL1"}, {"tracks": None}])
def test_fetch_playlist_malformed_result_raises(auth_file: Path, result: Any) -> None:
    with pytest.raises(UnexpectedResponseError):
        _refresh_client(_SequenceYTMusic([result]), auth_file).fetch_playlist("PL1")


@pytest.mark.parametrize(
    "first_failure", [_STALE, YTMusicServerError("Server returned HTTP 401: Unauthorized.")]
)
def test_fetch_playlist_refreshes_cookies_and_retries_once(
    extractions: list[tuple[str, Path]], auth_file: Path, first_failure: Exception
) -> None:
    fake = _SequenceYTMusic([first_failure, {"tracks": [{"videoId": "v1"}]}])
    assert _refresh_client(fake, auth_file).fetch_playlist("PL1") == {"tracks": [{"videoId": "v1"}]}
    assert len(extractions) == 1
    assert auth_file.read_text(encoding="utf-8") == '{"Cookie": "new"}'
    assert fake.calls == 2


def test_fetch_playlist_transport_error_is_not_refreshed(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    fake = _SequenceYTMusic([ConnectionError("network down"), {"tracks": []}])
    with pytest.raises(UnexpectedResponseError, match="network down"):
        _refresh_client(fake, auth_file).fetch_playlist("PL1")
    assert extractions == []


def test_fetch_playlist_missing_auth_file_is_not_refreshed(
    extractions: list[tuple[str, Path]],
) -> None:
    client = YTMusicClient(
        browser_path=Path("/nope/browser.json"), refresh_browser="chrome", refresh_account="@me"
    )
    with pytest.raises(AuthFileMissingError):
        client.fetch_playlist("PL1")
    assert extractions == []


def test_fetch_library_playlists_empty_with_readable_account_is_empty(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """A signed-in account without playlists: the menu still names it."""
    fake = _SequenceYTMusic([[]])
    assert _refresh_client(fake, auth_file).fetch_library_playlists() == []
    assert fake.account_reads == 1
    assert extractions == []
    assert auth_file.read_text(encoding="utf-8") == _OLD_COOKIES


def test_fetch_library_playlists_empty_with_unreadable_account_refreshes(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    """ytmusicapi reads a logged-out library page as no playlists; an account
    menu that can't be read tells it apart from an empty library."""
    fake = _SequenceYTMusic(
        [[], [{"playlistId": "PL1"}]],
        # Stale cookies; then the refresh's check of the new cookies.
        account=[KeyError("accountName"), {"accountName": "Me", "channelHandle": "@me"}],
    )
    assert _refresh_client(fake, auth_file).fetch_library_playlists() == [{"playlistId": "PL1"}]
    assert len(extractions) == 1
    assert auth_file.read_text(encoding="utf-8") == '{"Cookie": "new"}'
    assert fake.calls == 2


def test_fetch_library_playlists_empty_with_unreadable_account_without_refresh_raises(
    extractions: list[tuple[str, Path]], auth_file: Path
) -> None:
    fake = _SequenceYTMusic([[], []], account=KeyError("accountName"))
    with pytest.raises(StaleAuthError, match="couldn't confirm the signed-in"):
        _RefreshClient(fake, auth_file).fetch_library_playlists()
    assert extractions == []
    assert fake.calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"playlists": []},
        [{"title": "no id"}],
        [{"playlistId": ""}],
        [{"playlistId": 7}],
        ["PL1"],
    ],
    ids=["not-a-list", "no-id", "empty-id", "non-string-id", "entry-not-a-dict"],
)
def test_fetch_library_playlists_reshaped_response_is_not_refreshed(
    extractions: list[tuple[str, Path]], auth_file: Path, payload: Any
) -> None:
    fake = _SequenceYTMusic([payload, [{"playlistId": "PL1"}]])
    with pytest.raises(UnexpectedResponseError, match="get_library_playlists"):
        _refresh_client(fake, auth_file).fetch_library_playlists()
    assert extractions == []
    assert fake.calls == 1


class _StubCookie:
    """Minimal duck-typed stand-in for browser_cookie3's Cookie objects.

    Only ``.name``, ``.value``, and ``.domain`` are read by
    ``_cookies_to_browser_json``. The pure-builder tests use this stub so
    they don't have to instantiate browser_cookie3's own Cookie class
    (which would touch the OS cookie store / macOS Keychain). The
    wrapper tests below still ``import browser_cookie3`` to monkeypatch
    its top-level functions, but they hand this stub class to those
    monkeypatches rather than constructing real Cookie objects.

    ``.domain`` defaults to ``.youtube.com`` so existing tests pass the
    domain-match filter without per-test boilerplate; foreign-domain
    tests override it explicitly.
    """

    def __init__(
        self,
        name: str,
        value: str,
        domain: str = ".youtube.com",
    ) -> None:
        self.name = name
        self.value = value
        self.domain = domain


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


def test_cookies_to_browser_json_drops_foreign_domains() -> None:
    """browser_cookie3's `domain_name="youtube.com"` is a SQL substring
    filter, so cookies from `notyoutube.com`, `youtube.com.evil`, or
    sibling subdomains like `accounts.youtube.com` (which a real request
    to `music.youtube.com` would NOT send) can leak through. Verify they
    are filtered out before reaching the persisted Cookie header."""
    from likesurgeon.ytmusic_client import _cookies_to_browser_json

    cookies = [
        _StubCookie("__Secure-3PAPISID", "real", domain=".youtube.com"),
        _StubCookie("subdomain-ok", "ok", domain="music.youtube.com"),
        _StubCookie("foreign-tld", "f1", domain="notyoutube.com"),
        _StubCookie("typosquat", "f2", domain="youtube.com.evil"),
        _StubCookie("sibling-only", "f3", domain="accounts.youtube.com"),
    ]
    headers = _cookies_to_browser_json(cookies)
    cookie_header = headers["Cookie"]
    assert "__Secure-3PAPISID=real" in cookie_header
    assert "subdomain-ok=ok" in cookie_header
    assert "foreign-tld" not in cookie_header
    assert "typosquat" not in cookie_header
    assert "sibling-only" not in cookie_header


def test_cookies_to_browser_json_rejects_when_only_foreign_cookies() -> None:
    """If browser_cookie3 returns only cookies that don't match
    music.youtube.com (e.g. all from a typosquatted domain), surface a
    distinct error noting the cookies were filtered — the empty-input
    message ('No youtube.com cookies') would mislead the user into
    thinking the browser DB read failed."""
    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        _cookies_to_browser_json,
    )

    with pytest.raises(CookieExtractionError, match="none are valid for"):
        _cookies_to_browser_json([_StubCookie("__Secure-3PAPISID", "x", domain="notyoutube.com")])


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_write_browser_json_from_browser_rewrite_strips_old_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing browser.json that pre-dates the secure-write change might
    sit at 0o644 (umask-derived). The rewrite path must NOT write fresh
    cookies into that 0o644 inode — the secret would briefly leak. The
    atomic temp+rename approach replaces the inode entirely, so the
    rewritten file ends up at 0o600 regardless of the prior mode.

    We can't directly observe "never world-readable mid-write" without
    inotify, but the post-condition (existing 0o644 → final 0o600 +
    fresh content) plus the rename-based implementation give us the
    guarantee: the new content was written into a 0o600 temp file, then
    atomically swapped in."""
    import json as json_lib

    import browser_cookie3

    from likesurgeon.ytmusic_client import write_browser_json_from_browser

    target = tmp_path / "browser.json"
    target.write_text("stale", encoding="utf-8")
    os.chmod(target, 0o644)
    assert target.stat().st_mode & 0o777 == 0o644  # sanity-check the precondition

    monkeypatch.setattr(
        browser_cookie3,
        "chrome",
        lambda **kw: [
            _StubCookie("__Secure-3PAPISID", "fresh"),
            _StubCookie("SAPISID", "fresh"),
        ],
    )
    write_browser_json_from_browser("chrome", target)

    mode = target.stat().st_mode & 0o777
    assert mode == 0o600
    headers = json_lib.loads(target.read_text(encoding="utf-8"))
    assert "__Secure-3PAPISID=fresh" in headers["Cookie"]


def _page(*sections: dict[str, Any]) -> dict[str, Any]:
    return {
        "contents": {
            "singleColumnBrowseResultsRenderer": {
                "tabs": [
                    {"tabRenderer": {"content": {"sectionListRenderer": {"contents": [*sections]}}}}
                ]
            }
        }
    }


def _songs_shelf(video_ids: list[str], browse_id: str | None) -> dict[str, Any]:
    title: dict[str, Any] = {"runs": [{"text": "Songs"}]}
    if browse_id is not None:
        title["runs"][0]["navigationEndpoint"] = {"browseEndpoint": {"browseId": browse_id}}
    return {
        "musicShelfRenderer": {
            "title": title,
            "contents": [
                {"musicResponsiveListItemRenderer": {"playlistItemData": {"videoId": v}}}
                for v in video_ids
            ],
        }
    }


_PROMO_CAROUSEL = {"musicCarouselShelfRenderer": {"contents": []}}


def test_parse_artist_page_reads_top_ids_and_show_all_id() -> None:
    page = _page(_songs_shelf(["t1", "t2"], "VLOLAK-fictional"))
    assert parse_artist_page(page) == (["t1", "t2"], "VLOLAK-fictional")


def test_parse_artist_page_skips_headerless_carousel_before_songs_shelf() -> None:
    page = _page(_PROMO_CAROUSEL, _songs_shelf(["t1"], "VLOLAK-fictional"))
    assert parse_artist_page(page) == (["t1"], "VLOLAK-fictional")


def test_parse_artist_page_plain_channel_has_no_songs_list() -> None:
    page = _page({"musicCarouselShelfRenderer": {"header": {}}}, _PROMO_CAROUSEL)
    page["header"] = {"musicVisualHeaderRenderer": {}}
    assert parse_artist_page(page) is None


def _artist_page_without_songs() -> dict[str, Any]:
    """A real artist page (immersive header) whose songs shelf didn't come back."""
    page = _page({"musicCarouselShelfRenderer": {"header": {}}}, _PROMO_CAROUSEL)
    page["header"] = {"musicImmersiveHeaderRenderer": {}}
    return page


def test_parse_artist_page_artist_without_songs_shelf_raises() -> None:
    with pytest.raises(MissingSongsShelfError, match="songs list"):
        parse_artist_page(_artist_page_without_songs())


@pytest.mark.parametrize("header", [None, {"musicSomeNewHeaderRenderer": {}}])
def test_parse_artist_page_unknown_header_without_songs_shelf_raises(
    header: dict[str, Any] | None,
) -> None:
    # Only the plain-channel header means "no songs list": anything else might
    # be an artist whose list was dropped, and must block its songs.
    page = _page(_PROMO_CAROUSEL)
    if header is not None:
        page["header"] = header
    with pytest.raises(MissingSongsShelfError):
        parse_artist_page(page)


def test_parse_artist_page_shelf_without_show_all_endpoint() -> None:
    assert parse_artist_page(_page(_songs_shelf(["t1"], None))) == (["t1"], None)


def test_parse_artist_page_without_tabs_path_raises() -> None:
    with pytest.raises(UnexpectedResponseError, match="artist page"):
        parse_artist_page({"responseContext": {}})


class _ArtistClient(YTMusicClient):
    def __init__(self, pages: dict[str, Any]) -> None:
        super().__init__(browser_path=None)
        self._pages = pages
        self.builds = 0
        self.fake: _FakeYTMusic | None = None

    def _build(self) -> Any:
        self.builds += 1
        self.fake = _FakeYTMusic(self._pages)
        return self.fake


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr("likesurgeon.ytmusic_client.time.sleep", calls.append)
    return calls


def test_fetch_artist_songs_reads_each_artist_with_one_build(sleeps: list[float]) -> None:
    client = _ArtistClient(
        {
            "UC1": _page(_songs_shelf(["t1"], "VLOLAK1")),
            "UC2": _page(_songs_shelf(["t2"], "VLOLAK2")),
        }
    )
    progress: list[tuple[int, int]] = []
    results, errors = client.fetch_artist_songs(
        ["UC1", "UC2"], on_progress=lambda d, t: progress.append((d, t))
    )
    assert errors == {}
    assert client.builds == 1
    assert results["UC1"] == ArtistSongs(("t1",), ({"videoId": "VLOLAK1-song"},))
    assert results["UC2"] == ArtistSongs(("t2",), ({"videoId": "VLOLAK2-song"},))
    assert client.fake is not None
    assert client.fake.playlist_calls == [("VLOLAK1", None), ("VLOLAK2", None)]
    assert progress == [(1, 2), (2, 2)]
    assert sleeps == [0.2]


def test_fetch_artist_songs_without_show_all_link_skips_playlist(sleeps: list[float]) -> None:
    client = _ArtistClient({"UC1": _page(_songs_shelf(["t1"], None))})
    results, errors = client.fetch_artist_songs(["UC1"])
    assert errors == {}
    assert results["UC1"] == ArtistSongs(("t1",), ())
    assert client.fake is not None
    assert client.fake.playlist_calls == []


def test_fetch_artist_songs_plain_channel_is_none(sleeps: list[float]) -> None:
    page = _page(_PROMO_CAROUSEL)
    page["header"] = {"musicVisualHeaderRenderer": {}}
    client = _ArtistClient({"UC1": page})
    results, errors = client.fetch_artist_songs(["UC1"])
    assert results == {"UC1": None}
    assert errors == {}
    assert client.fake is not None
    assert len(client.fake.browse_calls) == 1  # no retry
    assert sleeps == []


def test_fetch_artist_songs_artist_without_songs_shelf_is_an_error_after_one_retry(
    sleeps: list[float],
) -> None:
    client = _ArtistClient({"UC1": [_artist_page_without_songs(), _artist_page_without_songs()]})
    results, errors = client.fetch_artist_songs(["UC1"])
    assert results == {}
    assert "MissingSongsShelfError" in errors["UC1"]
    assert client.fake is not None
    assert len(client.fake.browse_calls) == 2
    assert sleeps == [1.0]


def test_fetch_artist_songs_retry_recovers_the_songs_shelf(sleeps: list[float]) -> None:
    client = _ArtistClient(
        {"UC1": [_artist_page_without_songs(), _page(_songs_shelf(["t1"], None))]}
    )
    results, errors = client.fetch_artist_songs(["UC1"])
    assert errors == {}
    assert results["UC1"] == ArtistSongs(("t1",), ())
    assert sleeps == [1.0]


def test_fetch_artist_songs_stops_retrying_after_three_failed_retries_in_a_row(
    sleeps: list[float],
) -> None:
    missing = _artist_page_without_songs()
    pages: dict[str, Any] = {f"UC{n}": [missing, missing] for n in range(1, 10)}
    pages["UC3"] = [missing, _page(_songs_shelf(["t3"], None))]  # resets the streak
    pages["UC8"] = _page(_songs_shelf(["t8"], None))  # outage over: resets it too
    client = _ArtistClient(pages)
    results, errors = client.fetch_artist_songs([f"UC{n}" for n in range(1, 10)])
    assert set(errors) == {"UC1", "UC2", "UC4", "UC5", "UC6", "UC7", "UC9"}
    assert results == {"UC3": ArtistSongs(("t3",), ()), "UC8": ArtistSongs(("t8",), ())}
    assert client.fake is not None
    reads = [body["browseId"] for _, body in client.fake.browse_calls]
    assert reads.count("UC6") == 2  # third failed retry in a row
    assert reads.count("UC7") == 1  # outage: no more retries
    assert reads.count("UC9") == 2  # retried again after UC8 read cleanly
    assert sleeps.count(1.0) == 7


def test_fetch_artist_songs_one_failing_artist_does_not_stop_the_rest(
    sleeps: list[float],
) -> None:
    client = _ArtistClient(
        {
            "UC1": KeyError("boom"),
            "UC2": YTMusicServerError("Server returned HTTP 500"),
            "UC3": {"no": "tabs"},
            "UC4": _page(_songs_shelf(["t4"], None)),
        }
    )
    results, errors = client.fetch_artist_songs(["UC1", "UC2", "UC3", "UC4"])
    assert set(errors) == {"UC1", "UC2", "UC3"}
    assert "KeyError" in errors["UC1"]
    assert "HTTP 500" in errors["UC2"]
    assert results["UC4"] == ArtistSongs(("t4",), ())
    assert "UC1" not in results


def test_fetch_artist_songs_missing_auth_raises() -> None:
    client = YTMusicClient(browser_path=Path("/nope/browser.json"))
    with pytest.raises(AuthFileMissingError):
        client.fetch_artist_songs(["UC1"])


def test_fetch_playlists_reads_each_playlist_with_one_build(sleeps: list[float]) -> None:
    road_trip = {"title": "Road trip", "tracks": [{"videoId": "v1"}]}
    client = _ArtistClient({"PL1": road_trip})  # get_playlist reads the payload by id
    results, errors = client.fetch_playlists(["PL1", "PL2"])
    assert errors == {}
    assert results == {"PL1": road_trip, "PL2": {"tracks": [{"videoId": "PL2-song"}]}}
    assert client.builds == 1
    assert client.fake is not None
    assert client.fake.playlist_calls == [("PL1", None), ("PL2", None)]
    assert sleeps == [0.2]


@pytest.mark.parametrize(
    "bad",
    [
        KeyError("contents"),
        YTMusicServerError("Server returned HTTP 500"),
        ["not", "a", "dict"],
        {"title": "no tracks"},
        {"tracks": "not-a-list"},
    ],
    ids=["parse-error", "server-error", "not-a-dict", "no-tracks", "tracks-not-a-list"],
)
def test_fetch_playlists_one_failing_playlist_does_not_stop_the_rest(
    sleeps: list[float], bad: Any
) -> None:
    client = _ArtistClient({"PL1": bad})
    results, errors = client.fetch_playlists(["PL1", "PL2"])
    assert set(errors) == {"PL1"}
    if isinstance(bad, Exception):
        assert errors["PL1"] == f"{type(bad).__name__}: {bad}"
    else:
        assert errors["PL1"].startswith("UnexpectedResponseError: ")
    assert set(results) == {"PL2"}


def test_fetch_playlists_failure_does_not_refresh_cookies(
    extractions: list[tuple[str, Path]], auth_file: Path, sleeps: list[float]
) -> None:
    """One playlist ytmusicapi can't parse isn't a reason to re-extract cookies."""
    fake = _SequenceYTMusic([_STALE, {"tracks": []}])
    client = _refresh_client(fake, auth_file)
    results, errors = client.fetch_playlists(["PL1", "PL2"])
    assert set(errors) == {"PL1"}
    assert results == {"PL2": {"tracks": []}}
    assert extractions == []
    assert client.built_from == [None]
    assert auth_file.read_text(encoding="utf-8") == _OLD_COOKIES


def test_fetch_playlists_missing_auth_raises() -> None:
    client = YTMusicClient(browser_path=Path("/nope/browser.json"))
    with pytest.raises(AuthFileMissingError):
        client.fetch_playlists(["PL1"])
