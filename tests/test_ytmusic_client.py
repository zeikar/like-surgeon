"""Tests for YTMusicClient — fake ytmusicapi via subclass."""

from __future__ import annotations

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
