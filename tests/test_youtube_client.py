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
    YouTubeWriteError,
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

    Exposes only the attributes ``_service`` / ``authorize`` read — keeps
    the test isolated from google-auth internals. ``scopes`` includes the
    write scope so the 0.4 scope-check guard isn't what triggers the
    fall-through; this fixture is specifically about the refresh-failure
    path.
    """

    valid = False
    expired = True
    refresh_token = "rt"
    scopes = ["https://www.googleapis.com/auth/youtube"]

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


def test_classify_status_region_blocked_in_blocked_list() -> None:
    """user_region appears in regionRestriction.blocked → region_blocked."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "processed", "privacyStatus": "public"}
    content = {"regionRestriction": {"blocked": ["KR", "JP"]}}
    assert _classify_status(status, content, "KR") == VideoStatus(
        is_available=False, reason="region_blocked"
    )


def test_classify_status_region_blocked_via_allowed_whitelist() -> None:
    """allowed list is a whitelist — user_region not in it → region_blocked."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "processed", "privacyStatus": "public"}
    content = {"regionRestriction": {"allowed": ["JP"]}}
    assert _classify_status(status, content, "KR") == VideoStatus(
        is_available=False, reason="region_blocked"
    )


def test_classify_status_region_allowed_when_in_allowed_list() -> None:
    """user_region in allowed list → available."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "processed", "privacyStatus": "public"}
    content = {"regionRestriction": {"allowed": ["KR", "JP"]}}
    assert _classify_status(status, content, "KR") == VideoStatus(is_available=True, reason=None)


def test_classify_status_no_region_skips_check() -> None:
    """user_region=None preserves 0.3 behavior — region restriction ignored."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "processed", "privacyStatus": "public"}
    content = {"regionRestriction": {"blocked": ["KR"]}}
    assert _classify_status(status, content, None) == VideoStatus(is_available=True, reason=None)


def test_classify_status_deleted_takes_precedence_over_region() -> None:
    """uploadStatus=deleted is absolute — region check never runs."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "deleted", "privacyStatus": "public"}
    content = {"regionRestriction": {"blocked": ["KR"]}}
    assert _classify_status(status, content, "KR") == VideoStatus(
        is_available=False, reason="deleted"
    )


