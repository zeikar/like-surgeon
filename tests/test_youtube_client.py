"""Tests for YouTubeClient — fake the API resource via subclass override."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from google.auth.exceptions import RefreshError

from likesurgeon.youtube_client import (
    AuthorizationRequiredError,
    ClientSecretsMissingError,
    YouTubeClient,
)


class _FakeRequest:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def execute(self) -> dict[str, Any]:
        return self._response


class _FakePlaylistItems:
    """Mimics ``service.playlistItems().list(...).execute()``.

    ``pages`` returned in order; each call records its kwargs in ``calls``.
    """

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append(kwargs)
        if not self._pages:
            return _FakeRequest({"items": []})
        return _FakeRequest(self._pages.pop(0))


class _FakeChannels:
    """Mimics ``service.channels().list(...).execute()``."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append(kwargs)
        return _FakeRequest(self._response)


class _FakeService:
    def __init__(
        self,
        playlist_items: _FakePlaylistItems,
        channels: _FakeChannels,
    ) -> None:
        self._pi = playlist_items
        self._channels = channels

    def playlistItems(self) -> _FakePlaylistItems:  # noqa: N802 (mimics google API)
        return self._pi

    def channels(self) -> _FakeChannels:
        return self._channels


class _FakeClient(YouTubeClient):
    def __init__(
        self,
        pages: list[dict[str, Any]],
        *,
        likes_playlist_id: str = "LL_resolved",
    ) -> None:
        super().__init__(client_secrets_path=None, token_path=None)
        self._fake_pi = _FakePlaylistItems(pages)
        self._fake_channels = _FakeChannels(
            {"items": [{"contentDetails": {"relatedPlaylists": {"likes": likes_playlist_id}}}]}
        )

    def _service(self) -> Any:  # type: ignore[override]
        return _FakeService(self._fake_pi, self._fake_channels)


def test_fetch_liked_videos_returns_items_from_single_page():
    pages = [{"items": [{"snippet": {"title": "A", "resourceId": {"videoId": "v1"}}}]}]
    c = _FakeClient(pages)
    result = c.fetch_liked_videos(limit=10)
    assert len(result) == 1
    assert result[0]["snippet"]["title"] == "A"


def test_fetch_liked_videos_paginates():
    pages = [
        {
            "items": [{"snippet": {"title": f"T{i}"}} for i in range(50)],
            "nextPageToken": "page2",
        },
        {"items": [{"snippet": {"title": f"T{i}"}} for i in range(50, 75)]},
    ]
    c = _FakeClient(pages)
    result = c.fetch_liked_videos(limit=200)
    assert len(result) == 75
    # The second call should have passed pageToken="page2".
    assert c._fake_pi.calls[1]["pageToken"] == "page2"


def test_fetch_liked_videos_uses_resolved_playlist_id():
    """playlistItems.list should target the id resolved from
    channels.contentDetails.relatedPlaylists.likes, not the magic 'LL'."""
    pages = [{"items": [{"snippet": {"title": "X"}}]}]
    c = _FakeClient(pages, likes_playlist_id="LL_real_id")
    c.fetch_liked_videos(limit=10)
    assert c._fake_pi.calls[0]["playlistId"] == "LL_real_id"
    # And the channels resolver was actually consulted.
    assert c._fake_channels.calls[0]["mine"] is True
    assert c._fake_channels.calls[0]["part"] == "contentDetails"


def test_fetch_liked_videos_respects_limit():
    pages = [{"items": [{"snippet": {"title": f"T{i}"}} for i in range(50)]}]
    c = _FakeClient(pages)
    result = c.fetch_liked_videos(limit=10)
    assert len(result) == 10


def test_fetch_liked_videos_handles_empty_page():
    pages = [{}]
    c = _FakeClient(pages)
    assert c.fetch_liked_videos() == []


def test_authorize_without_client_secrets_raises():
    c = YouTubeClient(
        client_secrets_path=Path("/nope/oauth.json"),
        token_path=Path("/nope/token.json"),
    )
    with pytest.raises(ClientSecretsMissingError):
        c.authorize()


def test_service_without_token_raises():
    c = YouTubeClient(
        client_secrets_path=Path("/nope/oauth.json"),
        token_path=Path("/nope/token.json"),
    )
    # _service is what fetch_liked_videos calls internally.
    with pytest.raises(AuthorizationRequiredError):
        c._service()


def test_load_token_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    """A partial / mangled token file should look like 'no token' to callers
    instead of crashing the CLI with a stack trace from google-auth."""
    token_path = tmp_path / "youtube-token.json"
    token_path.write_text("{not valid json", encoding="utf-8")
    c = YouTubeClient(client_secrets_path=None, token_path=token_path)
    assert c._load_token() is None
    # _service rides on _load_token returning None; verify the wrapper
    # surfaces our own exception rather than leaking ValueError.
    with pytest.raises(AuthorizationRequiredError):
        c._service()


class _FakeRefreshFailingCreds:
    """Minimal Credentials stand-in whose ``refresh`` always blows up.

    Exposes only the attributes ``_service`` reads — keeps the test
    isolated from google-auth internals.
    """

    valid = False
    expired = True
    refresh_token = "rt"

    def refresh(self, _request: Any) -> None:
        raise RefreshError("token revoked")


