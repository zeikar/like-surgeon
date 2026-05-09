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


def test_fetch_video_statuses_retries_on_5xx(monkeypatch) -> None:
    """5xx → retry. Two failures then success → all IDs return real statuses,
    no `status_check_failed`."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    attempts = {"n": 0}

    def fake(*, ids: list[str]) -> dict:
        attempts["n"] += 1
        if attempts["n"] < 3:
            from googleapiclient.errors import HttpError

            raise HttpError(_FakeResp(503), b"server")
        return {
            "items": [
                {"id": vid, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
                for vid in ids
            ]
        }

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))  # zero-delay test

    out = client.fetch_video_statuses(["a", "b"])
    assert out["a"] == VideoStatus(is_available=True, reason=None)
    assert out["b"] == VideoStatus(is_available=True, reason=None)
    assert attempts["n"] == 3


def test_fetch_video_statuses_marks_batch_failed_after_retry_exhaustion(
    monkeypatch,
) -> None:
    """Persistent 5xx → batch items get status_check_failed, scan continues."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def always_5xx(*, ids: list[str]) -> dict:
        from googleapiclient.errors import HttpError

        raise HttpError(_FakeResp(503), b"down")

    monkeypatch.setattr(client, "_videos_list", always_5xx)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b"])
    assert out["a"] == VideoStatus(is_available=None, reason="status_check_failed")
    assert out["b"] == VideoStatus(is_available=None, reason="status_check_failed")


