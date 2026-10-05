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

    def __init__(self, **kwargs: Any) -> None:
        # Accept and ignore client_secrets_path / token_path / etc.
        pass

    def fetch_liked_videos(self, limit: int | None = None) -> list[dict[str, Any]]:
        type(self).fetch_limit = limit
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
    _FakeYouTubeClient.fetch_limit = "<unset-sentinel>"
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


def _seed_pairs(home: Path, pairs: list[tuple[str, str, str, bool | None]]) -> list[int]:
    """Real LL / LM scans and a diagnosis built from them, with one write-eligible
    pair finding per ``(issue_type, A, B, A's availability)``. LL holds the A's
    (availability as scanned; None = unknown), LM the B's. Returns item ids."""
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
            s, "ytmusic_liked_songs", [_ytm_raw(b, f"Song {b}", ["X"]) for _, _, b, _ in pairs]
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
                related_track_id=track["ytmusic_liked_songs", b],
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
    assert "154 units" in result.output
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
    acct.quota_after = 0

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

    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "applied=0 failed=0 skipped=1 restored=1" in out
    assert "1 action(s) were undone" in out
    assert "A" in acct.liked
    assert _sync_rows(fake_home)[0] == {item_id: "skipped"}


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
