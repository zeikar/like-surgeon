"""Tests for YTMusicClient — fake ytmusicapi via subclass."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from likesurgeon.ytmusic_client import AuthFileMissingError, YTMusicClient


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


def test_fetch_liked_songs_handles_missing_tracks_key():
    client = _FakeClient({})
    assert client.fetch_liked_songs() == []


def test_fetch_liked_songs_handles_non_dict_response():
    client = _FakeClient([])
    assert client.fetch_liked_songs() == []


def test_missing_auth_raises():
    client = YTMusicClient(browser_path=Path("/nope/browser.json"))
    with pytest.raises(AuthFileMissingError):
        client.fetch_liked_songs()