def test_fetch_video_statuses_quota_exceeded_short_circuits_remaining(
    monkeypatch,
) -> None:
    """403 quotaExceeded → bail entire status-check phase. First batch's
    items are real, remaining batches' items get status_check_failed."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    seen: list[list[str]] = []

    # Realistic Google API quota body — matches what `videos.list` returns
    # when the daily quota is exhausted. The HTTP `reason` header is just
    # "Forbidden"; the `quotaExceeded` reason lives inside the JSON body.
    quota_body = (
        b'{"error":{"code":403,"errors":[{"reason":"quotaExceeded","domain":"youtube.quota"}]}}'
    )

    def fake(*, ids: list[str]) -> dict:
        seen.append(list(ids))
        if len(seen) == 1:
            return {
                "items": [
                    {"id": vid, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
                    for vid in ids
                ]
            }
        from googleapiclient.errors import HttpError

        raise HttpError(_FakeResp(403), quota_body)

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_STATUS_BATCH_SIZE", 2)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b", "c", "d"])
    assert out["a"] == VideoStatus(is_available=True, reason=None)
    assert out["b"] == VideoStatus(is_available=True, reason=None)
    assert out["c"] == VideoStatus(is_available=None, reason="status_check_failed")
    assert out["d"] == VideoStatus(is_available=None, reason="status_check_failed")
    # Only the first failing batch is attempted (no retries on quota).
    assert len(seen) == 2


def test_fetch_video_statuses_retries_on_transport_error(monkeypatch) -> None:
    """Per spec 'Network error / HTTP 5xx': non-HttpError transport
    failures (TimeoutError, ConnectionResetError, etc.) retry just like
    5xx. Two failures then success → all IDs return real statuses, no
    `status_check_failed`."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    attempts = {"n": 0}

    def fake(*, ids: list[str]) -> dict:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("network down")
        return {
            "items": [
                {"id": vid, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
                for vid in ids
            ]
        }

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b"])
    assert out["a"] == VideoStatus(is_available=True, reason=None)
    assert out["b"] == VideoStatus(is_available=True, reason=None)
    assert attempts["n"] == 3


def test_fetch_video_statuses_403_with_unexpected_body_falls_through_to_failed(
    monkeypatch,
) -> None:
    """403 whose body isn't the documented `{"error": {"errors": [...]}}`
    shape (e.g. plain string body, JSON with `error` as a non-dict, errors
    list of non-dicts) must NOT crash _is_quota_exceeded — it falls through
    to the generic `status_check_failed` path so one weird response can't
    abort the whole scan."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        from googleapiclient.errors import HttpError

        # Malformed: `error` is a string, not the expected dict.
        raise HttpError(_FakeResp(403), b'{"error": "Forbidden"}')

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b"])
    assert out["a"] == VideoStatus(is_available=None, reason="status_check_failed")
    assert out["b"] == VideoStatus(is_available=None, reason="status_check_failed")


def test_fetch_video_statuses_404_batch_size_one_treats_as_deleted(
    monkeypatch,
) -> None:
    """404 videoNotFound on a 1-ID batch → that ID is deleted."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        from googleapiclient.errors import HttpError

        raise HttpError(_FakeResp(404), b"not found")

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_STATUS_BATCH_SIZE", 1)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["bogus"])
    assert out["bogus"] == VideoStatus(is_available=False, reason="deleted")


def test_fetch_video_statuses_404_multi_id_batch_marks_all_failed(
    monkeypatch,
) -> None:
    """404 on a >1-ID batch is undocumented but defensive — mark whole
    batch status_check_failed without binary-splitting."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        from googleapiclient.errors import HttpError

        raise HttpError(_FakeResp(404), b"weird")

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_STATUS_BATCH_SIZE", 5)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b", "c"])
    for v in ("a", "b", "c"):
        assert out[v] == VideoStatus(is_available=None, reason="status_check_failed")


# Tiny test helper: mimics googleapiclient.errors.HttpError's resp object.
# Only `.status` is read by the production retry/quota logic — the actual
# error reason now lives in the HTTP body bytes (see _is_quota_exceeded).
class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "fail"  # set so str(HttpError) doesn't blow up if printed


def test_disambiguate_status_via_snippet_title_private() -> None:
    from likesurgeon.youtube_client import VideoStatus, disambiguate_video_status

    placeholder = VideoStatus(is_available=False, reason="missing_from_videos_list")
    out = disambiguate_video_status(placeholder, snippet_title="Private video")
    assert out == VideoStatus(is_available=False, reason="private")


def test_disambiguate_status_via_snippet_title_deleted() -> None:
    from likesurgeon.youtube_client import VideoStatus, disambiguate_video_status

    placeholder = VideoStatus(is_available=False, reason="missing_from_videos_list")
    out = disambiguate_video_status(placeholder, snippet_title="Deleted video")
    assert out == VideoStatus(is_available=False, reason="deleted")


def test_disambiguate_status_falls_through_to_unavailable() -> None:
    """Region-restricted, age-gated, weird API state → conservative
    'unavailable' rather than misclassifying."""
    from likesurgeon.youtube_client import VideoStatus, disambiguate_video_status

    placeholder = VideoStatus(is_available=False, reason="missing_from_videos_list")
    out = disambiguate_video_status(placeholder, snippet_title="K-pop hit (region-locked)")
    assert out == VideoStatus(is_available=False, reason="unavailable")


def test_disambiguate_status_passes_through_non_placeholder() -> None:
    """Non-placeholder VideoStatus is returned unchanged — disambiguation
    only applies to the missing-from-response case."""
    from likesurgeon.youtube_client import VideoStatus, disambiguate_video_status

    available = VideoStatus(is_available=True, reason=None)
    rejected = VideoStatus(is_available=False, reason="rejected")
    failed = VideoStatus(is_available=None, reason="status_check_failed")
    assert disambiguate_video_status(available, snippet_title="anything") == available
    assert disambiguate_video_status(rejected, snippet_title="Private video") == rejected
    assert disambiguate_video_status(failed, snippet_title="Deleted video") == failed


def test_attach_video_statuses_injects_disambiguated_dict() -> None:
    """End-to-end of stage-2 + injection: placeholder + 'Private video'
    title → injected dict says private; available status passes through."""
    from likesurgeon.youtube_client import VideoStatus, attach_video_statuses

    items = [
        {
            "snippet": {"title": "Private video", "resourceId": {"videoId": "p1"}},
            "contentDetails": {"videoId": "p1"},
        },
        {
            "snippet": {"title": "Real Title", "resourceId": {"videoId": "ok"}},
            "contentDetails": {"videoId": "ok"},
        },
        {
            "snippet": {"title": "no id"},  # missing video_id — skipped
            "contentDetails": {},
        },
    ]
    statuses = {
        "p1": VideoStatus(is_available=False, reason="missing_from_videos_list"),
        "ok": VideoStatus(is_available=True, reason=None),
    }
    attach_video_statuses(items, statuses)

    # Injected as PRIMITIVE DICT (not VideoStatus dataclass) so json.dumps
    # over rec["raw"] in snapshot ingestion can't crash.
    assert items[0]["_likesurgeon_video_status"] == {
        "is_available": False,
        "reason": "private",
    }
    assert items[1]["_likesurgeon_video_status"] == {
        "is_available": True,
        "reason": None,
    }
    # Item without a video_id had nothing to inject.
    assert "_likesurgeon_video_status" not in items[2]
