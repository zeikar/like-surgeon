"""Tests for cli.py — region resolution helper, --region flag wiring,
and the InvalidRegionError → _fail conversion in _bootstrap.

Each scan-wiring test uses Typer's CliRunner against a tmp_path home
(LIKE_SURGEON_HOME env override) and a FakeYouTubeClient that captures
the user_region keyword reaching fetch_video_statuses.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

# `youtube_client` carries the symbol we monkeypatch (the
# scan_youtube_likes function does `from .youtube_client import
# YouTubeClient` at call time, which resolves through this module).
import likesurgeon.youtube_client as _yt_mod
from likesurgeon.youtube_client import VideoStatus

from .fake_account import FakeAccount


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
    fetch_limit: Any = "<unset-sentinel>"
    # Defaults to the one item fetch_liked_videos returns, i.e. nothing hidden.
    likes_item_count: int = 1
    likes_item_count_error: Exception | None = None
    # `scan playlists`: items per playlist id, per-playlist errors, an error
    # fetch_playlists raises, and the ids each call received.
    constructed: bool = False
    playlists: dict[str, list[dict[str, Any]]] = {}
    playlist_errors: dict[str, str] = {}
    playlists_error: Exception | None = None
    playlist_reads: list[list[str]] = []
    status_reads: list[list[str]] = []
    statuses_error: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        # Accept and ignore client_secrets_path / token_path / etc.
        type(self).constructed = True

    def fetch_liked_videos(self, limit: int | None = None) -> list[dict[str, Any]]:
        type(self).fetch_limit = limit
        return [
            {
                "snippet": {"title": "Song", "channelTitle": "C", "resourceId": {"videoId": "v1"}},
                "contentDetails": {"videoId": "v1"},
            }
        ]

    def fetch_likes_item_count(self) -> int:
        if type(self).likes_item_count_error is not None:
            raise type(self).likes_item_count_error
        return type(self).likes_item_count

    def fetch_video_statuses(
        self, video_ids: list[str], *, user_region: str | None = None
    ) -> dict[str, VideoStatus]:
        type(self).last_user_region = user_region
        type(self).fetch_called = True
        type(self).status_reads.append(list(video_ids))
        if type(self).statuses_error is not None:
            raise type(self).statuses_error
        return {vid: VideoStatus(is_available=True, reason=None) for vid in video_ids}

    def fetch_playlists(
        self, playlist_ids: list[str]
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        type(self).playlist_reads.append(list(playlist_ids))
        if type(self).playlists_error is not None:
            raise type(self).playlists_error
        errors = {i: m for i, m in type(self).playlist_errors.items() if i in playlist_ids}
        results = {i: type(self).playlists[i] for i in playlist_ids if i not in errors}
        return results, errors


@pytest.fixture(autouse=True)
def _reset_fake_client() -> Iterable[None]:
    """Reset class-level recording state between tests."""
    _FakeYouTubeClient.last_user_region = "<unset-sentinel>"
    _FakeYouTubeClient.fetch_called = False
    _FakeYouTubeClient.fetch_limit = "<unset-sentinel>"
    _FakeYouTubeClient.likes_item_count = 1
    _FakeYouTubeClient.likes_item_count_error = None
    _FakeYouTubeClient.constructed = False
    _FakeYouTubeClient.playlists = {}
    _FakeYouTubeClient.playlist_errors = {}
    _FakeYouTubeClient.playlists_error = None
    _FakeYouTubeClient.playlist_reads = []
    _FakeYouTubeClient.status_reads = []
    _FakeYouTubeClient.statuses_error = None
    _FakeYTMusicPlaylists.library = []
    _FakeYTMusicPlaylists.library_error = None
    _FakeYTMusicPlaylists.playlists = {}
    _FakeYTMusicPlaylists.playlist_errors = {}
    _FakeYTMusicPlaylists.playlist_reads = []
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


def test_scan_youtube_likes_silent_when_playlist_count_matches(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["scan", "youtube-likes"])

    assert result.exit_code == 0, result.output
    assert "hidden" not in result.output


def test_scan_youtube_likes_warns_when_playlist_count_exceeds_items(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    patch_youtube_client.likes_item_count = 4
    result = CliRunner().invoke(app, ["scan", "youtube-likes"])

    assert result.exit_code == 0, result.output
    # Rich wraps at the terminal width, which COLUMNS can't override after import.
    flat = "".join(result.output.split())
    assert "3like(s)arehidden" in flat
    assert "docs/notes/hidden-likes.md" in flat


def test_scan_youtube_likes_survives_playlist_count_failure(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    patch_youtube_client.likes_item_count_error = RuntimeError("boom")
    result = CliRunner().invoke(app, ["scan", "youtube-likes"])

    assert result.exit_code == 0, result.output
    assert "Snapshot #" in result.output
    assert "couldnotreadthelikesplaylistcount" in "".join(result.output.split())


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


def test_scan_youtube_likes_fetches_every_liked_video(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["scan", "youtube-likes"])

    assert result.exit_code == 0, result.output
    assert patch_youtube_client.fetch_limit is None


def test_scan_ytmusic_fetches_every_liked_song(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import likesurgeon.cli as _cli_mod

    limits: list[Any] = []

    class _FakeYTMusic:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch_liked_songs(self, limit: int | None = None) -> list[dict[str, Any]]:
            limits.append(limit)
            return [{"videoId": "v1", "title": "Song", "artists": [{"name": "X"}]}]

    monkeypatch.setattr(_cli_mod, "YTMusicClient", _FakeYTMusic)

    result = CliRunner().invoke(_cli_mod.app, ["scan", "ytmusic"])

    assert result.exit_code == 0, result.output
    assert limits == [None]


@pytest.mark.parametrize("command", ["ytmusic", "youtube-likes"])
def test_scan_commands_reject_limit(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
    command: str,
) -> None:
    """A truncated scan can't be aligned (spec §5.2), so there is no --limit."""
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["scan", command, "--limit", "5"])

    assert result.exit_code == 2, result.output
    assert "No such option" in result.output
    assert patch_youtube_client.fetch_called is False


class _FakeYTMusicPlaylists:
    """``YTMusicClient`` for `scan playlists`: class-level library entries and
    ``get_playlist`` dicts by id; records the ids each ``fetch_playlists`` got."""

    library: list[dict[str, Any]] = []
    library_error: Exception | None = None
    playlists: dict[str, dict[str, Any]] = {}
    playlist_errors: dict[str, str] = {}
    playlist_reads: list[list[str]] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    def fetch_library_playlists(self) -> list[dict[str, Any]]:
        if type(self).library_error is not None:
            raise type(self).library_error
        return [dict(p) for p in type(self).library]

    def fetch_playlists(self, playlist_ids: Iterable[str]) -> tuple[dict, dict]:
        ids = list(playlist_ids)
        type(self).playlist_reads.append(ids)
        errors = {i: m for i, m in type(self).playlist_errors.items() if i in ids}
        results = {i: type(self).playlists[i] for i in ids if i not in errors}
        return results, errors


def _ytm_track(video_id: str) -> dict[str, Any]:
    return {
        "videoId": video_id,
        "title": f"Track {video_id}",
        "artists": [{"name": "Invented Artist"}],
        "videoType": "MUSIC_VIDEO_TYPE_ATV",
    }


def _yt_item(video_id: str) -> dict[str, Any]:
    return {
        "snippet": {
            "title": f"Track {video_id}",
            "channelTitle": "Invented Channel",
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }


@pytest.fixture
def patch_playlist_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]]:
    """Default data: a library with the generated/system ids `LM`, `SE`, an
    `LRSR…` and an `LRYR…` (all skipped) plus two ordinary playlists. `PLroad`
    reads on both sides (its YouTube copy repeats `v1`); YouTube can't see
    `PLnight`."""
    import likesurgeon.cli as _cli_mod

    ytm, yt = _FakeYTMusicPlaylists, _FakeYouTubeClient
    ytm.library = [
        {"playlistId": "LM", "title": "Liked Music"},
        {"playlistId": "SE", "title": "Episodes for Later"},
        {"playlistId": "LRSRmix", "title": "Supermix"},
        {"playlistId": "LRYRmix", "title": "Replay Mix"},
        {"playlistId": "PLroad", "title": "Road Trip"},
        {"playlistId": "PLnight", "title": "Late Night"},
    ]
    ytm.playlists = {
        "PLroad": {
            "id": "PLroad",
            "title": "Road Trip",
            "owned": True,
            "tracks": [
                _ytm_track("v1"),
                _ytm_track("v2"),
            ],
        },
        "PLnight": {
            "id": "PLnight",
            "title": "Late Night",
            "owned": True,
            "tracks": [
                _ytm_track("v3"),
            ],
        },
    }
    yt.playlists = {"PLroad": [_yt_item("v1"), _yt_item("v2"), _yt_item("v1")]}
    yt.playlist_errors = {"PLnight": "HTTP 404: Not Found"}
    monkeypatch.setattr(_cli_mod, "YTMusicClient", ytm)
    monkeypatch.setattr(_yt_mod, "YouTubeClient", yt)
    return ytm, yt


def _playlist_state(home: Path) -> tuple[int, list[tuple[str, str, str]]]:
    """(PlaylistScan count, every playlist snapshot as (source, id, title))."""
    from sqlalchemy import func, select

    from likesurgeon.models import PlaylistScan, Snapshot

    s = _open_db(home)
    try:
        scans = s.scalar(select(func.count()).select_from(PlaylistScan))
        snaps = [
            (snap.source, snap.playlist_id, snap.playlist_title)
            for snap in s.scalars(
                select(Snapshot).where(Snapshot.playlist_scan_id.is_not(None)).order_by(Snapshot.id)
            )
        ]
        return scans, snaps
    finally:
        s.close()