class _FakeVideosRate:
    """Mimics ``service.videos().rate(id=..., rating=...).execute()``.

    Records (id, rating) per call. ``raise_with`` lets a test inject an
    exception on ``.execute()`` to drive the error-wrap path.
    """

    def __init__(self, raise_with: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise = raise_with

    def rate(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append(kwargs)
        if self._raise is not None:
            exc = self._raise

            class _RaisingRequest:
                def execute(self_inner) -> dict[str, Any]:  # noqa: N805
                    raise exc

            return _RaisingRequest()  # type: ignore[return-value]
        return _FakeRequest({})


class _FakeServiceWithVideos:
    def __init__(self, videos: _FakeVideosRate) -> None:
        self._videos = videos

    def videos(self) -> _FakeVideosRate:
        return self._videos


def test_rate_video_calls_discovery_with_id_and_rating(monkeypatch) -> None:
    """rate_video should call ``videos().rate(id=..., rating=...).execute()``
    with exactly the inputs it was handed — no implicit translation."""
    client = YouTubeClient(client_secrets_path=None, token_path=None)
    fake_videos = _FakeVideosRate()
    monkeypatch.setattr(client, "_service", lambda: _FakeServiceWithVideos(fake_videos))

    client.rate_video("vid123", "like")
    client.rate_video("vid456", "none")

    assert fake_videos.calls == [
        {"id": "vid123", "rating": "like"},
        {"id": "vid456", "rating": "none"},
    ]


def test_rate_video_wraps_http_error_as_youtube_write_error(monkeypatch) -> None:
    """``HttpError`` from googleapiclient must surface as ``YouTubeWriteError``
    carrying (video_id, rating, message) so the dispatcher can attribute
    the failure to a specific action."""
    from googleapiclient.errors import HttpError

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    err = HttpError(_FakeResp(403), b"forbidden")
    monkeypatch.setattr(
        client,
        "_service",
        lambda: _FakeServiceWithVideos(_FakeVideosRate(raise_with=err)),
    )

    with pytest.raises(YouTubeWriteError) as exc_info:
        client.rate_video("vidX", "like")
    assert exc_info.value.video_id == "vidX"
    assert exc_info.value.rating == "like"
    assert exc_info.value.__cause__ is err


class _FakeCreds:
    """Minimal ``Credentials`` stand-in for scope / validity tests.

    Only the attributes the production ``authorize`` / ``has_write_scope``
    paths read are exposed.
    """

    def __init__(
        self,
        *,
        scopes: list[str] | None,
        valid: bool = True,
        expired: bool = False,
        refresh_token: str | None = None,
    ) -> None:
        self.scopes = scopes
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token


def _write_token_json(path: Path, *, scopes: list[str]) -> None:
    """Write a real token JSON file in the shape google-auth expects.

    Tests use real files (not monkeypatched ``_load_token``) for scope
    checks because production reads the JSON's persisted ``scopes`` field
    directly — ``Credentials.from_authorized_user_file`` overrides
    ``creds.scopes`` with whatever you pass it, so a creds-based fake
    would silently bypass the bug we're guarding against.
    """
    import json

    payload = {
        "token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake-client-id",
        "client_secret": "fake-client-secret",
        "scopes": scopes,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_has_write_scope_false_for_readonly_token(tmp_path: Path) -> None:
    """Real-file regression test: a token JSON whose persisted ``scopes``
    list only has ``youtube.readonly`` must report no write scope, even
    though ``Credentials.from_authorized_user_file(SCOPES)`` would
    overwrite ``creds.scopes`` with the write scope."""
    token_path = tmp_path / "youtube-token.json"
    _write_token_json(token_path, scopes=["https://www.googleapis.com/auth/youtube.readonly"])

    c = YouTubeClient(client_secrets_path=None, token_path=token_path)
    assert c.has_write_scope() is False


def test_has_write_scope_true_after_upgrade(tmp_path: Path) -> None:
    token_path = tmp_path / "youtube-token.json"
    _write_token_json(token_path, scopes=["https://www.googleapis.com/auth/youtube"])

    c = YouTubeClient(client_secrets_path=None, token_path=token_path)
    assert c.has_write_scope() is True


def test_has_write_scope_false_when_no_token(tmp_path: Path) -> None:
    c = YouTubeClient(client_secrets_path=None, token_path=tmp_path / "does-not-exist.json")
    assert c.has_write_scope() is False


def test_has_write_scope_false_on_corrupt_token_json(tmp_path: Path) -> None:
    """Defensive: malformed JSON shouldn't crash sync's pre-flight check."""
    token_path = tmp_path / "youtube-token.json"
    token_path.write_text("{not valid json", encoding="utf-8")

    c = YouTubeClient(client_secrets_path=None, token_path=token_path)
    assert c.has_write_scope() is False


def test_has_write_scope_string_readonly_does_not_substring_match(tmp_path: Path) -> None:
    """Regression: the readonly scope string contains the write scope as
    a prefix, so a token JSON storing ``scopes`` as a string (which
    google-auth accepts as space-separated) must not trip naive ``in``
    substring matching. ``has_write_scope()`` must split-then-membership-
    test, not substring-match.
    """
    import json

    token_path = tmp_path / "youtube-token.json"
    payload = {
        "token": "fake",
        "refresh_token": "fake",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake",
        "client_secret": "fake",
        "scopes": "https://www.googleapis.com/auth/youtube.readonly",
    }
    token_path.write_text(json.dumps(payload), encoding="utf-8")

    c = YouTubeClient(client_secrets_path=None, token_path=token_path)
    assert c.has_write_scope() is False


def test_has_write_scope_string_with_write_scope_returns_true(tmp_path: Path) -> None:
    """Counterpart: a string ``scopes`` field that DOES include the write
    scope (space-separated) must still be recognized after normalization."""
    import json

    token_path = tmp_path / "youtube-token.json"
    payload = {
        "token": "fake",
        "refresh_token": "fake",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake",
        "client_secret": "fake",
        "scopes": (
            "https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/userinfo.email"
        ),
    }
    token_path.write_text(json.dumps(payload), encoding="utf-8")

    c = YouTubeClient(client_secrets_path=None, token_path=token_path)
    assert c.has_write_scope() is True


def test_load_token_preserves_stored_readonly_scope(tmp_path: Path) -> None:
    """Regression: ``_load_token`` must NOT pass our SCOPES list to
    ``Credentials.from_authorized_user_file``. Doing so would override
    ``creds.scopes`` with [youtube], which then leaks into the next
    ``_save_token`` (after a refresh) and silently inflates the persisted
    ``scopes`` field — turning a 0.3.x readonly token into a fake
    write-scoped token at the file level after one read-side scan.
    """
    token_path = tmp_path / "youtube-token.json"
    _write_token_json(token_path, scopes=["https://www.googleapis.com/auth/youtube.readonly"])

    c = YouTubeClient(client_secrets_path=None, token_path=token_path)
    creds = c._load_token()

    assert creds is not None
    assert creds.scopes == ["https://www.googleapis.com/auth/youtube.readonly"]


def test_save_after_load_preserves_readonly_scope_in_file(tmp_path: Path) -> None:
    """End-to-end regression: load a readonly token, simulate a refresh by
    re-saving the loaded creds (matches what ``_service()`` does after
    ``creds.refresh``), then verify the file's ``scopes`` field is still
    readonly. Before the fix, this test would see ``scopes: [youtube]``
    in the re-saved file and ``has_write_scope()`` would return True.
    """
    import json

    token_path = tmp_path / "youtube-token.json"
    _write_token_json(token_path, scopes=["https://www.googleapis.com/auth/youtube.readonly"])

    c = YouTubeClient(client_secrets_path=None, token_path=token_path)
    creds = c._load_token()
    assert creds is not None
    c._save_token(creds)

    persisted = json.loads(token_path.read_text(encoding="utf-8"))
    assert persisted.get("scopes") == ["https://www.googleapis.com/auth/youtube.readonly"]
    assert c.has_write_scope() is False


def test_authorize_re_runs_flow_when_token_lacks_write_scope(monkeypatch, tmp_path) -> None:
    """Critical regression test for the OAuth dead-end: a 0.3.x readonly
    token whose ``creds.valid`` is True must NOT short-circuit ``authorize``
    — we have to re-run the consent flow so the user can grant the write
    scope. Otherwise ``likesurgeon auth youtube`` is a no-op and the user
    is permanently stuck on read-only access.

    Uses a real token file for the scope check (the production code reads
    the JSON directly) plus a monkeypatched ``_load_token`` to control
    ``creds.valid`` without touching google-auth internals."""
    secrets = tmp_path / "client_secrets.json"
    secrets.write_text("{}", encoding="utf-8")  # presence-only; flow is mocked

    token_path = tmp_path / "youtube-token.json"
    _write_token_json(token_path, scopes=["https://www.googleapis.com/auth/youtube.readonly"])

    c = YouTubeClient(client_secrets_path=secrets, token_path=token_path)
    monkeypatch.setattr(
        c,
        "_load_token",
        lambda: _FakeCreds(scopes=["https://www.googleapis.com/auth/youtube.readonly"], valid=True),
    )
    monkeypatch.setattr(c, "_save_token", lambda creds: None)

    flow_calls: list[tuple[str, list[str]]] = []

    class _FakeFlow:
        def run_local_server(self, port: int = 0) -> _FakeCreds:
            return _FakeCreds(scopes=["https://www.googleapis.com/auth/youtube"])

    def fake_from_secrets(path: str, scopes: list[str]) -> _FakeFlow:
        flow_calls.append((path, scopes))
        return _FakeFlow()

    monkeypatch.setattr(
        "likesurgeon.youtube_client.InstalledAppFlow.from_client_secrets_file",
        staticmethod(fake_from_secrets),
    )

    c.authorize()

    assert len(flow_calls) == 1
    assert flow_calls[0][1] == ["https://www.googleapis.com/auth/youtube"]


def test_rate_video_wraps_authorization_required_error(monkeypatch) -> None:
    """If ``_service()`` raises (e.g. revoked token → AuthorizationRequiredError)
    or any non-HttpError surfaces, ``rate_video`` must wrap it as
    ``YouTubeWriteError`` so sync's continue-on-error loop records a per-item
    failed attempt instead of aborting the whole run."""
    from likesurgeon.youtube_client import AuthorizationRequiredError, YouTubeWriteError

    c = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake_service() -> Any:
        raise AuthorizationRequiredError("revoked")

    monkeypatch.setattr(c, "_service", fake_service)

    with pytest.raises(YouTubeWriteError) as exc_info:
        c.rate_video("VID123", "none")
    assert exc_info.value.video_id == "VID123"
    assert exc_info.value.rating == "none"
    assert "revoked" in str(exc_info.value)


def test_rate_video_wraps_transport_errors(monkeypatch) -> None:
    """Transport / SSL / connection errors during ``.execute()`` must also
    surface as ``YouTubeWriteError`` so they don't escape the dispatcher."""
    from likesurgeon.youtube_client import YouTubeWriteError

    class _ExplodingExecute:
        def execute(self) -> None:
            raise ConnectionError("network unreachable")

    class _ExplodingRate:
        def rate(self, **_kwargs: Any) -> _ExplodingExecute:
            return _ExplodingExecute()

    class _ExplodingService:
        def videos(self) -> _ExplodingRate:
            return _ExplodingRate()

    c = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(c, "_service", lambda: _ExplodingService())

    with pytest.raises(YouTubeWriteError) as exc_info:
        c.rate_video("VID456", "like")
    assert exc_info.value.video_id == "VID456"
    assert "network unreachable" in str(exc_info.value)


def test_iso8601_duration_to_seconds_typical_values() -> None:
    from likesurgeon.youtube_client import _iso8601_duration_to_seconds

    assert _iso8601_duration_to_seconds("PT4M31S") == 271
    assert _iso8601_duration_to_seconds("PT1H2M3S") == 3723
    assert _iso8601_duration_to_seconds("PT45S") == 45
    assert _iso8601_duration_to_seconds("PT0S") == 0
    assert _iso8601_duration_to_seconds("oops") is None
    assert _iso8601_duration_to_seconds("") is None
    assert _iso8601_duration_to_seconds("PT") is None


def test_fetch_canonical_metadata_batches_by_50(monkeypatch) -> None:
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    calls: list[list[str]] = []

    def fake(*, ids: list[str]) -> dict:
        calls.append(list(ids))
        return {
            "items": [
                {
                    "id": vid,
                    "snippet": {"channelId": "ch1", "title": "T"},
                    "contentDetails": {"duration": "PT1M"},
                }
                for vid in ids
            ]
        }

    monkeypatch.setattr(client, "_canonical_metadata_videos_list", fake)
    video_ids = [f"v{i}" for i in range(60)]
    client.fetch_canonical_metadata(video_ids)

    assert len(calls) == 2
    assert len(calls[0]) == 50
    assert len(calls[1]) == 10


def test_fetch_canonical_metadata_returns_dict_with_expected_fields(monkeypatch) -> None:
    from likesurgeon.compare import CanonicalMetadata
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        return {
            "items": [
                {
                    "id": "vid1",
                    "snippet": {"channelId": "ch_a", "title": "Song One"},
                    "contentDetails": {"duration": "PT3M30S"},
                },
                {
                    "id": "vid2",
                    "snippet": {"channelId": "ch_b", "title": "Song Two"},
                    "contentDetails": {"duration": "PT1H2M3S"},
                },
            ]
        }

    monkeypatch.setattr(client, "_canonical_metadata_videos_list", fake)
    result = client.fetch_canonical_metadata(["vid1", "vid2"])

    assert result["vid1"] == CanonicalMetadata(
        video_id="vid1", title="Song One", channel_id="ch_a", duration_seconds=210
    )
    assert result["vid2"] == CanonicalMetadata(
        video_id="vid2", title="Song Two", channel_id="ch_b", duration_seconds=3723
    )


def test_fetch_canonical_metadata_missing_video_id_absent_from_result(monkeypatch) -> None:
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        # Only returns vid1, not vid2
        return {
            "items": [
                {
                    "id": "vid1",
                    "snippet": {"channelId": "ch_a", "title": "Song One"},
                    "contentDetails": {"duration": "PT1M"},
                },
            ]
        }

    monkeypatch.setattr(client, "_canonical_metadata_videos_list", fake)
    result = client.fetch_canonical_metadata(["vid1", "vid2"])

    assert "vid1" in result
    assert "vid2" not in result


def test_fetch_canonical_metadata_skips_row_with_empty_channel_id(monkeypatch) -> None:
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        return {
            "items": [
                {
                    "id": "vid1",
                    "snippet": {"channelId": "", "title": "No Channel"},
                    "contentDetails": {"duration": "PT1M"},
                },
            ]
        }

    monkeypatch.setattr(client, "_canonical_metadata_videos_list", fake)
    result = client.fetch_canonical_metadata(["vid1"])

    assert "vid1" not in result


def test_fetch_canonical_metadata_skips_row_with_malformed_duration(monkeypatch) -> None:
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        return {
            "items": [
                {
                    "id": "vid1",
                    "snippet": {"channelId": "ch_a", "title": "Bad Duration"},
                    "contentDetails": {"duration": "oops"},
                },
            ]
        }

    monkeypatch.setattr(client, "_canonical_metadata_videos_list", fake)
    result = client.fetch_canonical_metadata(["vid1"])

    assert "vid1" not in result


def test_fetch_canonical_metadata_empty_input_returns_empty_dict(monkeypatch) -> None:
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    calls: list[Any] = []

    def fake(*, ids: list[str]) -> dict:
        calls.append(ids)
        return {"items": []}

    monkeypatch.setattr(client, "_canonical_metadata_videos_list", fake)
    result = client.fetch_canonical_metadata([])

    assert result == {}
    assert len(calls) == 0


def test_fetch_canonical_metadata_raises_when_batch_fails(monkeypatch) -> None:
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        raise RuntimeError("API down")

    monkeypatch.setattr(client, "_canonical_metadata_videos_list", fake)

    with pytest.raises(RuntimeError, match="API down"):
        client.fetch_canonical_metadata(["vid1"])


def test_fetch_video_statuses_passes_region_through(monkeypatch) -> None:
    """fetch_video_statuses receives user_region kwarg and threads it to
    classification — KR blocked items get region_blocked, JP-allowed get
    available, in the same response."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        return {
            "items": [
                {
                    "id": "v_kr_blocked",
                    "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                    "contentDetails": {"regionRestriction": {"blocked": ["KR"]}},
                },
                {
                    "id": "v_kr_allowed",
                    "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                    "contentDetails": {"regionRestriction": {"allowed": ["KR"]}},
                },
            ]
        }

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["v_kr_blocked", "v_kr_allowed"], user_region="KR")
    assert out["v_kr_blocked"] == VideoStatus(is_available=False, reason="region_blocked")
    assert out["v_kr_allowed"] == VideoStatus(is_available=True, reason=None)
