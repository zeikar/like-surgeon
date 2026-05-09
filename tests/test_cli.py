"""Tests for cli.py — region resolution helper, --region flag wiring,
and the InvalidRegionError → _fail conversion in _bootstrap.

Each scan-wiring test uses Typer's CliRunner against a tmp_path home
(LIKE_SURGEON_HOME env override) and a FakeYouTubeClient that captures
the user_region keyword reaching fetch_video_statuses.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

# `youtube_client` carries the symbol we monkeypatch (the
# scan_youtube_likes function does `from .youtube_client import
# YouTubeClient` at call time, which resolves through this module).
import likesurgeon.youtube_client as _yt_mod
from likesurgeon.youtube_client import VideoStatus


def test_resolve_region_prefers_cli_over_config() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region("JP", "KR") == "JP"


def test_resolve_region_falls_back_to_config_when_cli_none() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region(None, "KR") == "KR"


def test_resolve_region_returns_none_when_both_none() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region(None, None) is None


class _FakeYouTubeClient:
    """Replaces YouTubeClient for CLI wiring tests. Records the
    user_region kwarg passed to fetch_video_statuses for assertions."""

    # Class-level recording state. The sentinel string distinguishes
    # "fetch_video_statuses was called with user_region=None" (legit)
    # from "fetch_video_statuses was never called" (test setup error).
    # The `_reset_fake_client` autouse fixture rewrites these before
    # each test.
    last_user_region: Any = "<unset-sentinel>"
    fetch_called: bool = False

    def __init__(self, **kwargs: Any) -> None:
        # Accept and ignore client_secrets_path / token_path / etc.
        pass

    def fetch_liked_videos(self, *, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "snippet": {"title": "Song", "channelTitle": "C", "resourceId": {"videoId": "v1"}},
                "contentDetails": {"videoId": "v1"},
            }
        ]

    def fetch_video_statuses(
        self, video_ids: list[str], *, user_region: str | None = None
    ) -> dict[str, VideoStatus]:
        type(self).last_user_region = user_region
        type(self).fetch_called = True
        return {vid: VideoStatus(is_available=True, reason=None) for vid in video_ids}


@pytest.fixture(autouse=True)
def _reset_fake_client() -> Iterable[None]:
    """Reset class-level recording state between tests."""
    _FakeYouTubeClient.last_user_region = "<unset-sentinel>"
    _FakeYouTubeClient.fetch_called = False
    yield


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point LIKE_SURGEON_HOME at tmp_path so Config.load reads a
    test-controlled directory. Required for every CLI wiring test."""
    monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def patch_youtube_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeYouTubeClient]:
    """Replace likesurgeon.youtube_client.YouTubeClient with the fake
    so the function-local `from .youtube_client import YouTubeClient`
    inside scan_youtube_likes resolves to it."""
    monkeypatch.setattr(_yt_mod, "YouTubeClient", _FakeYouTubeClient)
    return _FakeYouTubeClient


def test_scan_youtube_likes_passes_cli_region_to_fetch_video_statuses(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "youtube-likes", "--region", "JP"])

    assert result.exit_code == 0, result.output
    assert patch_youtube_client.fetch_called is True
    assert patch_youtube_client.last_user_region == "JP"


def test_scan_youtube_likes_passes_config_region_when_no_cli_flag(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    import json

    (fake_home / "config.json").write_text(json.dumps({"region": "KR"}))

    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "youtube-likes"])

    assert result.exit_code == 0, result.output
    assert patch_youtube_client.last_user_region == "KR"


def test_scan_youtube_likes_warns_once_when_region_unset(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "youtube-likes"])

    assert result.exit_code == 0, result.output
    assert patch_youtube_client.last_user_region is None
    # Warning text must be present, exactly once.
    assert result.output.count("No region configured") == 1


def test_scan_youtube_likes_rejects_invalid_region_flag(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "youtube-likes", "--region", "KOREA"])

    assert result.exit_code == 2
    assert "ISO 3166-1 alpha-2" in result.output
    assert patch_youtube_client.fetch_called is False