def test_scan_playlists_backs_up_ordinary_playlists(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    import json

    from likesurgeon.cli import app

    (fake_home / "config.json").write_text(json.dumps({"region": "KR"}))
    ytm, yt = patch_playlist_clients

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 0, result.output
    assert ytm.playlist_reads == [["PLroad", "PLnight"]]
    assert yt.playlist_reads == [["PLroad", "PLnight"]]
    assert yt.status_reads == [["v1", "v2"]]
    assert yt.last_user_region == "KR"
    assert _playlist_state(fake_home) == (
        1,
        [
            ("ytmusic_playlist", "PLroad", "Road Trip"),
            ("youtube_playlist", "PLroad", "Road Trip"),
            ("ytmusic_playlist", "PLnight", "Late Night"),
        ],
    )
    out = _out(result)
    assert "Playlist scan #1 stored" in out
    assert "2 playlists" in out
    assert "3 tracks" in out
    assert "4 generated or system playlists skipped" in out
    assert "likesurgeon playlists" in out


@pytest.mark.parametrize("library_ids", [[], ["LM", "SE", "LRSRmix"]])
def test_scan_playlists_with_nothing_to_back_up_stores_an_empty_scan(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
    library_ids: list[str],
) -> None:
    from likesurgeon.cli import app

    ytm, yt = patch_playlist_clients
    ytm.library = [{"playlistId": i, "title": i} for i in library_ids]

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 0, result.output
    assert _playlist_state(fake_home) == (1, [])
    assert "No playlists to back up" in _out(result)
    assert yt.constructed is False


def test_scan_playlists_fails_when_the_library_read_fails(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.ytmusic_client import UnexpectedResponseError

    ytm, yt = patch_playlist_clients
    ytm.library_error = UnexpectedResponseError("library page [changed]")

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 2, result.output
    assert "library page [changed]" in _out(result)
    assert _playlist_state(fake_home) == (0, [])
    assert yt.constructed is False


def test_scan_playlists_fails_when_every_playlist_is_unreadable(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.cli import app

    ytm, yt = patch_playlist_clients
    ytm.playlist_errors = {"PLroad": "KeyError: 'contents'", "PLnight": "KeyError: 'contents'"}

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 2, result.output
    out = _out(result)
    assert "Road Trip" in out and "Late Night" in out
    assert "Could not read any of the 2 playlists" in out
    assert _playlist_state(fake_home) == (0, [])
    assert yt.constructed is False


def test_scan_playlists_warns_about_one_unreadable_playlist_and_stores_the_rest(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.cli import app

    ytm, yt = patch_playlist_clients
    ytm.library[5]["title"] = "Late [bold]Night"
    ytm.playlist_errors = {"PLnight": "KeyError: 'contents'"}

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "Late [bold]Night" in out
    assert "KeyError: 'contents'" in out
    # YouTube is only asked for what YT Music backed up.
    assert yt.playlist_reads == [["PLroad"]]
    assert _playlist_state(fake_home) == (
        1,
        [
            ("ytmusic_playlist", "PLroad", "Road Trip"),
            ("youtube_playlist", "PLroad", "Road Trip"),
        ],
    )


def test_scan_playlists_falls_back_to_the_id_for_an_untitled_playlist(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.cli import app

    ytm, _ = patch_playlist_clients
    del ytm.library[4]["title"]

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 0, result.output
    assert ("ytmusic_playlist", "PLroad", "PLroad") in _playlist_state(fake_home)[1]


@pytest.mark.parametrize("error_name", ["AuthorizationRequiredError", "ClientSecretsMissingError"])
def test_scan_playlists_without_youtube_auth_stores_the_ytmusic_side(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
    error_name: str,
) -> None:
    from likesurgeon.cli import app

    _, yt = patch_playlist_clients
    yt.playlists_error = getattr(_yt_mod, error_name)("no token")

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 0, result.output
    assert "auth youtube" in _out(result)
    assert yt.status_reads == []
    assert _playlist_state(fake_home) == (
        1,
        [
            ("ytmusic_playlist", "PLroad", "Road Trip"),
            ("ytmusic_playlist", "PLnight", "Late Night"),
        ],
    )


def test_scan_playlists_survives_a_youtube_read_failure(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.cli import app

    _, yt = patch_playlist_clients
    yt.playlists_error = RuntimeError("transport [down]")

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 0, result.output
    assert "RuntimeError: transport [down]" in _out(result)
    assert [src for src, _, _ in _playlist_state(fake_home)[1]] == [
        "ytmusic_playlist",
        "ytmusic_playlist",
    ]


def test_scan_playlists_lists_playlists_youtube_cannot_read(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.cli import app

    ytm, yt = patch_playlist_clients
    ytm.library.append({"playlistId": "PLgym", "title": "Gym"})
    ytm.playlists["PLgym"] = {"id": "PLgym", "title": "Gym", "tracks": [_ytm_track("v4")]}
    yt.playlist_errors["PLgym"] = "HTTP 403: Forbidden"

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "2 playlist(s) couldn't be read through the YouTube Data API" in out
    assert "Late Night: HTTP 404: Not Found" in out
    assert "Gym: HTTP 403: Forbidden" in out
    youtube_ids = [
        pid for src, pid, _ in _playlist_state(fake_home)[1] if src == "youtube_playlist"
    ]
    assert youtube_ids == ["PLroad"]


def test_scan_playlists_stores_youtube_items_unchecked_when_the_status_read_fails(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.models import Snapshot, SnapshotItem

    _, yt = patch_playlist_clients
    yt.statuses_error = RuntimeError("quota")

    result = CliRunner().invoke(app, ["scan", "playlists"])

    assert result.exit_code == 0, result.output
    assert "RuntimeError: quota" in _out(result)
    s = _open_db(fake_home)
    try:
        availability = s.scalars(
            select(SnapshotItem.is_available)
            .join(Snapshot, SnapshotItem.snapshot_id == Snapshot.id)
            .where(Snapshot.source == "youtube_playlist")
        ).all()
    finally:
        s.close()
    assert availability == [None, None, None]


def _scan_playlists(
    patch: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["scan", "playlists"])
    assert result.exit_code == 0, result.output


def _playlists_cmd(*args: str) -> Any:
    from likesurgeon.cli import app

    return CliRunner().invoke(app, ["playlists", *args], env={"COLUMNS": "200"})


def _cells(result: Any) -> str:
    """Table output with the box-drawing borders removed, whitespace collapsed."""
    return " ".join(re.sub("[│┃┏┓┗┛┡┩└┘┌┐├┤┬┴┼━─╇╈┳┻╋]", " ", result.output).split())


def test_playlists_without_a_scan_hints_at_scan_playlists(fake_home: Path) -> None:
    result = _playlists_cmd()

    assert result.exit_code == 0, result.output
    assert "likesurgeon scan playlists" in _out(result)


def test_playlists_with_an_empty_scan_says_it_found_none(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    patch_playlist_clients[0].library = []
    _scan_playlists(patch_playlist_clients)

    result = _playlists_cmd()

    out = _out(result)
    assert result.exit_code == 0, result.output
    assert "#1" in out
    assert "found no playlists" in out
    assert "auth youtube" not in out


def test_playlists_lists_one_row_per_playlist(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    ytm, yt = patch_playlist_clients
    ytm.library[4]["title"] = ytm.playlists["PLroad"]["title"] = "[Live] Road Trip"
    ytm.playlists["PLroad"]["tracks"][1]["videoType"] = "MUSIC_VIDEO_TYPE_UGC"
    _scan_playlists(patch_playlist_clients)

    result = _playlists_cmd()

    out = _cells(result)
    assert result.exit_code == 0, result.output
    assert "Playlist scan #1" in out
    assert "[Live] Road Trip" in out
    assert "Late Night" in out
    # id, title, own, YT Music count, music %, music / UGC / other, YouTube count, dead
    assert "1 [Live] Road Trip own 2 50% 1 1 0 3 0" in out
    # PLnight has no YouTube side: placeholders, not zeros.
    assert "3 Late Night own 1 100% 1 0 0 - -" in out
    assert "dead items" not in out
    assert "auth youtube" not in out  # one playlist has a YouTube side


def test_playlists_says_youtube_coverage_is_absent_and_suggests_auth(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    patch_playlist_clients[1].playlists_error = RuntimeError("boom")
    _scan_playlists(patch_playlist_clients)

    out = _out(_playlists_cmd())

    assert "no YouTube coverage" in out
    assert "likesurgeon auth youtube" in out


def test_playlists_does_not_suggest_auth_when_youtube_is_authorized(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.config import Config

    patch_playlist_clients[1].playlists_error = RuntimeError("boom")
    _scan_playlists(patch_playlist_clients)
    Config.load().youtube_token_path.write_text("{}")

    out = _out(_playlists_cmd())

    assert "no YouTube coverage" in out
    assert "auth youtube" not in out


def _mark_dead(
    monkeypatch: pytest.MonkeyPatch, yt: type[_FakeYouTubeClient], dead: set[str]
) -> None:
    def statuses(self: Any, video_ids: list[str], *, user_region: str | None = None) -> dict:
        # v2 has no verdict: it is stored unchecked.
        return {
            v: VideoStatus(
                is_available=None if v == "v2" else v not in dead,
                reason="deleted" if v in dead else None,
            )
            for v in video_ids
        }

    monkeypatch.setattr(yt, "fetch_video_statuses", statuses)


def test_playlists_dead_lists_dead_items_and_flag_off_hides_them(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    ytm, yt = patch_playlist_clients
    _mark_dead(monkeypatch, yt, {"v1"})
    _scan_playlists(patch_playlist_clients)

    plain = _cells(_playlists_cmd())
    dead = _cells(_playlists_cmd("--dead"))

    # v1 sits at positions 1 and 3 of the YouTube copy; v2 is unchecked.
    assert "2 (1 unchecked)" in plain
    assert "deleted" not in plain
    assert "Road Trip 1 v1 deleted Track v1" in dead
    assert "Road Trip 3 v1 deleted Track v1" in dead


def test_playlists_dead_escapes_markup_in_titles(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    ytm, yt = patch_playlist_clients
    yt.playlists["PLroad"][0]["snippet"]["title"] = "Gone [x] song"
    _mark_dead(monkeypatch, yt, {"v1"})
    _scan_playlists(patch_playlist_clients)

    assert "Gone [x] song" in _cells(_playlists_cmd("--dead"))


def test_playlists_dead_without_youtube_coverage_adds_no_none_found_line(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    patch_playlist_clients[1].playlists_error = RuntimeError("boom")
    _scan_playlists(patch_playlist_clients)

    out = _out(_playlists_cmd("--dead"))

    assert "no YouTube coverage" in out
    assert "No dead items" not in out


def test_snapshots_with_only_playlist_snapshots_mentions_them(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    from likesurgeon.cli import app

    patch_playlist_clients[0].library = patch_playlist_clients[0].library[-1:]
    _scan_playlists(patch_playlist_clients)

    out = _out(CliRunner().invoke(app, ["snapshots"], env={"COLUMNS": "200"}))

    assert "No likes snapshots yet" in out
    assert "1 playlist snapshot is not listed" in out
    assert "likesurgeon playlists" in out


def test_playlists_dead_with_none_says_so(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    _scan_playlists(patch_playlist_clients)

    assert "No dead items" in _out(_playlists_cmd("--dead"))


def test_export_returns_the_tracks_of_a_reported_snapshot_id(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
) -> None:
    import json

    from likesurgeon.cli import app

    _scan_playlists(patch_playlist_clients)
    assert "1 Road Trip" in _cells(_playlists_cmd())

    result = CliRunner().invoke(app, ["export", "1"])

    assert result.exit_code == 0, result.output
    assert "Track v1" in result.output
    assert "Track v2" in result.output
    json.loads(result.output)


def test_snapshots_lists_likes_only_and_counts_playlist_snapshots(
    fake_home: Path,
    patch_playlist_clients: tuple[type[_FakeYTMusicPlaylists], type[_FakeYouTubeClient]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from likesurgeon.cli import app

    _scan_playlists(patch_playlist_clients)
    monkeypatch.setattr(
        _FakeYTMusicPlaylists,
        "fetch_liked_songs",
        lambda self, limit=None: [{"videoId": "v1", "title": "Song", "artists": [{"name": "X"}]}],
        raising=False,
    )
    assert CliRunner().invoke(app, ["scan", "ytmusic"]).exit_code == 0

    result = CliRunner().invoke(app, ["snapshots"], env={"COLUMNS": "200"})

    out = _out(result)
    assert result.exit_code == 0, result.output
    assert "ytmusic_playlist" not in out
    assert "youtube_playlist" not in out
    assert "ytmusic_liked_songs" in out
    assert "3 playlist snapshots are not listed" in out
    assert "likesurgeon playlists" in out


def test_config_load_propagates_invalid_region_in_json(
    fake_home: Path,
) -> None:
    """Unit-level guard: Config.load() raises InvalidRegionError when
    config.json holds a non-alpha-2 region. Pinned independently of the
    CLI catch so a future config refactor can't silently swallow it."""
    import json

    from likesurgeon.config import Config, InvalidRegionError

    (fake_home / "config.json").write_text(json.dumps({"region": "KOREA"}))

    with pytest.raises(InvalidRegionError):
        Config.load()


def test_bootstrap_converts_invalid_region_to_fail(
    fake_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_bootstrap catches InvalidRegionError from Config.load and
    converts it to typer.Exit(code=2) with a friendly message — the
    user must never see a traceback for a config typo. The error
    message must surface the offending value so the user can fix it."""
    import json

    import typer

    from likesurgeon.cli import _bootstrap

    (fake_home / "config.json").write_text(json.dumps({"region": "KOREA"}))

    with pytest.raises(typer.Exit) as exc_info:
        _bootstrap()
    assert exc_info.value.exit_code == 2
    # `_fail` prints to err_console (stderr by default in this codebase).
    # Capture both streams so we don't depend on Rich's stream choice.
    out, err = capsys.readouterr()
    combined = out + err
    assert "KOREA" in combined
    assert "ISO 3166-1 alpha-2" in combined


def test_auth_ytmusic_converts_invalid_region_to_fail(
    fake_home: Path,
) -> None:
    """`auth ytmusic` must use the same fail-fast catch as `_bootstrap`.
    A typo in config.json's region must NOT print a traceback when the
    user runs auth setup."""
    import json

    from likesurgeon.cli import app

    (fake_home / "config.json").write_text(json.dumps({"region": "KOREA"}))

    runner = CliRunner()
    result = runner.invoke(app, ["auth", "ytmusic"])

    assert result.exit_code == 2
    assert "KOREA" in result.output
    assert "ISO 3166-1 alpha-2" in result.output
    assert "Traceback" not in result.output


@pytest.fixture
def fake_extraction(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """`auth ytmusic --from-browser` without a browser: extraction is recorded,
    and the account read off the new cookies is ``@me``."""
    import likesurgeon.cli as _cli_mod

    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        _cli_mod, "write_browser_json_from_browser", lambda b, t: calls.append((b, t))
    )
    monkeypatch.setattr(_cli_mod.YTMusicClient, "account_identity", lambda self: "@me")
    return calls


def test_auth_ytmusic_from_browser_records_browser_and_account(
    fake_home: Path, fake_extraction: list[tuple[str, Path]]
) -> None:
    """Kept in config.json (beside the user's region) so a stale Liked songs
    read can re-extract cookies, as long as the browser is still that account."""
    import json

    from likesurgeon.cli import app

    (fake_home / "config.json").write_text(json.dumps({"region": "KR"}))

    result = CliRunner().invoke(app, ["auth", "ytmusic", "--from-browser", " Chrome "])

    assert result.exit_code == 0, result.output
    assert fake_extraction == [("chrome", fake_home / "browser.json")]
    saved = json.loads((fake_home / "config.json").read_text())
    assert saved == {"region": "KR", "ytmusic_browser": "chrome", "ytmusic_account": "@me"}


def test_auth_ytmusic_from_browser_unreadable_account_records_no_browser(
    fake_home: Path, fake_extraction: list[tuple[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the account a refresh couldn't tell a switched browser profile,
    so refresh is off — including a pair recorded by an earlier auth."""
    import json

    import likesurgeon.cli as _cli_mod
    from likesurgeon.ytmusic_client import UnexpectedResponseError

    def fail(self: Any) -> str:
        raise UnexpectedResponseError("couldn't confirm the signed-in YT Music account")

    monkeypatch.setattr(_cli_mod.YTMusicClient, "account_identity", fail)
    (fake_home / "config.json").write_text(
        json.dumps({"region": "KR", "ytmusic_browser": "firefox", "ytmusic_account": "@old"})
    )

    result = CliRunner().invoke(_cli_mod.app, ["auth", "ytmusic", "--from-browser", "chrome"])

    assert result.exit_code == 0, result.output
    assert "won't be refreshed automatically" in _out(result)
    assert json.loads((fake_home / "config.json").read_text()) == {"region": "KR"}


def test_auth_ytmusic_from_browser_failure_records_nothing(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import likesurgeon.cli as _cli_mod
    from likesurgeon.ytmusic_client import CookieExtractionError

    def fail(browser: str, target: Path) -> None:
        raise CookieExtractionError("not signed in")

    monkeypatch.setattr(_cli_mod, "write_browser_json_from_browser", fail)

    result = CliRunner().invoke(_cli_mod.app, ["auth", "ytmusic", "--from-browser", "chrome"])

    assert result.exit_code == 2
    assert not (fake_home / "config.json").exists()


def test_auth_ytmusic_from_browser_unreadable_config_warns(
    fake_home: Path, fake_extraction: list[tuple[str, Path]]
) -> None:
    """browser.json is written either way; a config.json that isn't a JSON
    object is left alone and the user is told refresh is off."""
    from likesurgeon.cli import app

    (fake_home / "config.json").write_text("{bad")

    result = CliRunner().invoke(app, ["auth", "ytmusic", "--from-browser", "chrome"])

    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "Couldn't update" in out and "won't be refreshed automatically" in out
    assert (fake_home / "config.json").read_text() == "{bad"


def test_auth_ytmusic_from_browser_save_failure_names_still_recorded_browser(
    fake_home: Path, fake_extraction: list[tuple[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import likesurgeon.cli as _cli_mod

    def fail(app_dir: Path, changes: dict[str, Any]) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(_cli_mod, "update_config", fail)
    (fake_home / "config.json").write_text(
        json.dumps({"ytmusic_browser": "chrome", "ytmusic_account": "@me"})
    )

    result = CliRunner().invoke(_cli_mod.app, ["auth", "ytmusic", "--from-browser", "chrome"])

    assert result.exit_code == 0, result.output
    assert "cookies are still refreshed from chrome for @me" in _out(result)


def test_auth_ytmusic_from_browser_save_failure_with_a_different_recorded_pair_fails(
    fake_home: Path, fake_extraction: list[tuple[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale firefox/@old pair would later refresh chrome's @me cookies back
    to @old, so the command must not end successfully with it still armed."""
    import json

    import likesurgeon.cli as _cli_mod

    def fail(app_dir: Path, changes: dict[str, Any]) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(_cli_mod, "update_config", fail)
    (fake_home / "config.json").write_text(
        json.dumps({"ytmusic_browser": "firefox", "ytmusic_account": "@old"})
    )

    result = CliRunner().invoke(_cli_mod.app, ["auth", "ytmusic", "--from-browser", "chrome"])

    assert result.exit_code == 1, result.output
    assert "still names firefox for @old" in _out(result)


def test_auth_ytmusic_manual_flow_forgets_recorded_browser(fake_home: Path) -> None:
    """A pasted browser.json doesn't come from the recorded browser, so a
    refresh must not overwrite it."""
    import json

    from likesurgeon.cli import app

    (fake_home / "config.json").write_text(
        json.dumps({"region": "KR", "ytmusic_browser": "chrome", "ytmusic_account": "@me"})
    )

    result = CliRunner().invoke(app, ["auth", "ytmusic"])

    assert result.exit_code == 0, result.output
    assert json.loads((fake_home / "config.json").read_text()) == {"region": "KR"}


def test_auth_ytmusic_manual_flow_fails_when_browser_cannot_be_forgotten(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import likesurgeon.cli as _cli_mod

    def fail(app_dir: Path, changes: dict[str, Any]) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(_cli_mod, "update_config", fail)
    (fake_home / "config.json").write_text(
        json.dumps({"ytmusic_browser": "chrome", "ytmusic_account": "@me"})
    )

    result = CliRunner().invoke(_cli_mod.app, ["auth", "ytmusic"])

    assert result.exit_code == 1
    assert "could overwrite a pasted browser.json" in _out(result)


def test_video_ids_for_tracks_chunks_lookup_above_batch_size(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Track lookup used by `issues` must chunk its IN(...) clause
    so it doesn't exceed SQLite's `SQLITE_MAX_VARIABLE_NUMBER` for large
    diagnoses. Use a small batch size to exercise the chunking branch
    deterministically with a manageable number of fixture rows.

    The helper lives in ``sync.py`` (cli.py re-exports it for back-compat),
    so the batch-size constant must be patched on the sync module — that's
    where the function reads it.
    """
    from likesurgeon import sync as _sync_mod
    from likesurgeon.cli import _video_ids_for_tracks
    from likesurgeon.models import Track

    monkeypatch.setattr(_sync_mod, "_TRACK_LOOKUP_BATCH_SIZE", 100)

    n = 250  # > 2× batch size — forces at least 3 chunks
    tracks = [
        Track(
            source="youtube_liked_videos",
            video_id=f"vid_{i:04d}",
            title=f"t{i}",
            artists="[]",
            canonical_key=f"k{i}",
            dedupe_key=f"d{i}",
        )
        for i in range(n)
    ]
    session.add_all(tracks)
    session.commit()

    track_ids = {t.id for t in tracks}
    out = _video_ids_for_tracks(session, track_ids)

    assert len(out) == n
    assert all(out[t.id] == f"vid_{i:04d}" for i, t in enumerate(tracks))


def test_video_ids_for_tracks_handles_empty_input(session: Session) -> None:
    """Empty input must short-circuit without issuing any queries."""
    from likesurgeon.cli import _video_ids_for_tracks

    assert _video_ids_for_tracks(session, set()) == {}


def test_auth_youtube_converts_invalid_region_to_fail(
    fake_home: Path,
) -> None:
    """`auth youtube` must use the same fail-fast catch as `_bootstrap`."""
    import json

    from likesurgeon.cli import app

    (fake_home / "config.json").write_text(json.dumps({"region": "KOREA"}))

    runner = CliRunner()
    result = runner.invoke(app, ["auth", "youtube"])

    assert result.exit_code == 2
    assert "KOREA" in result.output
    assert "ISO 3166-1 alpha-2" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# `sync` command — integration tests via CliRunner.
#
# The CLI uses LIKE_SURGEON_HOME to resolve cfg.db_path, so we seed that
# concrete on-disk DB before invoking; the CLI's own session_scope reads
# what we wrote. Clients are stubbed module-side so the function-local
# `from .youtube_client import YouTubeClient` inside `sync` resolves to
# the fake (matches the existing `scan_youtube_likes` pattern). YTMusicClient
# is module-level on cli.py, so it's monkeypatched directly there. Both fakes
# act on one ``FakeAccount`` (tests/fake_account.py).
# ---------------------------------------------------------------------------


class _FakeYouTubeWrite:
    """``YouTubeClient`` for `sync`: likes live on the shared ``account``;
    ``has_write_scope_return`` fakes a token without the write scope."""

    instances: list[_FakeYouTubeWrite] = []
    has_write_scope_return: bool = True
    account: FakeAccount

    def __init__(self, **kwargs: Any) -> None:
        self.scope_calls = 0
        type(self).instances.append(self)

    def has_write_scope(self) -> bool:
        self.scope_calls += 1
        return type(self).has_write_scope_return

    def rate_video(self, video_id: str, rating: str) -> None:
        type(self).account.rate_video(video_id, rating)

    def is_in_liked_videos(self, video_id: str) -> bool:
        return type(self).account.is_in_liked_videos(video_id)


class _FakeYTMusicWrite:
    """``YTMusicClient`` for `sync`: liked songs are read off the shared ``account``."""

    instances: list[_FakeYTMusicWrite] = []
    account: FakeAccount

    def __init__(self, **kwargs: Any) -> None:
        type(self).instances.append(self)

    def fetch_liked_songs(self, limit: int | None = None) -> list[dict[str, Any]]:
        return type(self).account.fetch_liked_songs(limit)


@pytest.fixture(autouse=True)
def _reset_sync_fakes(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    """Reset class-level state on the sync fakes; no real sleeps in sync."""
    monkeypatch.setattr("likesurgeon.sync_dispatch.time.sleep", lambda _s: None)
    _FakeYouTubeWrite.instances = []
    _FakeYouTubeWrite.has_write_scope_return = True
    _FakeYTMusicWrite.instances = []
    yield


@pytest.fixture
def patch_sync_clients(monkeypatch: pytest.MonkeyPatch) -> FakeAccount:
    """Swap YouTubeClient / YTMusicClient for the sync fakes, both backed by
    the returned (empty) account.

    YouTubeClient is patched on the `youtube_client` module (function-local
    import). YTMusicClient is patched on `cli` (module-level import there).
    """
    import likesurgeon.cli as _cli_mod

    account = FakeAccount([])
    _FakeYouTubeWrite.account = account
    _FakeYTMusicWrite.account = account
    monkeypatch.setattr(_yt_mod, "YouTubeClient", _FakeYouTubeWrite)
    monkeypatch.setattr(_cli_mod, "YTMusicClient", _FakeYTMusicWrite)
    return account


def _seed_diagnosis(home: Path, *, issue_types: list[str]) -> dict[str, int]:
    """A diagnosis without scans on the CLI's on-disk DB, one item per issue
    type, each with a fresh source Track. Returns ``{issue_type: item_id}``."""
    from likesurgeon.models import Diagnosis, DiagnosisItem, Track

    out: dict[str, int] = {}
    s = _open_db(home)
    try:
        diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
        s.add(diag)
        s.flush()
        for i, issue_type in enumerate(issue_types):
            src = Track(
                source="youtube_liked_videos",
                video_id=f"src_{i}",
                title=f"src-{i}",
                artists="[]",
                canonical_key=f"sk-{i}",
                dedupe_key=f"sd-{i}",
            )
            s.add(src)
            s.flush()
            item = DiagnosisItem(
                diagnosis_id=diag.id,
                issue_type=issue_type,
                confidence=1.0,
                reason="diagnosis-time evidence",
                source_track_id=src.id,
                related_track_id=None,
                status="open",
            )
            s.add(item)
            s.flush()
            out[issue_type] = item.id
        s.commit()
    finally:
        s.close()
    return out


def _open_db(home: Path) -> Session:
    from likesurgeon.config import DEFAULT_DB_FILENAME
    from likesurgeon.db import init_db, make_engine, make_session_factory

    engine = make_engine(home / DEFAULT_DB_FILENAME)
    init_db(engine)
    return make_session_factory(engine)()


def _seed_pairs(home: Path, pairs: list[tuple[str, str, str | None, bool | None]]) -> list[int]:
    """Real LL / LM scans and a diagnosis built from them, with one write-eligible
    pair finding per ``(issue_type, A, B, A's availability)``. LL holds the A's
    (availability as scanned; None = unknown), LM the B's. A ``B`` of None means
    no LM entry and no related track (an ``unrendered_music`` finding). Returns
    item ids."""
    from sqlalchemy import select

    from likesurgeon.models import Diagnosis, DiagnosisItem, Track
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(home)
    try:
        ll_raw = []
        for _, a, _, available in pairs:
            raw = _yt_raw(a, f"Song {a}")
            if available is not None:
                raw["_likesurgeon_video_status"] = {
                    "is_available": available,
                    "reason": None if available else "deleted",
                }
            ll_raw.append(raw)
        ll = create_snapshot(s, "youtube_liked_videos", ll_raw)
        lm = create_snapshot(
            s,
            "ytmusic_liked_songs",
            [_ytm_raw(b, f"Song {b}", ["X"]) for _, _, b, _ in pairs if b is not None],
        )
        diag = Diagnosis(youtube_snapshot_id=ll.id, ytmusic_snapshot_id=lm.id)
        s.add(diag)
        s.flush()
        track = {(t.source, t.video_id): t.id for t in s.scalars(select(Track))}
        items = [
            DiagnosisItem(
                diagnosis_id=diag.id,
                issue_type=issue_type,
                confidence=1.0,
                reason="diagnosis-time evidence",
                source_track_id=track["youtube_liked_videos", a],
                related_track_id=track["ytmusic_liked_songs", b] if b is not None else None,
                status="open",
            )
            for issue_type, a, b, _ in pairs
        ]
        s.add_all(items)
        s.commit()
        return [it.id for it in items]
    finally:
        s.close()


def _sync_rows(home: Path) -> tuple[dict[int, str], list[tuple[int, str, str]]]:
    """Every finding's status, and every sync attempt as (item id, kind, status)."""
    from sqlalchemy import select

    from likesurgeon.models import DiagnosisItem, SyncAttempt

    s = _open_db(home)
    try:
        statuses = {it.id: it.status for it in s.scalars(select(DiagnosisItem))}
        attempts = [
            (a.diagnosis_item_id, a.kind, a.status)
            for a in s.scalars(select(SyncAttempt).order_by(SyncAttempt.id))
        ]
        return statuses, attempts
    finally:
        s.close()


def _out(result: Any) -> str:
    return " ".join(result.output.split())  # Rich wraps at the runner's 80 columns


_REPOINT_ROWS = [
    "repoint_precheck_a",
    "repoint_precheck_b",
    "repoint_like",
    "repoint_verify",
    "repoint_unlike",
    "repoint_unlike_verify",
    "lm_check",
]


def test_sync_no_diagnosis_exits_one(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    """Empty DB → friendly message + exit 1, no clients ever instantiated."""
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["sync"])

    assert result.exit_code == 1, result.output
    assert "No diagnosis yet" in result.output
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


@pytest.mark.parametrize("args", [["--dry-run"], ["--dry-run", "--yes"]])
def test_sync_dry_run_prints_plan_and_writes_nothing(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
    args: list[str],
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import (
        ISSUE_RELINKED,
        ISSUE_RENDERED_AS_OTHER,
        ISSUE_SHADOW_DUPLICATE,
    )

    _seed_pairs(
        fake_home,
        [
            (ISSUE_RELINKED, "A1", "B1", False),
            (ISSUE_SHADOW_DUPLICATE, "A2", "B2", False),
            (ISSUE_RENDERED_AS_OTHER, "A3", "B3", True),  # playable: needs the opt-in
        ],
    )

    result = CliRunner().invoke(app, ["sync", *args])

    assert result.exit_code == 0, result.output
    assert "Sync plan" in result.output
    # Action lines (2-space indent), not the 4-space skip-breakdown lines.
    assert "\n  repoint: 1" in result.output
    assert "\n  unlike_shadow: 1" in result.output
    assert "skipped: 1" in result.output
    assert "157 units" in result.output
    assert _sync_rows(fake_home)[1] == []
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_sync_confirmation_no_aborts(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])

    result = CliRunner().invoke(app, ["sync"], input="n\n")

    assert result.exit_code != 0
    assert _sync_rows(fake_home)[1] == []
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_sync_rejects_removed_flags(fake_home: Path) -> None:
    from likesurgeon.cli import app

    for args in (["--include-fuzzy-drift"], ["--drift-min-confidence", "0.9"]):
        result = CliRunner().invoke(app, ["sync", *args])
        assert result.exit_code == 2, result.output
        assert "No such option" in result.output


def test_sync_missing_write_scope_blocks_run(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    _FakeYouTubeWrite.has_write_scope_return = False

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    assert "write scope" in result.output
    assert patch_sync_clients.rate_calls == []
    # YTMusicClient was never built — the scope failure short-circuits first.
    assert _FakeYTMusicWrite.instances == []
    assert _sync_rows(fake_home)[1] == []


@pytest.mark.parametrize("missing_file", [True, False])
def test_sync_failing_ytmusic_auth_blocks_run(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
    missing_file: bool,
) -> None:
    """Every action reads YT Music liked songs before and after it: missing
    or expired cookies are refused up front."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED
    from likesurgeon.ytmusic_client import AuthFileMissingError, UnexpectedResponseError

    [item_id] = _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    patch_sync_clients.lm_read_errors = {
        1: AuthFileMissingError("No YouTube Music auth file found. Run `likesurgeon auth ytmusic`")
        if missing_file
        else UnexpectedResponseError("ytmusicapi request failed (HTTP 401)")
    }

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    assert "auth check failed" in result.output
    assert patch_sync_clients.rate_calls == []
    assert _sync_rows(fake_home) == ({item_id: "open"}, [])


def test_sync_repoints_a_relinked_pair(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    [item_id] = _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["x", "A"], {"A": "B"}

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 0, result.output
    assert "applied=1 failed=0 skipped=0 restored=0" in _out(result)
    assert _FakeYouTubeWrite.instances[0].scope_calls == 1
    assert acct.rate_calls == [("B", "like"), ("A", "none")]
    statuses, attempts = _sync_rows(fake_home)
    assert statuses == {item_id: "applied"}
    assert [kind for _, kind, status in attempts if status == "applied"] == _REPOINT_ROWS


def test_sync_partial_failure_exits_one(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    """One action's write errors → that finding stays 'open', the next still runs."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED
    from likesurgeon.models import DiagnosisItem

    ids = _seed_pairs(
        fake_home, [(ISSUE_RELINKED, "A1", "B1", False), (ISSUE_RELINKED, "A2", "B2", False)]
    )
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A1", "A2"], {"A1": "B1", "A2": "B2"}
    acct.rate_errors = {("B1", "like")}

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    assert "applied=1 failed=1" in _out(result)
    statuses, _ = _sync_rows(fake_home)
    assert statuses == {ids[0]: "open", ids[1]: "applied"}
    s = _open_db(fake_home)
    try:
        # Reason is the diagnosis-time evidence, not the failure detail.
        assert s.get(DiagnosisItem, ids[0]).reason == "diagnosis-time evidence"
    finally:
        s.close()


def test_sync_include_playable_repoints_rendered_as_other(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RENDERED_AS_OTHER

    [item_id] = _seed_pairs(fake_home, [(ISSUE_RENDERED_AS_OTHER, "MV", "T", True)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["MV"], {"MV": "T"}
    runner = CliRunner()

    result = runner.invoke(app, ["sync", "--yes"])
    assert result.exit_code == 0, result.output
    assert acct.rate_calls == []
    # A plan-time skip is no write, so it doesn't block the next run.
    assert _sync_rows(fake_home) == ({item_id: "open"}, [(item_id, "repoint", "skipped")])

    result = runner.invoke(app, ["sync", "--yes", "--include-playable"])
    assert result.exit_code == 0, result.output
    assert acct.rate_calls == [("T", "like"), ("MV", "none")]
    assert _sync_rows(fake_home)[0] == {item_id: "applied"}


def _seed_unrendered(home: Path, n: int = 2) -> list[int]:
    from likesurgeon.diagnosis import ISSUE_UNRENDERED_MUSIC

    return _seed_pairs(home, [(ISSUE_UNRENDERED_MUSIC, f"A{i}", None, None) for i in range(n)])


def test_sync_dry_run_counts_relikes_only_with_the_flag(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    _seed_unrendered(fake_home)
    runner = CliRunner()

    result = runner.invoke(app, ["sync", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "\n  relike: 0" in result.output

    result = runner.invoke(app, ["sync", "--dry-run", "--relike-unrendered"])
    assert result.exit_code == 0, result.output
    assert "\n  relike: 2" in result.output
    assert "206 units" in result.output
    assert "tothetopofLikedvideos" in "".join(result.output.split())
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_sync_relikes_unrendered_oldest_first(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    ids = _seed_unrendered(fake_home)
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A0", "A1"], {"A0": None, "A1": None}
    acct.relike_renders = {"A0": "A0", "A1": "T"}

    result = CliRunner().invoke(app, ["sync", "--yes", "--relike-unrendered"])

    assert result.exit_code == 0, result.output
    assert "applied=2" in _out(result)
    assert acct.rate_calls == [("A1", "none"), ("A1", "like"), ("A0", "none"), ("A0", "like")]
    assert _sync_rows(fake_home)[0] == {ids[0]: "applied", ids[1]: "applied"}


def test_sync_relike_that_stays_unrendered_is_skipped(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    [item_id] = _seed_unrendered(fake_home, 1)
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A0"], {"A0": None}
    acct.relike_renders = {"A0": None}

    result = CliRunner().invoke(app, ["sync", "--yes", "--relike-unrendered"])

    assert result.exit_code == 0, result.output
    assert "skipped=1" in _out(result)
    assert _sync_rows(fake_home)[0] == {item_id: "skipped"}


def test_sync_relike_limit_takes_the_oldest(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    _seed_unrendered(fake_home)
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A0", "A1"], {"A0": None, "A1": None}
    acct.relike_renders = {"A0": "A0", "A1": "A1"}

    result = CliRunner().invoke(app, ["sync", "--yes", "--relike-unrendered", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert acct.rate_calls == [("A1", "none"), ("A1", "like")]


def test_sync_without_the_flag_leaves_unrendered_untouched(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    ids = _seed_unrendered(fake_home)

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 0, result.output
    assert patch_sync_clients.rate_calls == []
    assert _sync_rows(fake_home) == ({ids[0]: "open", ids[1]: "open"}, [])


def test_sync_unlikes_shadows_by_the_scanned_availability_of_a(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    """The availability map comes from the diagnosis's YouTube scan: only the
    unavailable A is unliked by default, a playable one needs the opt-in, and
    an unknown one never."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_SHADOW_DUPLICATE

    ids = _seed_pairs(
        fake_home,
        [
            (ISSUE_SHADOW_DUPLICATE, "A0", "B0", False),
            (ISSUE_SHADOW_DUPLICATE, "A1", "B1", True),
            (ISSUE_SHADOW_DUPLICATE, "A2", "B2", None),
        ],
    )
    acct = patch_sync_clients
    acct.liked = ["A0", "B0", "A1", "B1", "A2", "B2"]
    acct.renders = {"A0": "B0", "A1": "B1", "A2": "B2"}
    runner = CliRunner()

    result = runner.invoke(app, ["sync", "--dry-run", "--include-playable"])
    assert result.exit_code == 0, result.output
    assert "\n  unlike_shadow: 2" in result.output

    result = runner.invoke(app, ["sync", "--yes"])
    assert result.exit_code == 0, result.output
    assert acct.rate_calls == [("A0", "none")]
    statuses, attempts = _sync_rows(fake_home)
    assert statuses == {ids[0]: "applied", ids[1]: "open", ids[2]: "open"}
    assert (ids[1], "unlike_shadow", "skipped") in attempts
    assert (ids[2], "unlike_shadow", "skipped") in attempts


def test_sync_refuses_when_a_sync_ran_after_the_older_scan(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    """Spec §5.1: one of our writes after the older scan means the diagnosis
    may misalign or be outdated — refuse before any client is built."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED
    from likesurgeon.models import SyncAttempt

    [item_id] = _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    s = _open_db(fake_home)
    try:
        s.add(
            SyncAttempt(diagnosis_item_id=item_id, kind="repoint_like", status="applied", reason="")
        )
        s.commit()
    finally:
        s.close()

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    out = _out(result)
    assert "Refusing to sync: 1 sync attempt(s) ran after the older of the two scans" in out
    assert "Re-scan both sources" in out
    assert _FakeYouTubeWrite.instances == []
    assert len(_sync_rows(fake_home)[1]) == 1


def test_sync_warns_when_newer_scan_exists(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED
    from likesurgeon.models import Diagnosis
    from likesurgeon.snapshot import create_snapshot

    _seed_diagnosis(fake_home, issue_types=[ISSUE_RELINKED])
    s = _open_db(fake_home)
    try:
        used = create_snapshot(s, "ytmusic_liked_songs", [])
        create_snapshot(s, "ytmusic_liked_songs", [])
        s.scalars(select(Diagnosis)).one().ytmusic_snapshot_id = used.id
        s.commit()
    finally:
        s.close()

    result = CliRunner().invoke(app, ["sync", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Stale diagnosis" in result.output
    assert "newer ytmusic_liked_songs scan" in result.output


def test_sync_lists_videos_stranded_by_an_earlier_run(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    """An earlier run's restore failed: say so, by video id, on every run."""
    from datetime import UTC, datetime, timedelta

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED
    from likesurgeon.models import SyncAttempt

    old = _seed_diagnosis(fake_home, issue_types=[ISSUE_RELINKED])[ISSUE_RELINKED]
    s = _open_db(fake_home)
    try:
        s.add(
            SyncAttempt(
                diagnosis_item_id=old,
                kind="restore_like",
                status="failed",
                reason="quotaExceeded",
                # Before the scans below, so this run isn't refused.
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        s.commit()
    finally:
        s.close()
    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])

    result = CliRunner().invoke(app, ["sync", "--dry-run"])

    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "without confirming the result: src_0. For each, check whether the song" in out
    assert "if it isn't, re-like that original YouTube video by hand." in out


def test_sync_quota_exhaustion_stops_and_says_to_rescan(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(
        fake_home, [(ISSUE_RELINKED, "A1", "B1", False), (ISSUE_RELINKED, "A2", "B2", False)]
    )
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A1", "A2"], {"A1": "B1", "A2": "B2"}
    acct.quota_after = 2  # the two prechecks pass, the first write is rejected

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    out = _out(result)
    assert "quota exhausted" in out
    assert "1 action(s) not attempted" in out
    assert "re-scan both sources" in out
    assert acct.rate_calls == [("B1", "like")]


def test_sync_reports_an_action_undone_by_its_lm_check(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    [item_id] = _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A", "D"], {"A": "C", "D": "B"}  # A really shows as C

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output  # a restore stops the run
    out = _out(result)
    assert "applied=0 failed=0 skipped=1 restored=1" in out
    assert "1 action(s) were undone" in out
    assert "re-scan both sources" in out
    assert "A" in acct.liked and "B" not in acct.liked
    assert _sync_rows(fake_home)[0] == {item_id: "skipped"}


def test_sync_restored_message_does_not_claim_b_removed_when_rollback_failed(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A", "D"], {"A": "C", "D": "B"}
    acct.rate_errors = {("B", "none")}  # undoing B's like fails

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    out = _out(result)
    assert "restored=1" in out
    assert "was removed" not in out
    assert "undoing a B like this run added failed" in out
    assert "unlike them on YouTube by hand if you don't want them" in out and "B" in out


def test_sync_names_videos_left_unliked(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A", "D"], {"A": "C", "D": "B"}
    acct.rate_errors = {("A", "like")}  # the restore fails

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    out = _out(result)
    assert "they may be liked nowhere right now: A Re-like them on YouTube by hand." in out


def test_sync_stops_when_liked_songs_cannot_be_read(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED
    from likesurgeon.ytmusic_client import UnexpectedResponseError

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A"], {"A": "B"}
    # Read 1 is the auth probe; read 2 the baseline before the first action.
    acct.lm_read_errors = {2: UnexpectedResponseError("HTTP 500 [server]")}

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    out = _out(result)
    assert "Stopped early: could not read YT Music liked songs" in out
    assert "HTTP 500 [server]" in out
    assert "1 action(s) not attempted" in out
    assert acct.rate_calls == []


@pytest.fixture
def real_ytmusic_client(
    fake_home: Path, patch_sync_clients: FakeAccount, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """The real ``YTMusicClient`` reading the fake account, with chrome / @me
    recorded for refresh; returns the browsers cookies were re-extracted from."""
    import json

    import likesurgeon.cli as _cli_mod
    from likesurgeon.ytmusic_client import YTMusicClient

    acct = patch_sync_clients

    class _AccountYTMusic:
        def get_liked_songs(self, limit: int | None) -> dict[str, Any]:
            return {"tracks": acct.fetch_liked_songs(limit)}

        def _send_request(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
            from .test_ytmusic_client import account_menu

            return account_menu({"accountName": "Me", "channelHandle": "@me"})

    class _AccountClient(YTMusicClient):
        def _build(self, path: Path | None = None) -> Any:
            return _AccountYTMusic()

    extractions: list[str] = []
    monkeypatch.setattr(
        "likesurgeon.ytmusic_client.write_browser_json_from_browser",
        lambda browser, target: extractions.append(browser),
    )
    monkeypatch.setattr(_cli_mod, "YTMusicClient", _AccountClient)
    (fake_home / "browser.json").write_text("{}")
    (fake_home / "config.json").write_text(
        json.dumps({"ytmusic_browser": "chrome", "ytmusic_account": "@me"})
    )
    return extractions


def test_sync_recovers_stale_liked_songs_read_by_refreshing_cookies(
    fake_home: Path, patch_sync_clients: FakeAccount, real_ytmusic_client: list[str]
) -> None:
    """The post-action LM check hits a logged-out response, cookies are
    re-extracted from the recorded browser, and the retried read passes the
    check — no restore."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    ids = _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A"], {"A": "B"}
    # Read 1 is the auth probe, read 2 the baseline, read 3 the check after the
    # action: a logged-out page, which ytmusicapi's parser reports as KeyError.
    acct.lm_read_errors = {3: KeyError("twoColumnBrowseResultsRenderer")}

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 0, result.output
    assert real_ytmusic_client == ["chrome"]
    assert "YT Music cookies refreshed from chrome" in result.stderr
    assert "applied=1" in result.output
    assert acct.rate_calls == [("B", "like"), ("A", "none")]
    assert _sync_rows(fake_home)[0] == {ids[0]: "applied"}


def test_sync_restores_when_refreshed_liked_songs_read_fails_too(
    fake_home: Path, patch_sync_clients: FakeAccount, real_ytmusic_client: list[str]
) -> None:
    """A refresh that doesn't help is a failed LM check, as before: A is
    re-liked and the run stops."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    ids = _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A"], {"A": "B"}
    stale = KeyError("twoColumnBrowseResultsRenderer")
    acct.lm_read_errors = {3: stale, 4: stale}

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    assert real_ytmusic_client == ["chrome"]
    assert "Retried once after refreshing cookies from chrome" in _out(result)
    assert "restored=1" in result.output
    # The restore re-likes A, then the rollback unlikes B.
    assert acct.rate_calls == [("B", "like"), ("A", "none"), ("A", "like"), ("B", "none")]
    assert _sync_rows(fake_home)[0] == {ids[0]: "skipped"}


def test_sync_limit_truncates_actions_and_leaves_rest_open(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    ids = _seed_pairs(fake_home, [(ISSUE_RELINKED, f"A{i}", f"B{i}", False) for i in range(3)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A0", "A1", "A2"], {f"A{i}": f"B{i}" for i in range(3)}

    result = CliRunner().invoke(app, ["sync", "--yes", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "applying first 2 of 3" in result.output
    assert "applied=2" in result.output
    assert acct.rate_calls == [("B0", "like"), ("A0", "none"), ("B1", "like"), ("A1", "none")]
    assert _sync_rows(fake_home)[0] == {ids[0]: "applied", ids[1]: "applied", ids[2]: "open"}


def test_sync_limit_above_action_count_is_noop(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    acct = patch_sync_clients
    acct.liked, acct.renders = ["A"], {"A": "B"}

    result = CliRunner().invoke(app, ["sync", "--yes", "--limit", "10"])

    assert result.exit_code == 0, result.output
    assert "applying first" not in result.output
    assert "applied=1" in result.output


# ---------------------------------------------------------------------------
# compare-likes — LL → LM alignment pipeline.
#
# These tests drive the on-disk DB through the CliRunner like the sync tests
# above, then inspect persisted DiagnosisItem rows. The YouTubeClient used for
# the videos.list same-recording check is patched on the youtube_client module
# so the function-local `from .youtube_client import YouTubeClient` resolves to
# the fake.
# ---------------------------------------------------------------------------


def _ytm_raw(video_id: str, title: str, artists: list[str]) -> dict:
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": a} for a in artists],
    }


def _yt_raw(video_id: str, title: str, channel: str = "ArtistVEVO") -> dict:
    return {
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }


class _FakeYouTubeMetadata:
    """Stub YouTubeClient for compare-likes: serves ``fetch_canonical_metadata``.

    Raises AuthorizationRequiredError unless a test sets ``metadata``;
    ``raise_instead`` overrides both. ``requested`` records each call's ids.
    """

    metadata: dict | None = None
    raise_instead: Exception | None = None
    requested: list[list[str]] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    def fetch_canonical_metadata(self, video_ids: list[str]) -> dict:
        from likesurgeon.youtube_client import AuthorizationRequiredError

        type(self).requested.append(list(video_ids))
        if type(self).raise_instead is not None:
            raise type(self).raise_instead
        if type(self).metadata is None:
            raise AuthorizationRequiredError("no auth")
        return type(self).metadata


@pytest.fixture(autouse=True)
def _reset_metadata_fake() -> Iterable[None]:
    _FakeYouTubeMetadata.metadata = None
    _FakeYouTubeMetadata.raise_instead = None
    _FakeYouTubeMetadata.requested = []
    yield


@pytest.fixture
def patch_metadata_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeYouTubeMetadata]:
    monkeypatch.setattr(_yt_mod, "YouTubeClient", _FakeYouTubeMetadata)
    return _FakeYouTubeMetadata


def _seed_relink(home: Path) -> None:
    """LL [a1, A, a3] / LM [a1, B, a3], newest first: a1 and a3 anchor, so the
    LM entry between them renders the unavailable LL video A as B."""
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(home)
    try:
        create_snapshot(
            s, "ytmusic_liked_songs", [_ytm_raw(v, f"Song {v}", ["X"]) for v in ("a1", "B", "a3")]
        )
        ll = [_yt_raw(v, f"Song {v}") for v in ("a1", "A", "a3")]
        ll[1]["_likesurgeon_video_status"] = {"is_available": False, "reason": "deleted"}
        create_snapshot(s, "youtube_liked_videos", ll)
        s.commit()
    finally:
        s.close()


def _relink_metadata() -> dict:
    """videos.list metadata under which A → B passes the same-recording check."""
    from likesurgeon.compare import CanonicalMetadata

    return {
        vid: CanonicalMetadata(video_id=vid, title="Song", channel_id="UC1", duration_seconds=200)
        for vid in ("A", "B")
    }


def _only_finding(home: Path):
    """The single DiagnosisItem in the DB plus the A / B track ids."""
    from sqlalchemy import select

    from likesurgeon.models import DiagnosisItem, Track

    s = _open_db(home)
    try:
        [item] = s.scalars(select(DiagnosisItem)).all()
        track = {t.video_id: t.id for t in s.scalars(select(Track))}
        return item, track["A"], track["B"]
    finally:
        s.close()


def test_compare_likes_relinked_pair_is_write_eligible_with_metadata(
    fake_home: Path,
    patch_metadata_client: type[_FakeYouTubeMetadata],
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_relink(fake_home)
    patch_metadata_client.metadata = _relink_metadata()

    result = CliRunner().invoke(app, ["compare-likes"])

    assert result.exit_code == 0, result.output
    # Both sides of every rendered pair, fetched once.
    assert patch_metadata_client.requested == [["A", "B"]]
    item, a_track, b_track = _only_finding(fake_home)
    assert (item.issue_type, item.confidence) == (ISSUE_RELINKED, 1.0)
    assert (item.source_track_id, item.related_track_id) == (a_track, b_track)
    assert "report-only" not in result.output
    assert "fuzzy_threshold" not in result.output
    assert "sync attempt" not in result.output


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (None, "no YouTube auth"),  # the fake's default: AuthorizationRequiredError
        (_yt_mod.ClientSecretsMissingError("no client secrets"), "no YouTube auth"),
        (RuntimeError("network boom"), "RuntimeError: network boom"),
    ],
)
def test_compare_likes_keeps_pairs_report_only_when_metadata_fetch_fails(
    fake_home: Path,
    patch_metadata_client: type[_FakeYouTubeMetadata],
    error: Exception | None,
    shown: str,
) -> None:
    """Missing auth (fake default) or any other error: one line, then carry on."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_relink(fake_home)
    patch_metadata_client.raise_instead = error

    result = CliRunner().invoke(app, ["compare-likes"])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert shown in out
    assert "report-only" in out
    item, _, _ = _only_finding(fake_home)
    assert (item.issue_type, item.confidence) == (ISSUE_RELINKED, 0.5)


def test_compare_likes_summary_shows_alignment_rows(
    fake_home: Path,
    patch_metadata_client: type[_FakeYouTubeMetadata],
) -> None:
    import re

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ALIGNMENT_ISSUE_TYPES

    _seed_relink(fake_home)
    patch_metadata_client.metadata = _relink_metadata()

    result = CliRunner().invoke(app, ["compare-likes"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    rows = {line for line in result.output.splitlines() if "│" in line or "|" in line}

    def row(label: str) -> str:
        [line] = [r for r in rows if label in r]
        return line

    assert re.search(r"\b3\b", row("YT Music liked songs (LM)"))
    assert re.search(r"\b3\b", row("YouTube liked videos (LL)"))
    assert re.search(r"\b2\b", row("LM entries backed by their own video"))
    assert "1 / 1" in row("relinked (write-eligible / total)")
    assert "0 / 0" in row("shadow_duplicate (write-eligible / total)")
    for issue_type in ALIGNMENT_ISSUE_TYPES:
        assert row(issue_type)


def test_compare_likes_warns_when_a_sync_ran_after_the_older_scan(
    fake_home: Path,
    patch_metadata_client: type[_FakeYouTubeMetadata],
) -> None:
    """Spec §5.1: a sync attempt after the older scan — here after both —
    means the diagnosis may not describe the current lists. The window rules
    themselves are covered by ``sync_attempts_since`` tests."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED
    from likesurgeon.models import Snapshot, SyncAttempt

    def at(hour: int) -> datetime:
        return datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=hour)

    ids = _seed_diagnosis(fake_home, issue_types=[ISSUE_RELINKED])
    _seed_relink(fake_home)
    s = _open_db(fake_home)
    try:
        lm_snap, ll_snap = s.scalars(select(Snapshot).order_by(Snapshot.id)).all()
        lm_snap.created_at, ll_snap.created_at = at(1), at(3)
        s.add(
            SyncAttempt(
                diagnosis_item_id=ids[ISSUE_RELINKED],
                kind="yt_unlike",
                status="applied",
                reason="r",
                created_at=at(4),
            )
        )
        s.commit()
    finally:
        s.close()

    result = CliRunner().invoke(app, ["compare-likes"])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "1 sync attempt(s) ran after the older of the two scans" in out
    assert "Re-scan both sources" in out


def test_compare_likes_shadow_duplicate_from_a_duplicated_lm_entry(
    fake_home: Path,
    patch_metadata_client: type[_FakeYouTubeMetadata],
) -> None:
    """LL [a1, A, T, a4] / LM [a1, T, T, a4]: T's own like anchors one copy and
    the other copy renders A. Only the raw (non-deduped) LM shows this."""
    import re

    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.compare import CanonicalMetadata
    from likesurgeon.diagnosis import ISSUE_SHADOW_DUPLICATE
    from likesurgeon.models import DiagnosisItem, Track
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(fake_home)
    try:
        create_snapshot(
            s,
            "ytmusic_liked_songs",
            [_ytm_raw(v, f"Song {v}", ["X"]) for v in ("a1", "T", "T", "a4")],
        )
        ll = [_yt_raw(v, f"Song {v}") for v in ("a1", "A", "T", "a4")]
        ll[1]["_likesurgeon_video_status"] = {"is_available": True, "reason": None}
        create_snapshot(s, "youtube_liked_videos", ll)
        s.commit()
    finally:
        s.close()
    patch_metadata_client.metadata = {
        vid: CanonicalMetadata(video_id=vid, title="Song", channel_id="UC1", duration_seconds=200)
        for vid in ("A", "T")
    }

    result = CliRunner().invoke(app, ["compare-likes"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert patch_metadata_client.requested == [["A", "T"]]
    [lm_row] = [r for r in result.output.splitlines() if "YT Music liked songs (LM)" in r]
    assert re.search(r"\b4\b", lm_row)
    s = _open_db(fake_home)
    try:
        [item] = s.scalars(select(DiagnosisItem)).all()
        a_track = s.scalar(select(Track.id).where(Track.video_id == "A"))
        t_track = s.scalar(
            select(Track.id).where(Track.source == "ytmusic_liked_songs", Track.video_id == "T")
        )
    finally:
        s.close()
    assert (item.issue_type, item.confidence) == (ISSUE_SHADOW_DUPLICATE, 1.0)
    assert (item.source_track_id, item.related_track_id) == (a_track, t_track)


def test_compare_likes_warns_that_fuzzy_threshold_is_ignored(
    fake_home: Path,
    patch_metadata_client: type[_FakeYouTubeMetadata],
) -> None:
    import json

    from likesurgeon.cli import app

    _seed_relink(fake_home)
    (fake_home / "config.json").write_text(json.dumps({"fuzzy_threshold": 80}))

    result = CliRunner().invoke(app, ["compare-likes"])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "fuzzy_threshold" in out
    assert "ignored" in out


def test_invalid_fuzzy_threshold_in_config_exits_2_cleanly(
    fake_home: Path,
) -> None:
    """A fuzzy_threshold of 150 in config.json must produce exit code 2
    with the invalid value in output and no traceback."""
    import json

    from likesurgeon.cli import app

    (fake_home / "config.json").write_text(json.dumps({"region": "KR", "fuzzy_threshold": 150}))

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 2
    assert "150" in result.output
    assert "Traceback" not in result.output


def test_issues_type_filter_uses_alignment_issue_types(fake_home: Path) -> None:
    import json

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED, ISSUE_UNBACKED_LM_ENTRY

    _seed_diagnosis(fake_home, issue_types=[ISSUE_RELINKED, ISSUE_UNBACKED_LM_ENTRY])
    runner = CliRunner()

    result = runner.invoke(app, ["issues", "--type", "relinked", "--format", "json"])
    assert result.exit_code == 0, result.output
    [item] = json.loads(result.stdout)["items"]
    assert item["issue_type"] == ISSUE_RELINKED

    for old_type in ("ytmusic_only", "duplicate_in_source", "possible_pointer_drift"):
        result = runner.invoke(app, ["issues", "--type", old_type])
        assert result.exit_code == 2, result.output
        assert "Unknown issue type" in result.output

    help_text = " ".join(runner.invoke(app, ["issues", "--help"]).output.split())
    assert "unbacked_lm_entry" in help_text
    assert "ytmusic_only" not in help_text


def test_doctor_reports_alignment_counts_and_lm_backed_share(
    fake_home: Path,
    patch_metadata_client: type[_FakeYouTubeMetadata],
) -> None:
    from likesurgeon.cli import app

    _seed_relink(fake_home)
    runner = CliRunner()
    assert runner.invoke(app, ["compare-likes"]).exit_code == 0

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "1 relinked" in out
    assert "0 unbacked_lm_entry" in out
    assert "LM backed: 100.0%" in out
    assert "Match-rate" not in out


def test_issues_rejects_unknown_type(fake_home: Path) -> None:
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["issues", "--type", "ytmusic-only"])

    assert result.exit_code == 2, result.output
    assert "Unknown issue type" in result.output


def test_out_of_range_numeric_options_are_rejected(fake_home: Path) -> None:
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["sync", "--limit", "-1"])
    assert result.exit_code == 2, result.output


def test_skip_and_unskip_flip_latest_findings(fake_home: Path) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_DEAD_UNRENDERED, ISSUE_UNBACKED_LM_ENTRY
    from likesurgeon.models import DiagnosisItem

    ids = _seed_diagnosis(fake_home, issue_types=[ISSUE_DEAD_UNRENDERED, ISSUE_UNBACKED_LM_ENTRY])
    ghost, ytm_only = ids[ISSUE_DEAD_UNRENDERED], ids[ISSUE_UNBACKED_LM_ENTRY]
    runner = CliRunner()

    result = runner.invoke(app, ["skip", str(ghost), str(ytm_only)])
    assert result.exit_code == 0, result.output
    # Idempotent: skipping an already-skipped finding is fine.
    assert runner.invoke(app, ["skip", str(ghost)]).exit_code == 0
    result = runner.invoke(app, ["unskip", str(ytm_only)])
    assert result.exit_code == 0, result.output

    s = _open_db(fake_home)
    try:
        assert s.get(DiagnosisItem, ghost).status == "skipped"
        assert s.get(DiagnosisItem, ytm_only).status == "open"
    finally:
        s.close()


def test_skip_is_all_or_nothing_on_bad_ids(fake_home: Path) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_DEAD_UNRENDERED, ISSUE_UNBACKED_LM_ENTRY
    from likesurgeon.models import DiagnosisItem

    ids = _seed_diagnosis(fake_home, issue_types=[ISSUE_DEAD_UNRENDERED, ISSUE_UNBACKED_LM_ENTRY])
    s = _open_db(fake_home)
    try:
        s.get(DiagnosisItem, ids[ISSUE_UNBACKED_LM_ENTRY]).status = "applied"
        s.commit()
    finally:
        s.close()

    result = CliRunner().invoke(
        app, ["skip", str(ids[ISSUE_DEAD_UNRENDERED]), str(ids[ISSUE_UNBACKED_LM_ENTRY]), "9999"]
    )

    assert result.exit_code == 2, result.output
    out = " ".join(result.output.split())
    assert "is 'applied'" in out
    assert "#9999 is not a finding of the latest diagnosis" in out
    s = _open_db(fake_home)
    try:
        assert s.get(DiagnosisItem, ids[ISSUE_DEAD_UNRENDERED]).status == "open"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# canonicalize (report mode). Discovery reads only YT Music: liked songs and
# each artist's songs list, both served by ``_FakeYTMusicDiscovery``.
# ---------------------------------------------------------------------------

_ATV = "MUSIC_VIDEO_TYPE_ATV"


def _atv(video_id: str, album: str, *, artist: str = "ch_art") -> dict[str, Any]:
    """A ytmusicapi track dict: an ATV of "Paper Lantern", 200 s, by ``artist``."""
    return {
        "videoId": video_id,
        "title": "Paper Lantern" if video_id != "vid_listed" else "Other Song",
        "album": {"name": album, "id": f"alb_{video_id}"},
        "artists": [{"name": f"Name {artist}", "id": artist}],
        "videoType": _ATV,
        "duration_seconds": 200,
    }


class _FakeYTMusicDiscovery:
    """``YTMusicClient`` for `canonicalize`: class-level liked songs and artist
    pages. ``during_artist_read`` runs inside ``fetch_artist_songs``, before
    any result is returned, so a test can look at the DB mid-run."""

    liked: list[dict[str, Any]] = []
    liked_error: Exception | None = None
    artists: dict[str, Any] = {}
    artist_errors: dict[str, str] = {}
    during_artist_read: Any = None
    artist_reads: list[list[str]] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    def fetch_liked_songs(self, limit: int | None = None) -> list[dict[str, Any]]:
        if type(self).liked_error is not None:
            raise type(self).liked_error
        return [dict(s) for s in type(self).liked]

    def fetch_artist_songs(self, channel_ids: Any, on_progress: Any = None) -> tuple[dict, dict]:
        ids = list(channel_ids)
        type(self).artist_reads.append(ids)
        if type(self).during_artist_read is not None:
            type(self).during_artist_read()
        errors = {i: msg for i, msg in type(self).artist_errors.items() if i in ids}
        results = {i: type(self).artists.get(i) for i in ids if i not in errors}
        for done in range(1, len(ids) + 1):
            if on_progress is not None:
                on_progress(done, len(ids))
        return results, errors


@pytest.fixture
def patch_discovery_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeYTMusicDiscovery]:
    """Swap `cli.YTMusicClient` for the discovery fake. Default data: A
    (``vid_a``, on a single) is missing from its artist's list, which holds
    exactly one same-title ATV B (``vid_b``); ``vid_listed`` is listed."""
    import likesurgeon.cli as _cli_mod
    from likesurgeon.ytmusic_client import ArtistSongs

    fake = _FakeYTMusicDiscovery
    fake.liked = [_atv("vid_a", "Paper Lantern - Single"), _atv("vid_listed", "Night Market")]
    fake.liked_error = None
    fake.artists = {
        "ch_art": ArtistSongs(
            top_video_ids=("vid_b",),
            songs=(_atv("vid_b", "Night Market"), _atv("vid_listed", "Night Market")),
        )
    }
    fake.artist_errors = {}
    fake.during_artist_read = None
    fake.artist_reads = []
    monkeypatch.setattr(_cli_mod, "YTMusicClient", fake)
    return fake


def _canonical_items(home: Path) -> list[tuple[int, int, str, str]]:
    """Every canonicalize finding as (diagnosis id, item id, issue type, status)."""
    from sqlalchemy import select

    from likesurgeon.diagnosis import DIAGNOSIS_KIND_CANONICALIZE
    from likesurgeon.models import Diagnosis, DiagnosisItem

    s = _open_db(home)
    try:
        return [
            (it.diagnosis_id, it.id, it.issue_type, it.status)
            for it in s.scalars(
                select(DiagnosisItem)
                .join(Diagnosis, DiagnosisItem.diagnosis_id == Diagnosis.id)
                .where(Diagnosis.kind == DIAGNOSIS_KIND_CANONICALIZE)
                .order_by(DiagnosisItem.id)
            )
        ]
    finally:
        s.close()


def test_canonicalize_reports_a_canonical_version_without_youtube(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import (
        DIAGNOSIS_KIND_CANONICALIZE,
        ISSUE_CANONICAL_VERSION,
        latest_diagnosis,
    )
    from likesurgeon.models import Snapshot

    monkeypatch.setattr(_yt_mod, "YouTubeClient", _FakeYouTubeWrite)

    result = CliRunner().invoke(app, ["canonicalize"], env={"COLUMNS": "250"})

    assert result.exit_code == 0, result.output
    assert patch_discovery_client.artist_reads == [["ch_art"]]
    out = _out(result)
    assert "Liked songs (distinct videos)" in out
    assert "already listed" in out
    [row] = [r for r in result.stdout.splitlines() if ISSUE_CANONICAL_VERSION in r and "vid_a" in r]
    assert "vid_b" in row
    assert "likesurgeon skip" in out
    assert "--apply" in out
    assert _FakeYouTubeWrite.instances == []
    s = _open_db(fake_home)
    try:
        diag = latest_diagnosis(s, kind=DIAGNOSIS_KIND_CANONICALIZE)
        assert diag is not None
        assert diag.youtube_snapshot_id is None
        [snap] = s.scalars(select(Snapshot)).all()
        assert (diag.ytmusic_snapshot_id, snap.source, snap.raw_count) == (
            snap.id,
            "ytmusic_liked_songs",
            2,
        )
        assert latest_diagnosis(s) is None  # not an alignment diagnosis
    finally:
        s.close()
    [(_, _, issue_type, status)] = _canonical_items(fake_home)
    assert (issue_type, status) == (ISSUE_CANONICAL_VERSION, "open")


def test_canonicalize_json_is_one_document_on_stdout(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    import json

    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["canonicalize", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"diagnosis_id", "summary", "artist_errors", "items"}
    assert payload["summary"]["candidates"] == 1
    assert payload["summary"]["listed"] == 1
    assert payload["artist_errors"] == {}
    [item] = payload["items"]
    assert (item["issue_type"], item["source_video_id"], item["related_video_id"]) == (
        "canonical_version",
        "vid_a",
        "vid_b",
    )
    assert item["confidence"] == 1.0
    assert "1/1" in result.stderr  # progress stays off stdout
    assert "\r" not in result.stderr  # not a terminal: no in-place overwrite


def test_canonicalize_failing_liked_songs_read_exits_2(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.models import Snapshot
    from likesurgeon.ytmusic_client import UnexpectedResponseError

    patch_discovery_client.liked_error = UnexpectedResponseError("logged-out response")

    result = CliRunner().invoke(app, ["canonicalize"])

    assert result.exit_code == 2, result.output
    assert "logged-out response" in result.output
    assert patch_discovery_client.artist_reads == []
    s = _open_db(fake_home)
    try:
        assert s.scalars(select(Snapshot)).all() == []
    finally:
        s.close()


def test_canonicalize_unreadable_artist_is_a_warning(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    from likesurgeon.cli import app

    fake = patch_discovery_client
    fake.liked.append(_atv("vid_c", "Lone Single", artist="ch_broken"))
    fake.artist_errors = {"ch_broken": "KeyError: 'header'"}

    result = CliRunner().invoke(app, ["canonicalize"], env={"COLUMNS": "250"})

    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "Name ch_broken" in out
    assert "KeyError: 'header'" in out
    assert len(_canonical_items(fake_home)) == 1  # vid_c gets no finding


def test_canonicalize_artist_page_without_songs_shelf_is_unreadable(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real ``fetch_artist_songs``: an artist page
    (immersive header) that came back without its songs shelf, twice, blocks
    the artist's songs instead of reading as "no songs list"."""
    import json

    import likesurgeon.cli as _cli_mod
    from likesurgeon.cli import app
    from likesurgeon.ytmusic_client import YTMusicClient

    page = {
        "header": {"musicImmersiveHeaderRenderer": {}},
        "contents": {
            "singleColumnBrowseResultsRenderer": {
                "tabs": [
                    {
                        "tabRenderer": {
                            "content": {
                                "sectionListRenderer": {
                                    "contents": [{"musicCarouselShelfRenderer": {"contents": []}}]
                                }
                            }
                        }
                    }
                ]
            }
        },
    }

    class _Raw:
        def get_liked_songs(self, limit: Any = None) -> dict[str, Any]:
            return {"tracks": [_atv("vid_a", "Paper Lantern - Single")]}

        def _send_request(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
            return page

    class _Client(YTMusicClient):
        def _build(self) -> Any:
            return _Raw()

    monkeypatch.setattr(_cli_mod, "YTMusicClient", _Client)
    monkeypatch.setattr("likesurgeon.ytmusic_client.time.sleep", lambda s: None)

    result = CliRunner().invoke(app, ["canonicalize", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "MissingSongsShelfError" in payload["artist_errors"]["ch_art"]
    assert "re-run canonicalize later" in result.stderr
    assert payload["summary"]["unreadable"] == 1
    assert payload["items"] == []


def test_canonicalize_many_unreadable_artists_collapse_to_a_count(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    from likesurgeon.cli import app

    fake = patch_discovery_client
    for n in range(15):
        fake.liked.append(_atv(f"vid_c{n}", "Lone Single", artist=f"ch_broken{n}"))
    fake.artist_errors = {f"ch_broken{n}": "MissingSongsShelfError: gone" for n in range(15)}

    result = CliRunner().invoke(app, ["canonicalize"], env={"COLUMNS": "250"})

    assert result.exit_code == 0, result.output
    assert result.output.count("couldn't be read") == 10
    assert "5 more unreadable artist(s); `canonicalize --json` lists them all" in _out(result)
    assert result.output.count("sometimes omits") == 1
    [row] = [r for r in result.stdout.splitlines() if "unreadable artist (no finding)" in r]
    assert "15" in row

    as_json = CliRunner().invoke(app, ["canonicalize", "--json"], env={"COLUMNS": "250"})
    assert as_json.exit_code == 0, as_json.output
    assert "5 more unreadable artist(s); the JSON report lists them all" in as_json.stderr


def test_canonicalize_second_run_keeps_a_skipped_finding_skipped(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    from likesurgeon.cli import app

    runner = CliRunner()
    assert runner.invoke(app, ["canonicalize"]).exit_code == 0
    [(_, first_id, _, _)] = _canonical_items(fake_home)
    assert runner.invoke(app, ["skip", str(first_id)]).exit_code == 0

    result = runner.invoke(app, ["canonicalize"], env={"COLUMNS": "250"})

    assert result.exit_code == 0, result.output
    assert "Kept 1 finding(s) 'skipped'" in _out(result)
    [row] = [r for r in result.stdout.splitlines() if "open write-eligible / total" in r]
    assert "0 / 1" in row  # the skipped finding isn't one --apply would act on
    first, second = _canonical_items(fake_home)
    assert first[0] != second[0]
    assert second[3] == "skipped"


def test_canonicalize_commits_the_snapshot_before_reading_artists(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    """Spec §4.3: the LM snapshot's timestamp is when LM was observed."""
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.models import Snapshot

    seen: list[tuple[str, int]] = []

    def look() -> None:
        s = _open_db(fake_home)  # another connection: sees committed rows only
        try:
            seen.extend((snap.source, snap.raw_count) for snap in s.scalars(select(Snapshot)))
        finally:
            s.close()

    patch_discovery_client.during_artist_read = look

    result = CliRunner().invoke(app, ["canonicalize"])

    assert result.exit_code == 0, result.output
    assert seen == [("ytmusic_liked_songs", 2)]


def test_a_write_during_the_artist_reads_makes_sync_refuse(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
    patch_metadata_client: type[_FakeYouTubeMetadata],
) -> None:
    """A write made while canonicalize reads artist pages is newer than its LM
    snapshot, so an alignment built on that snapshot is refused (spec §4.3)."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED
    from likesurgeon.models import SyncAttempt
    from likesurgeon.snapshot import create_snapshot

    [item_id] = _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])

    def write() -> None:
        s = _open_db(fake_home)
        try:
            s.add(
                SyncAttempt(
                    diagnosis_item_id=item_id, kind="repoint_unlike", status="applied", reason=""
                )
            )
            s.commit()
        finally:
            s.close()

    patch_discovery_client.during_artist_read = write
    runner = CliRunner()
    assert runner.invoke(app, ["canonicalize"]).exit_code == 0
    s = _open_db(fake_home)
    try:
        create_snapshot(s, "youtube_liked_videos", [_yt_raw("vid_a", "Paper Lantern")])
        s.commit()
    finally:
        s.close()

    result = runner.invoke(app, ["compare-likes"])
    assert result.exit_code == 0, result.output
    assert "1 sync attempt(s) ran after the older of the two scans" in _out(result)

    result = runner.invoke(app, ["sync", "--dry-run"])
    assert result.exit_code == 1, result.output
    assert "Refusing to sync" in _out(result)


def test_issues_type_canonical_lists_the_latest_canonicalize_report(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    import json

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    runner = CliRunner()
    result = runner.invoke(app, ["issues", "--type", "canonical_version"])
    assert result.exit_code == 0, result.output
    assert "Run likesurgeon canonicalize first" in _out(result)

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    assert runner.invoke(app, ["canonicalize"]).exit_code == 0
    [(diag_id, item_id, _, _)] = _canonical_items(fake_home)

    result = runner.invoke(app, ["issues", "--type", "canonical_version", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["diagnosis_id"] == diag_id
    assert [(it["id"], it["source_video_id"]) for it in payload["items"]] == [(item_id, "vid_a")]
    result = runner.invoke(app, ["issues", "--type", "canonical_version"])
    assert f"Canonicalize report #{diag_id} — issues (1)" in _out(result)

    result = runner.invoke(app, ["issues", "--type", "canonical_ambiguous", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"diagnosis_id": diag_id, "items": []}

    # Without a type, issues still lists the alignment diagnosis.
    result = runner.invoke(app, ["issues", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert [it["issue_type"] for it in json.loads(result.stdout)["items"]] == [ISSUE_RELINKED]


def test_sync_dry_run_ignores_a_newer_canonicalize_report(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "A", "B", False)])
    runner = CliRunner()
    assert runner.invoke(app, ["canonicalize"]).exit_code == 0

    result = runner.invoke(app, ["sync", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "\n  repoint: 1" in result.output
    assert "canonical" not in result.output


def test_skip_accepts_ids_from_both_latest_reports_only(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_DEAD_UNRENDERED
    from likesurgeon.models import DiagnosisItem

    ids = _seed_diagnosis(fake_home, issue_types=[ISSUE_DEAD_UNRENDERED])
    alignment_id = ids[ISSUE_DEAD_UNRENDERED]
    runner = CliRunner()
    assert runner.invoke(app, ["canonicalize"]).exit_code == 0
    assert runner.invoke(app, ["canonicalize"]).exit_code == 0
    [(_, older_id, _, _), (_, latest_id, _, _)] = _canonical_items(fake_home)

    # An older report's id fails the whole call.
    result = runner.invoke(app, ["skip", str(alignment_id), str(latest_id), str(older_id)])
    assert result.exit_code == 2, result.output
    assert f"#{older_id} is not a finding of the latest diagnosis" in _out(result)
    assert [st for *_, st in _canonical_items(fake_home)] == ["open", "open"]

    result = runner.invoke(app, ["skip", str(alignment_id), str(latest_id)])
    assert result.exit_code == 0, result.output
    assert "2 finding(s) now 'skipped'" in _out(result)
    result = runner.invoke(app, ["unskip", str(latest_id)])
    assert result.exit_code == 0, result.output

    s = _open_db(fake_home)
    try:
        assert s.get(DiagnosisItem, alignment_id).status == "skipped"
        assert s.get(DiagnosisItem, latest_id).status == "open"
        assert s.get(DiagnosisItem, older_id).status == "open"
    finally:
        s.close()

    help_text = _out(runner.invoke(app, ["skip", "--help"]))
    assert "canonicalize" in help_text


# ---------------------------------------------------------------------------
# canonicalize --apply. The report is seeded on the on-disk DB; the writes run
# against ``FakeAccount`` through the sync fakes (``patch_sync_clients``).
# ---------------------------------------------------------------------------

_CANONICAL_REPOINT_ROWS = [
    "canonical_precheck_a",
    "canonical_precheck_b",
    "canonical_like",
    "canonical_verify",
    "canonical_unlike",
    "canonical_unlike_verify",
    "lm_check",
]


def _seed_canonical(
    home: Path, findings: list[tuple[str, str, bool]], *, title: str = "Paper Lantern"
) -> list[int]:
    """A canonicalize report built like the command builds it: an LM snapshot
    of the A's (in the given order, newest first) and one ``canonical_version``
    per ``(A, B, report_only)``, every A and B titled ``title``; a report-only
    A's album has a version keyword (confidence 0.5). Returns item ids, in
    ``findings`` order."""
    from likesurgeon.canonicalize import Candidate, Match, persist_report, version_keyword
    from likesurgeon.diagnosis import diagnosis_items
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(home)
    try:
        liked = [
            {
                **_atv(a, "Best Live! collection" if report_only else "Paper Lantern - Single"),
                "title": title,
            }
            for a, _, report_only in findings
        ]
        snap = create_snapshot(s, "ytmusic_liked_songs", liked)
        candidates = [
            Candidate(
                a=song,
                matches=(
                    Match(
                        {**_atv(b, "Night Market"), "title": title},
                        "ch_art",
                        in_top=True,
                        liked=False,
                    ),
                ),
                shown=1,
                keyword=version_keyword(song["album"]["name"]),
            )
            for song, (_, b, _) in zip(liked, findings, strict=True)
        ]
        diag, _ = persist_report(s, snap.id, candidates)
        s.commit()
        return [it.id for it in sorted(diagnosis_items(s, diag.id), key=lambda it: it.id)]
    finally:
        s.close()


def test_canonicalize_apply_without_a_report_exits_one(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "PA", "PB", False)])  # alignment only

    result = CliRunner().invoke(app, ["canonicalize", "--apply", "--yes"])

    assert result.exit_code == 1, result.output
    assert "No canonicalize report yet" in result.output
    assert "likesurgeon canonicalize" in result.output
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_canonicalize_apply_moves_the_like_to_b(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    [item_id] = _seed_canonical(fake_home, [("A", "B", False)])
    acct = patch_sync_clients
    acct.liked = ["A"]

    result = CliRunner().invoke(app, ["canonicalize", "--apply", "--yes"], env={"COLUMNS": "250"})

    assert result.exit_code == 0, result.output
    out = _out(result)
    [row] = [r for r in result.stdout.splitlines() if "canonical_repoint" in r and " A " in r]
    assert str(item_id) in row and " B " in row and "Paper Lantern" in row
    assert "canonical_repoint: 1" in out
    assert "104 units" in out
    assert "applied=1 failed=0 skipped=0 restored=0" in out
    assert "re-scan both sources" in out and "compare-likes" in out
    assert _FakeYouTubeWrite.instances[0].scope_calls == 1
    assert acct.rate_calls == [("B", "like"), ("A", "none")]
    statuses, attempts = _sync_rows(fake_home)
    assert statuses == {item_id: "applied"}
    assert [kind for _, kind, status in attempts if status == "applied"] == _CANONICAL_REPOINT_ROWS


def test_canonicalize_apply_declined_prompt_writes_nothing(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    [item_id] = _seed_canonical(fake_home, [("A", "B", False)])
    patch_sync_clients.liked = ["A"]

    result = CliRunner().invoke(app, ["canonicalize", "--apply"], input="n\n")

    assert result.exit_code == 1, result.output
    assert patch_sync_clients.rate_calls == []
    assert _sync_rows(fake_home) == ({item_id: "open"}, [])
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_canonicalize_apply_leaves_skipped_and_report_only_findings_alone(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    skipped, report_only, eligible = _seed_canonical(
        fake_home, [("A1", "B1", False), ("A2", "B2", True), ("A3", "B3", False)]
    )
    runner = CliRunner()
    assert runner.invoke(app, ["skip", str(skipped)]).exit_code == 0
    acct = patch_sync_clients
    acct.liked = ["A1", "A2", "A3"]

    result = runner.invoke(app, ["canonicalize", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert "applied=1" in _out(result)
    assert acct.rate_calls == [("B3", "like"), ("A3", "none")]
    assert _sync_rows(fake_home)[0] == {
        skipped: "skipped",
        report_only: "open",
        eligible: "applied",
    }


def test_canonicalize_apply_include_flagged_acts_on_a_keyword_finding(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    skipped, flagged, eligible = _seed_canonical(
        fake_home, [("A1", "B1", True), ("A2", "B2", True), ("A3", "B3", False)]
    )
    runner = CliRunner()
    assert runner.invoke(app, ["skip", str(skipped)]).exit_code == 0
    acct = patch_sync_clients
    acct.liked = ["A1", "A2", "A3"]

    result = runner.invoke(
        app, ["canonicalize", "--apply", "--include-flagged", "--yes"], env={"COLUMNS": "250"}
    )

    assert result.exit_code == 0, result.output
    [flagged_row] = [r for r in result.stdout.splitlines() if " A2 " in r]
    [eligible_row] = [r for r in result.stdout.splitlines() if " A3 " in r]
    assert "'Live' in album" in flagged_row
    assert "Live" not in eligible_row
    assert "applied=2" in _out(result)
    assert acct.rate_calls == [("B3", "like"), ("A3", "none"), ("B2", "like"), ("A2", "none")]
    assert _sync_rows(fake_home)[0] == {
        skipped: "skipped",
        flagged: "applied",
        eligible: "applied",
    }


def test_canonicalize_apply_limit_takes_the_oldest(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    newer, older = _seed_canonical(fake_home, [("A1", "B1", False), ("A2", "B2", False)])
    acct = patch_sync_clients
    acct.liked = ["A1", "A2"]

    result = CliRunner().invoke(app, ["canonicalize", "--apply", "--yes", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert acct.rate_calls == [("B2", "like"), ("A2", "none")]
    assert _sync_rows(fake_home)[0] == {newer: "open", older: "applied"}


def test_canonicalize_apply_with_nothing_left_builds_no_youtube_client(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    _seed_canonical(fake_home, [("A", "B", False)])
    patch_sync_clients.liked = ["A"]
    runner = CliRunner()
    assert runner.invoke(app, ["canonicalize", "--apply", "--yes"]).exit_code == 0
    _FakeYouTubeWrite.instances = []
    _FakeYTMusicWrite.instances = []

    result = runner.invoke(app, ["canonicalize", "--apply"])  # no prompt either

    assert result.exit_code == 0, result.output
    assert "Nothing to apply" in result.output
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_canonicalize_apply_missing_write_scope_blocks_run(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    [item_id] = _seed_canonical(fake_home, [("A", "B", False)])
    patch_sync_clients.liked = ["A"]
    _FakeYouTubeWrite.has_write_scope_return = False

    result = CliRunner().invoke(app, ["canonicalize", "--apply", "--yes"])

    assert result.exit_code == 1, result.output
    assert "write scope" in result.output
    assert patch_sync_clients.rate_calls == []
    assert _FakeYTMusicWrite.instances == []
    assert _sync_rows(fake_home) == ({item_id: "open"}, [])


def test_canonicalize_apply_failing_ytmusic_auth_blocks_run(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.ytmusic_client import UnexpectedResponseError

    [item_id] = _seed_canonical(fake_home, [("A", "B", False)])
    acct = patch_sync_clients
    acct.liked = ["A"]
    acct.lm_read_errors = {1: UnexpectedResponseError("ytmusicapi request failed (HTTP 401)")}

    result = CliRunner().invoke(app, ["canonicalize", "--apply", "--yes"])

    assert result.exit_code == 1, result.output
    assert "auth check failed" in result.output
    assert acct.rate_calls == []
    assert _sync_rows(fake_home) == ({item_id: "open"}, [])


def test_canonicalize_apply_restore_stops_the_run(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    # A is the older entry in the report, so it goes first.
    second, first = _seed_canonical(fake_home, [("A2", "B2", False), ("A", "B", False)])
    acct = patch_sync_clients
    # LM shows A once, but that entry is D's like; A's own like shows as C.
    acct.liked, acct.renders = ["A2", "A", "D"], {"A": "C", "D": "A"}

    result = CliRunner().invoke(app, ["canonicalize", "--apply", "--yes"])

    assert result.exit_code == 1, result.output
    out = _out(result)
    assert "restored=1" in out
    assert "Stopped early: a canonicalize action needed A re-liked" in out
    assert "1 action(s) not attempted" in out
    assert "A" in acct.liked and "B" not in acct.liked
    assert _sync_rows(fake_home)[0] == {first: "skipped", second: "open"}


@pytest.mark.parametrize(
    "args",
    [
        ["--json", "--apply"],
        ["--limit", "1"],
        ["--yes"],
        ["--json", "--yes"],
        ["--include-flagged"],
    ],
)
def test_canonicalize_rejects_flag_combinations(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
    args: list[str],
) -> None:
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["canonicalize", *args])

    assert result.exit_code == 2, result.output
    assert "No such option" not in result.output
    assert _FakeYTMusicWrite.instances == []


def test_sync_refuses_after_a_canonicalize_apply(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_RELINKED

    _seed_pairs(fake_home, [(ISSUE_RELINKED, "PA", "PB", False)])
    _seed_canonical(fake_home, [("A", "B", False)])
    patch_sync_clients.liked = ["A"]
    runner = CliRunner()
    assert runner.invoke(app, ["canonicalize", "--apply", "--yes"]).exit_code == 0

    result = runner.invoke(app, ["sync", "--dry-run"])

    assert result.exit_code == 1, result.output
    assert "Refusing to sync" in _out(result)


_MARKUP_TITLE = "Song [/x] [bold]"


def test_canonicalize_report_prints_markup_in_titles_literally(
    fake_home: Path,
    patch_discovery_client: type[_FakeYTMusicDiscovery],
) -> None:
    from likesurgeon.cli import app

    fake = patch_discovery_client
    fake.liked = [{**_atv("vid_a", "Paper Lantern - Single"), "title": _MARKUP_TITLE}]
    fake.artists["ch_art"] = type(fake.artists["ch_art"])(
        top_video_ids=(), songs=({**_atv("vid_b", "Night Market"), "title": _MARKUP_TITLE},)
    )

    result = CliRunner().invoke(app, ["canonicalize"], env={"COLUMNS": "400"})

    assert result.exit_code == 0, result.output
    assert _MARKUP_TITLE in result.output


def test_canonicalize_apply_prints_markup_in_titles_literally(
    fake_home: Path,
    patch_sync_clients: FakeAccount,
) -> None:
    from likesurgeon.cli import app

    _seed_canonical(fake_home, [("A", "B", False)], title=_MARKUP_TITLE)
    patch_sync_clients.liked = ["A"]

    result = CliRunner().invoke(app, ["canonicalize", "--apply"], input="n\n")

    assert result.exit_code == 1, result.output
    assert _MARKUP_TITLE in result.output
    assert "Proceed?" in result.output  # reached the prompt