def test_service_raises_authorization_required_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(c, "_load_token", lambda: _FakeRefreshFailingCreds())
    with pytest.raises(AuthorizationRequiredError, match="refresh failed"):
        c._service()


def test_authorize_falls_through_to_consent_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authorize() is the recovery command, so a revoked refresh token
    must NOT raise — it should fall through to the consent flow. With no
    client_secrets file configured here, that fall-through ends in
    ClientSecretsMissingError, which proves we got past the refresh step
    instead of bailing on RefreshError."""
    c = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(c, "_load_token", lambda: _FakeRefreshFailingCreds())
    with pytest.raises(ClientSecretsMissingError):
        c.authorize()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_save_token_chmods_to_user_only(tmp_path: Path) -> None:
    """Persisted token files contain refresh credentials — must not be
    world/group readable on shared machines."""
    token_path = tmp_path / "youtube-token.json"
    c = YouTubeClient(client_secrets_path=None, token_path=token_path)

    class _ToJson:
        def to_json(self) -> str:
            return '{"token": "abc"}'

    c._save_token(_ToJson())  # type: ignore[arg-type]
    assert token_path.exists()
    mode = token_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_video_status_dataclass_field_types() -> None:
    """VideoStatus mirrors SnapshotItem's tri-state: bool | None for
    `is_available` (available / unavailable / unknown) and str | None
    for `reason`."""
    from likesurgeon.youtube_client import VideoStatus

    available = VideoStatus(is_available=True, reason=None)
    unavailable = VideoStatus(is_available=False, reason="deleted")
    unknown = VideoStatus(is_available=None, reason="status_check_failed")
    assert available.is_available is True
    assert unavailable.is_available is False
    assert unknown.is_available is None


def test_fetch_video_statuses_maps_uploadStatus_rejected(monkeypatch) -> None:
    """`status.uploadStatus = "rejected"` → is_available=False, reason='rejected'."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake_videos_list(*, ids: list[str]) -> dict:
        return {
            "items": [
                {"id": "rej1", "status": {"uploadStatus": "rejected", "privacyStatus": "public"}},
            ]
        }

    monkeypatch.setattr(client, "_videos_list", fake_videos_list)
    out = client.fetch_video_statuses(["rej1"])
    assert out["rej1"] == VideoStatus(is_available=False, reason="rejected")


def test_fetch_video_statuses_maps_uploadStatus_deleted(monkeypatch) -> None:
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(
        client,
        "_videos_list",
        lambda *, ids: {
            "items": [
                {"id": "del1", "status": {"uploadStatus": "deleted", "privacyStatus": "public"}},
            ]
        },
    )

    out = client.fetch_video_statuses(["del1"])
    assert out["del1"] == VideoStatus(is_available=False, reason="deleted")


def test_fetch_video_statuses_maps_present_video_to_available(monkeypatch) -> None:
    """Anything not rejected/deleted, when the video resource is present,
    is treated as available — including private videos returned to the
    owner (they can still play it). See spec for false-ghost discussion."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(
        client,
        "_videos_list",
        lambda *, ids: {
            "items": [
                {"id": "pub", "status": {"uploadStatus": "processed", "privacyStatus": "public"}},
                {"id": "unl", "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"}},
                {"id": "own", "status": {"uploadStatus": "processed", "privacyStatus": "private"}},
            ]
        },
    )

    out = client.fetch_video_statuses(["pub", "unl", "own"])
    assert out["pub"] == VideoStatus(is_available=True, reason=None)
    assert out["unl"] == VideoStatus(is_available=True, reason=None)
    assert out["own"] == VideoStatus(is_available=True, reason=None)


def test_fetch_video_statuses_emits_placeholder_for_missing(monkeypatch) -> None:
    """IDs absent from the response → placeholder VideoStatus that the
    scan command will disambiguate via snippet titles. fetch_video_statuses
    NEVER emits 'private'/'deleted'/'unavailable' for missing IDs — those
    decisions belong to stage 2."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(
        client,
        "_videos_list",
        lambda *, ids: {
            "items": [
                {
                    "id": "present",
                    "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                },
            ]
        },
    )

    out = client.fetch_video_statuses(["present", "gone"])
    assert out["present"] == VideoStatus(is_available=True, reason=None)
    assert out["gone"] == VideoStatus(is_available=False, reason="missing_from_videos_list")


def test_fetch_video_statuses_batches_by_50(monkeypatch) -> None:
    """120 IDs should produce 3 calls (50 + 50 + 20). The function returns
    one entry per input ID regardless of batching."""
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    calls: list[list[str]] = []

    def fake(*, ids: list[str]) -> dict:
        calls.append(list(ids))
        return {
            "items": [
                {"id": vid, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
                for vid in ids
            ]
        }

    monkeypatch.setattr(client, "_videos_list", fake)
    video_ids = [f"v{i}" for i in range(120)]
    out = client.fetch_video_statuses(video_ids)

    assert len(calls) == 3
    assert [len(c) for c in calls] == [50, 50, 20]
    assert set(out.keys()) == set(video_ids)
