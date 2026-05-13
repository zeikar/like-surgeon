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
# is module-level on cli.py, so it's monkeypatched directly there.
# ---------------------------------------------------------------------------


class _FakeYouTubeWrite:
    """Stub for the `sync` write path. Records rate_video calls and
    has_write_scope queries; configurable to fail on specific (video_id, rating)
    pairs or to report a missing scope."""

    instances: list[_FakeYouTubeWrite] = []
    has_write_scope_return: bool = True
    rate_raise_on: set[tuple[str, str]] = set()

    def __init__(self, **kwargs: Any) -> None:
        self.scope_calls: int = 0
        self.rate_calls: list[tuple[str, str]] = []
        type(self).instances.append(self)

    def has_write_scope(self) -> bool:
        self.scope_calls += 1
        return type(self).has_write_scope_return

    def rate_video(self, video_id: str, rating: str) -> None:
        from likesurgeon.youtube_client import YouTubeWriteError

        self.rate_calls.append((video_id, rating))
        if (video_id, rating) in type(self).rate_raise_on:
            raise YouTubeWriteError(video_id, rating, "boom")


class _FakeYTMusicWrite:
    """Stub for the YT Music half of `sync`. Records like_song and unlike_song calls."""

    instances: list[_FakeYTMusicWrite] = []
    raise_on: set[str] = set()
    raise_on_unlike: set[str] = set()
    in_library: set[str] = set()

    def __init__(self, **kwargs: Any) -> None:
        self.like_song_calls: list[str] = []
        self.unlike_calls: list[str] = []
        self.is_in_liked_songs_calls: list[tuple[str, int]] = []
        type(self).instances.append(self)

    def like_song(self, video_id: str) -> None:
        """Negative-assertion guard — _try_ytm_like must NOT call this."""
        from likesurgeon.ytmusic_client import YTMusicWriteError

        self.like_song_calls.append(video_id)
        if video_id in type(self).raise_on:
            raise YTMusicWriteError(video_id, "boom")

    def is_in_liked_songs(self, video_id: str, *, limit: int = 10000) -> bool:
        self.is_in_liked_songs_calls.append((video_id, limit))
        return video_id in type(self).in_library

    def unlike_song(self, video_id: str) -> None:
        from likesurgeon.ytmusic_client import YTMusicWriteError

        self.unlike_calls.append(video_id)
        if video_id in type(self).raise_on_unlike:
            raise YTMusicWriteError(video_id, "boom")


@pytest.fixture(autouse=True)
def _reset_sync_fakes(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    """Reset class-level recording state on the sync fakes between tests.
    Also suppresses real sleeps in _try_ytm_like."""
    monkeypatch.setattr("likesurgeon.sync.time.sleep", lambda _s: None)
    _FakeYouTubeWrite.instances = []
    _FakeYouTubeWrite.has_write_scope_return = True
    _FakeYouTubeWrite.rate_raise_on = set()
    _FakeYTMusicWrite.instances = []
    _FakeYTMusicWrite.raise_on = set()
    _FakeYTMusicWrite.raise_on_unlike = set()
    _FakeYTMusicWrite.in_library = set()
    yield


@pytest.fixture
def patch_sync_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap YouTubeClient / YTMusicClient with the sync fakes.

    YouTubeClient is patched on the `youtube_client` module (function-local
    import). YTMusicClient is patched on `cli` (module-level import there).
    """
    import likesurgeon.cli as _cli_mod

    monkeypatch.setattr(_yt_mod, "YouTubeClient", _FakeYouTubeWrite)
    monkeypatch.setattr(_cli_mod, "YTMusicClient", _FakeYTMusicWrite)


def _seed_diagnosis(
    home: Path,
    *,
    issue_types: list[str],
    confidences: list[float] | None = None,
) -> dict[str, int]:
    """Build a diagnosis on the CLI's on-disk DB and return item-id mapping.

    Each issue type creates one DiagnosisItem with a fresh Track (and a
    related Track for drift). Returns ``{issue_type: item_id}``.
    """
    from likesurgeon.db import init_db, make_engine, make_session_factory
    from likesurgeon.diagnosis import (
        ISSUE_POINTER_DRIFT,
    )
    from likesurgeon.models import Diagnosis, DiagnosisItem, Track

    home.mkdir(parents=True, exist_ok=True)
    from likesurgeon.config import DEFAULT_DB_FILENAME

    db_path = home / DEFAULT_DB_FILENAME
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)
    confs = confidences or [1.0] * len(issue_types)

    out: dict[str, int] = {}
    s = factory()
    try:
        diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
        s.add(diag)
        s.flush()
        for i, (issue_type, conf) in enumerate(zip(issue_types, confs, strict=True)):
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
            related_id = None
            if issue_type == ISSUE_POINTER_DRIFT:
                rel = Track(
                    source="ytmusic_liked_songs",
                    video_id=f"rel_{i}",
                    title=f"rel-{i}",
                    artists="[]",
                    canonical_key=f"rk-{i}",
                    dedupe_key=f"rd-{i}",
                )
                s.add(rel)
                s.flush()
                related_id = rel.id
            item = DiagnosisItem(
                diagnosis_id=diag.id,
                issue_type=issue_type,
                confidence=conf,
                reason="diagnosis-time evidence",
                source_track_id=src.id,
                related_track_id=related_id,
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


def test_sync_no_diagnosis_exits_one(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """Empty DB → friendly message + exit 1, no clients ever instantiated."""
    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 1, result.output
    assert "No diagnosis yet" in result.output
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_sync_dry_run_prints_plan_and_writes_nothing(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """--dry-run: summary printed, exit 0, zero SyncAttempt rows, no client calls."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import (
        ISSUE_POINTER_DRIFT,
        ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        ISSUE_UNAVAILABLE_VIDEO,
    )
    from likesurgeon.models import SyncAttempt

    _seed_diagnosis(
        fake_home,
        issue_types=[
            ISSUE_UNAVAILABLE_VIDEO,
            ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
            ISSUE_POINTER_DRIFT,
        ],
        confidences=[1.0, 0.9, 0.99],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Sync plan" in result.output
    assert "yt_unlike: 1" in result.output
    assert "ytm_like: 1" in result.output
    assert "yt_relike: 1" in result.output

    s = _open_db(fake_home)
    try:
        from sqlalchemy import select as _select

        rows = list(s.scalars(_select(SyncAttempt)).all())
    finally:
        s.close()
    assert rows == []


def test_sync_dry_run_with_yes_is_harmless(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """--dry-run --yes: dry-run wins; --yes is ignored, no error."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import SyncAttempt

    _seed_diagnosis(fake_home, issue_types=[ISSUE_UNAVAILABLE_VIDEO])

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--dry-run", "--yes"])

    assert result.exit_code == 0, result.output
    s = _open_db(fake_home)
    try:
        from sqlalchemy import select as _select

        rows = list(s.scalars(_select(SyncAttempt)).all())
    finally:
        s.close()
    assert rows == []
    # No clients should have been built — dry-run returns before client init.
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_sync_confirmation_no_aborts(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """Default flow: prompt declined → typer.Abort, no SyncAttempt rows, no calls."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import SyncAttempt

    _seed_diagnosis(fake_home, issue_types=[ISSUE_UNAVAILABLE_VIDEO])

    runner = CliRunner()
    result = runner.invoke(app, ["sync"], input="n\n")

    assert result.exit_code != 0
    s = _open_db(fake_home)
    try:
        from sqlalchemy import select as _select

        rows = list(s.scalars(_select(SyncAttempt)).all())
    finally:
        s.close()
    assert rows == []
    assert _FakeYouTubeWrite.instances == []
    assert _FakeYTMusicWrite.instances == []


def test_sync_missing_write_scope_blocks_run(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """--yes set but has_write_scope() returns False → exit 1, no rate_video calls."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import SyncAttempt

    _seed_diagnosis(fake_home, issue_types=[ISSUE_UNAVAILABLE_VIDEO])
    _FakeYouTubeWrite.has_write_scope_return = False

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    assert "write scope" in result.output
    # YouTubeClient was built (for the scope check), but rate_video was not called.
    assert len(_FakeYouTubeWrite.instances) == 1
    assert _FakeYouTubeWrite.instances[0].rate_calls == []
    # YTMusicClient was never built — scope failure short-circuits before that.
    assert _FakeYTMusicWrite.instances == []
    s = _open_db(fake_home)
    try:
        from sqlalchemy import select as _select

        rows = list(s.scalars(_select(SyncAttempt)).all())
    finally:
        s.close()
    assert rows == []


def test_sync_happy_path_applies_actions(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """--yes + write scope OK + clients succeed → SyncAttempt rows + status='applied' + exit 0."""
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import (
        ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        ISSUE_UNAVAILABLE_VIDEO,
    )
    from likesurgeon.models import DiagnosisItem, SyncAttempt

    ids = _seed_diagnosis(
        fake_home,
        issue_types=[ISSUE_UNAVAILABLE_VIDEO, ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC],
    )
    ghost_id = ids[ISSUE_UNAVAILABLE_VIDEO]
    missing_id = ids[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC]
    # ytm_like cross-prop: verify must find the song in LM.
    _FakeYTMusicWrite.in_library = {"src_1"}

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes"])

    assert result.exit_code == 0, result.output
    assert "applied=2" in result.output
    assert "failed=0" in result.output

    # Verify the actual mock arg lists.
    yt = _FakeYouTubeWrite.instances[0]
    ytm = _FakeYTMusicWrite.instances[0]
    # yt_unlike for ghost (src_0) + ytm_like cross-prop: unlike+relike for src_1.
    assert yt.rate_calls == [("src_0", "none"), ("src_1", "none"), ("src_1", "like")]
    assert ytm.like_song_calls == []

    s = _open_db(fake_home)
    try:
        ghost_item = s.get(DiagnosisItem, ghost_id)
        missing_item = s.get(DiagnosisItem, missing_id)
        assert ghost_item.status == "applied"
        assert missing_item.status == "applied"
        attempts = list(s.scalars(select(SyncAttempt).order_by(SyncAttempt.id)).all())
        kinds_statuses = [(a.kind, a.status) for a in attempts]
        assert ("yt_unlike", "applied") in kinds_statuses
        assert ("ytm_like_yt_unlike", "applied") in kinds_statuses
        assert ("ytm_like_yt_relike", "applied") in kinds_statuses
        assert ("ytm_like_verify", "applied") in kinds_statuses
    finally:
        s.close()


def test_sync_partial_failure_exits_one(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """One rate_video raises → that item stays 'open', other applied, exit 1."""
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import DiagnosisItem, SyncAttempt

    _seed_diagnosis(
        fake_home,
        issue_types=[ISSUE_UNAVAILABLE_VIDEO, ISSUE_UNAVAILABLE_VIDEO],
    )
    # _seed_diagnosis returns one entry per unique issue_type, so for two
    # ghosts we need to fish the ids out of the DB.
    s = _open_db(fake_home)
    try:
        rows = list(s.scalars(select(DiagnosisItem).order_by(DiagnosisItem.id)).all())
        item_ids = [r.id for r in rows]
    finally:
        s.close()
    assert len(item_ids) == 2

    # Fail on the second video only.
    _FakeYouTubeWrite.rate_raise_on = {("src_1", "none")}

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    assert "applied=1" in result.output
    assert "failed=1" in result.output

    s = _open_db(fake_home)
    try:
        items = {r.id: r for r in s.scalars(select(DiagnosisItem)).all()}
        # First applied, second failed → still 'open'.
        assert items[item_ids[0]].status == "applied"
        assert items[item_ids[1]].status == "open"
        # Reason is the diagnosis-time evidence, not the failure detail.
        assert items[item_ids[1]].reason == "diagnosis-time evidence"
        attempts = list(s.scalars(select(SyncAttempt).order_by(SyncAttempt.id)).all())
        by_item = {a.diagnosis_item_id: a for a in attempts}
        assert by_item[item_ids[0]].status == "applied"
        assert by_item[item_ids[1]].status == "failed"
    finally:
        s.close()


def test_sync_ytm_only_run_requires_youtube_scope(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """ytm_like is in needs_youtube → has_write_scope() IS called; cross-prop uses rate_video."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC

    _seed_diagnosis(fake_home, issue_types=[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC])
    # ytm_like verify needs to find the song in LM.
    _FakeYTMusicWrite.in_library = {"src_0"}

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes"])

    assert result.exit_code == 0, result.output
    # has_write_scope IS consulted because ytm_like is in needs_youtube.
    assert len(_FakeYouTubeWrite.instances) == 1
    assert _FakeYouTubeWrite.instances[0].scope_calls >= 1
    # Cross-prop: unlike then relike via rate_video.
    assert _FakeYouTubeWrite.instances[0].rate_calls == [("src_0", "none"), ("src_0", "like")]
    # like_song was NOT called (new flow uses rate_video, not like_song).
    assert _FakeYTMusicWrite.instances[0].like_song_calls == []


def test_sync_limit_truncates_actions_and_leaves_rest_open(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """--limit N processes only the first N actions; the rest stay 'open'
    so the next sync run picks them up. Used for ramped first runs."""
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import DiagnosisItem, SyncAttempt

    _seed_diagnosis(
        fake_home,
        issue_types=[ISSUE_UNAVAILABLE_VIDEO] * 5,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "applying first 2 of 5" in result.output
    assert "applied=2" in result.output

    yt = _FakeYouTubeWrite.instances[0]
    assert len(yt.rate_calls) == 2
    assert yt.rate_calls == [("src_0", "none"), ("src_1", "none")]

    s = _open_db(fake_home)
    try:
        items = list(s.scalars(select(DiagnosisItem).order_by(DiagnosisItem.id)).all())
        # First two applied; remaining three stay 'open' for next run.
        assert [it.status for it in items] == ["applied", "applied", "open", "open", "open"]
        attempts = list(s.scalars(select(SyncAttempt)).all())
        assert len(attempts) == 2
    finally:
        s.close()


def test_sync_limit_above_action_count_is_noop(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """--limit N where N >= total actions doesn't print a truncation notice
    and processes everything normally."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO

    _seed_diagnosis(fake_home, issue_types=[ISSUE_UNAVAILABLE_VIDEO] * 2)

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes", "--limit", "10"])

    assert result.exit_code == 0, result.output
    assert "applying first" not in result.output
    assert "applied=2" in result.output


def _seed_dedupe_item(home: Path, *, video_id: str, source: str = "ytmusic_liked_songs") -> int:
    """Seed a Diagnosis with one ISSUE_DUPLICATE_IN_SOURCE item. Returns item id."""
    from likesurgeon.config import DEFAULT_DB_FILENAME
    from likesurgeon.db import init_db, make_engine, make_session_factory
    from likesurgeon.diagnosis import ISSUE_DUPLICATE_IN_SOURCE
    from likesurgeon.models import Diagnosis, DiagnosisItem, Track

    home.mkdir(parents=True, exist_ok=True)
    db_path = home / DEFAULT_DB_FILENAME
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)

    s = factory()
    try:
        diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
        s.add(diag)
        s.flush()

        track = Track(
            source=source,
            video_id=video_id,
            title="dup title",
            artists="[]",
            canonical_key="ck-dup",
            dedupe_key="dk-dup",
        )
        s.add(track)
        s.flush()

        item = DiagnosisItem(
            diagnosis_id=diag.id,
            issue_type=ISSUE_DUPLICATE_IN_SOURCE,
            confidence=1.0,
            reason=f"appears 2 times in {source} snapshot (positions: 0, 1)",
            source_track_id=track.id,
            related_track_id=None,
            status="open",
        )
        s.add(item)
        s.flush()
        item_id = item.id
        s.commit()
    finally:
        s.close()
    return item_id


def test_sync_ytm_only_dedupe_success_no_youtube_method_calls(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """ytm_dedupe-only plan: unlike_song is called, has_write_scope and rate_video are NOT."""
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.models import SyncAttempt

    _seed_dedupe_item(fake_home, video_id="dup_vid")

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes"])

    assert result.exit_code == 0, result.output

    yt = _FakeYouTubeWrite.instances[0]
    assert yt.scope_calls == 0, "has_write_scope must not be called for ytm_dedupe-only runs"
    assert yt.rate_calls == [], "rate_video must not be called for ytm_dedupe-only runs"

    ytm = _FakeYTMusicWrite.instances[0]
    assert ytm.unlike_calls == ["dup_vid"]

    assert "ytm_dedupe: 1" in result.output

    s = _open_db(fake_home)
    try:
        attempts = list(s.scalars(select(SyncAttempt)).all())
        assert len(attempts) == 1
        assert attempts[0].kind == "ytm_dedupe"
        assert attempts[0].status == "applied"
    finally:
        s.close()


def test_sync_ytm_only_dedupe_failure_exits_nonzero_but_marks_applied(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """ytm_dedupe that raises: exit non-zero, no YouTube calls, item.status='applied' (terminal-on-attempt)."""
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.models import DiagnosisItem, SyncAttempt

    item_id = _seed_dedupe_item(fake_home, video_id="dup_vid")
    _FakeYTMusicWrite.raise_on_unlike = {"dup_vid"}

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes"])

    assert result.exit_code != 0

    yt = _FakeYouTubeWrite.instances[0]
    assert yt.scope_calls == 0
    assert yt.rate_calls == []

    s = _open_db(fake_home)
    try:
        item = s.get(DiagnosisItem, item_id)
        assert item.status == "applied", "ytm_dedupe is terminal-on-attempt even on failure"
        attempts = list(s.scalars(select(SyncAttempt)).all())
        assert len(attempts) == 1
        assert attempts[0].kind == "ytm_dedupe"
        assert attempts[0].status == "failed"
    finally:
        s.close()


def test_sync_limit_with_mixed_dedupe_and_unlike(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """--limit 1 with one ytm_dedupe + one yt_unlike: exactly one action runs, other stays open."""
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.config import DEFAULT_DB_FILENAME
    from likesurgeon.db import init_db, make_engine, make_session_factory
    from likesurgeon.diagnosis import ISSUE_DUPLICATE_IN_SOURCE, ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import Diagnosis, DiagnosisItem, SyncAttempt, Track

    # Seed a diagnosis with two items in one transaction so they share one Diagnosis row.
    fake_home.mkdir(parents=True, exist_ok=True)
    db_path = fake_home / DEFAULT_DB_FILENAME
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)

    s = factory()
    try:
        diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
        s.add(diag)
        s.flush()

        track_dup = Track(
            source="ytmusic_liked_songs",
            video_id="dup_vid",
            title="dup",
            artists="[]",
            canonical_key="ck-dup",
            dedupe_key="dk-dup",
        )
        track_ghost = Track(
            source="youtube_liked_videos",
            video_id="ghost_vid",
            title="ghost",
            artists="[]",
            canonical_key="ck-ghost",
            dedupe_key="dk-ghost",
        )
        s.add_all([track_dup, track_ghost])
        s.flush()

        item_dup = DiagnosisItem(
            diagnosis_id=diag.id,
            issue_type=ISSUE_DUPLICATE_IN_SOURCE,
            confidence=1.0,
            reason="appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1)",
            source_track_id=track_dup.id,
            related_track_id=None,
            status="open",
        )
        item_ghost = DiagnosisItem(
            diagnosis_id=diag.id,
            issue_type=ISSUE_UNAVAILABLE_VIDEO,
            confidence=1.0,
            reason="unavailable",
            source_track_id=track_ghost.id,
            related_track_id=None,
            status="open",
        )
        s.add_all([item_dup, item_ghost])
        s.flush()
        dup_id = item_dup.id
        ghost_id = item_ghost.id
        s.commit()
    finally:
        s.close()

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes", "--limit", "1"])

    assert result.exit_code == 0, result.output

    ytm = _FakeYTMusicWrite.instances[0]
    yt = _FakeYouTubeWrite.instances[0]

    dedupe_ran = len(ytm.unlike_calls) == 1
    unlike_ran = len(yt.rate_calls) == 1
    # Exactly one of the two actions ran.
    assert dedupe_ran ^ unlike_ran, (
        f"Expected exactly one action; got unlike_calls={ytm.unlike_calls}, rate_calls={yt.rate_calls}"
    )

    s = _open_db(fake_home)
    try:
        items = {r.id: r for r in s.scalars(select(DiagnosisItem)).all()}
        applied_statuses = [v.status for v in items.values() if v.status == "applied"]
        open_statuses = [v.status for v in items.values() if v.status == "open"]
        assert len(applied_statuses) == 1, "exactly one item should be applied"
        assert len(open_statuses) == 1, "exactly one item should remain open"

        attempts = list(s.scalars(select(SyncAttempt)).all())
        assert len(attempts) == 1, "exactly one SyncAttempt row expected"
    finally:
        s.close()

    _ = dup_id, ghost_id  # referenced above via items dict; kept for clarity


# ---------------------------------------------------------------------------
# duplicate_in_source — compare-likes pipeline + canonicalization.
#
# These tests drive the on-disk DB through the CliRunner like the sync tests
# above, then inspect persisted DiagnosisItem rows. Stdout assertions are
# label-presence-only (Rich table column wrapping is brittle); the real
# correctness check is the SQL row count.
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


def test_compare_likes_reports_duplicate_in_source_bucket(
    fake_home: Path,
) -> None:
    """One ytmusic video_id duplicated twice → exactly 1 duplicate_in_source row
    and the bucket label appears in compare-likes stdout."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_DUPLICATE_IN_SOURCE
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(fake_home)
    try:
        create_snapshot(
            s,
            "ytmusic_liked_songs",
            [
                _ytm_raw("dup", "Day by Day", ["X"]),
                _ytm_raw("dup", "Day by Day", ["X"]),
                _ytm_raw("unique", "Other", ["Y"]),
            ],
        )
        create_snapshot(
            s,
            "youtube_liked_videos",
            [_yt_raw("yt_only", "Random Music Video", "MusicVEVO")],
        )
        s.commit()
    finally:
        s.close()

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])

    assert result.exit_code == 0, result.output
    assert "Duplicate likes (within-source)" in result.output

    s = _open_db(fake_home)
    try:
        from sqlalchemy import select as _select

        rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_DUPLICATE_IN_SOURCE)
            ).all()
        )
    finally:
        s.close()
    assert len(rows) == 1


def test_duplicate_does_not_phantom_inflate_ytmusic_only_bucket(fake_home: Path) -> None:
    """Canonicalization removes phantom surplus rows from ytmusic_only: a
    video_id duplicated twice (pre-fix: 2 phantom ``ytmusic_only`` rows)
    must collapse to exactly 1 — the canonical row remains because it
    genuinely has no YT counterpart. The phantom inflation is what
    ``sync`` could previously act on by mistake."""
    from sqlalchemy import select as _select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_YTMUSIC_ONLY
    from likesurgeon.models import DiagnosisItem, Track
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(fake_home)
    try:
        create_snapshot(
            s,
            "ytmusic_liked_songs",
            [
                _ytm_raw("dup", "Day by Day", ["X"]),
                _ytm_raw("dup", "Day by Day", ["X"]),
            ],
        )
        create_snapshot(
            s,
            "youtube_liked_videos",
            [_yt_raw("yt_only", "Random Music Video", "MusicVEVO")],
        )
        s.commit()
    finally:
        s.close()

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])
    assert result.exit_code == 0, result.output

    s = _open_db(fake_home)
    try:
        dup_track = s.scalar(
            _select(Track).where(Track.source == "ytmusic_liked_songs", Track.video_id == "dup")
        )
        assert dup_track is not None
        ytm_only_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(
                    DiagnosisItem.issue_type == ISSUE_YTMUSIC_ONLY,
                    DiagnosisItem.source_track_id == dup_track.id,
                )
            ).all()
        )
    finally:
        s.close()
    # Exactly 1 canonical row — the phantom surplus row is gone.
    assert len(ytm_only_rows) == 1


def test_compare_likes_handles_youtube_side_duplicate(fake_home: Path) -> None:
    """Rare-but-possible: YouTube has a duplicated music-candidate video_id
    with no ytmusic counterpart. Pre-canonicalization, the surplus row
    would surface as a phantom ``possibly_missing_from_ytmusic`` that sync
    would happily act on (this is the bug). After canonicalization: 1
    duplicate_in_source finding tagged to the YouTube side, and the
    canonical row produces exactly 1 ``possibly_missing_from_ytmusic``
    (phantom surplus is gone — only the genuine row remains)."""
    from sqlalchemy import select as _select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import (
        ISSUE_DUPLICATE_IN_SOURCE,
        ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
    )
    from likesurgeon.models import DiagnosisItem, Track
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(fake_home)
    try:
        create_snapshot(
            s,
            "ytmusic_liked_songs",
            [_ytm_raw("other", "Other Song", ["Y"])],
        )
        create_snapshot(
            s,
            "youtube_liked_videos",
            [
                _yt_raw("dup_yt", "Some Music Video (Official MV)", "MusicVEVO"),
                _yt_raw("dup_yt", "Some Music Video (Official MV)", "MusicVEVO"),
            ],
        )
        s.commit()
    finally:
        s.close()

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])
    assert result.exit_code == 0, result.output

    s = _open_db(fake_home)
    try:
        # 1 duplicate_in_source row tagged to the YouTube side.
        dup_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_DUPLICATE_IN_SOURCE)
            ).all()
        )
        assert len(dup_rows) == 1
        assert "youtube_liked_videos" in dup_rows[0].reason

        # Exactly 1 (canonical) possibly_missing row for the duplicated
        # YouTube video_id — the phantom surplus is gone.
        dup_track = s.scalar(
            _select(Track).where(Track.source == "youtube_liked_videos", Track.video_id == "dup_yt")
        )
        assert dup_track is not None
        rows = list(
            s.scalars(
                _select(DiagnosisItem).where(
                    DiagnosisItem.issue_type == ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
                    DiagnosisItem.source_track_id == dup_track.id,
                )
            ).all()
        )
    finally:
        s.close()
    assert len(rows) == 1


def test_issues_filter_by_duplicate_in_source_type(fake_home: Path) -> None:
    """`issues --type duplicate_in_source` returns exactly the 1 dup finding;
    `--type ytmusic_only` does not include the duplicated video_id."""
    from likesurgeon.cli import app
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(fake_home)
    try:
        create_snapshot(
            s,
            "ytmusic_liked_songs",
            [
                _ytm_raw("dup", "Day by Day", ["X"]),
                _ytm_raw("dup", "Day by Day", ["X"]),
            ],
        )
        create_snapshot(
            s,
            "youtube_liked_videos",
            [_yt_raw("yt_only", "Random Music Video", "MusicVEVO")],
        )
        s.commit()
    finally:
        s.close()

    runner = CliRunner()
    assert runner.invoke(app, ["compare-likes"]).exit_code == 0

    dup_result = runner.invoke(app, ["issues", "--type", "duplicate_in_source", "--format", "json"])
    assert dup_result.exit_code == 0, dup_result.output
    import json as _json

    payload = _json.loads(dup_result.stdout)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["issue_type"] == "duplicate_in_source"

    ytm_only = runner.invoke(app, ["issues", "--type", "ytmusic_only", "--format", "json"])
    assert ytm_only.exit_code == 0
    payload2 = _json.loads(ytm_only.stdout)
    # The duplicated "dup" video_id appears in ytmusic_only at most once
    # (its canonical row) — phantom surplus is eliminated by
    # canonicalization.
    dup_in_only = [it for it in payload2["items"] if it["source_video_id"] == "dup"]
    assert len(dup_in_only) <= 1


def test_compare_likes_raw_total_counts_include_duplicates(
    fake_home: Path,
) -> None:
    """User-facing semantic: the raw ``YouTube Music liked songs`` count
    reflects the snapshot's actual row count (= what's in the user's
    account) even when duplicates are present. Buckets reflect post-
    canonicalization findings."""
    from likesurgeon.cli import app
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(fake_home)
    try:
        create_snapshot(
            s,
            "ytmusic_liked_songs",
            [
                _ytm_raw("a", "A", ["X"]),
                _ytm_raw("b", "B", ["Y"]),
                _ytm_raw("dup", "D", ["Z"]),
                _ytm_raw("dup", "D", ["Z"]),
                _ytm_raw("c", "C", ["W"]),
            ],
        )
        create_snapshot(
            s,
            "youtube_liked_videos",
            [_yt_raw("yt_only", "Random Music Video", "MusicVEVO")],
        )
        s.commit()
    finally:
        s.close()

    runner = CliRunner()
    # Use a wide terminal so Rich doesn't wrap the count column.
    result = runner.invoke(app, ["compare-likes"], terminal_width=200)
    assert result.exit_code == 0, result.output
    out = result.output
    # The raw ytmusic count row must reflect 5 (the snapshot has 5 rows),
    # not 4 (the canonicalized matcher view).
    assert "YouTube Music liked songs" in out

    # Stronger check via _PipelineResult directly: the CLI surfaces raw
    # counts in the totals rows while the bucket counts reflect the
    # canonicalized comparison.
    from likesurgeon.cli import _compare_and_persist

    s = _open_db(fake_home)
    try:
        # Fresh pipeline run for the assertion. The CLI invocation above
        # already persisted a Diagnosis; this is purely about the in-
        # memory counts the renderer would feed Rich.
        outcome = _compare_and_persist(s)
        s.rollback()
    finally:
        s.close()
    assert outcome.raw_ytmusic_count == 5
    # compare_result.ytmusic_count is the post-canonicalization view: 4.
    assert outcome.compare_result.ytmusic_count == 4


# ---------------------------------------------------------------------------
# Stage 4 enrichment — compare-likes pipeline.
#
# These tests exercise the Stage 4 drift-detection path wired into
# _compare_and_persist. The YouTubeClient is patched on the youtube_client
# module so the function-local `from .youtube_client import YouTubeClient`
# resolves to the fake. Each test drives the full CLI via CliRunner + fake_home
# and then inspects persisted DiagnosisItem rows.
# ---------------------------------------------------------------------------


class _FakeYouTubeStage4:
    """Stub YouTubeClient for Stage 4 tests.

    Provides fetch_canonical_metadata and the minimal interface that
    compare-likes expects. By default raises AuthorizationRequiredError;
    individual tests override `canonical_metadata_return` on the class.
    """

    canonical_metadata_return: dict | None = None  # None → raise AuthorizationRequiredError
    raise_instead: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        pass

    def fetch_canonical_metadata(self, video_ids: list[str]) -> dict:
        from likesurgeon.youtube_client import AuthorizationRequiredError

        if type(self).raise_instead is not None:
            raise type(self).raise_instead
        if type(self).canonical_metadata_return is None:
            raise AuthorizationRequiredError("no auth")
        return type(self).canonical_metadata_return

    # Satisfy the VideoStatus-based scan path (not called in compare-likes).
    def fetch_video_statuses(self, video_ids: list[str], *, user_region: str | None = None) -> dict:
        return {}


@pytest.fixture(autouse=True)
def _reset_stage4_fake() -> Iterable[None]:
    _FakeYouTubeStage4.canonical_metadata_return = None
    _FakeYouTubeStage4.raise_instead = None
    yield


@pytest.fixture
def patch_stage4_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeYouTubeStage4]:
    monkeypatch.setattr(_yt_mod, "YouTubeClient", _FakeYouTubeStage4)
    return _FakeYouTubeStage4


def _seed_stage4_snapshots(home: Path, *, ghost: bool = False) -> None:
    """Seed one ytm item (video_id='ytm_vid') and one yt item (video_id='yt_vid').

    The two items do NOT match via stages 1-3 (different video_ids and very
    different titles — fuzzy score ~29). The yt item uses an "Artist - Title"
    pattern so it is classified as a music candidate. Stage 4 matches them via
    canonical metadata that shares channel_id + duration + normalized title.

    The yt item has is_available=False if ghost=True.
    """
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(home)
    try:
        create_snapshot(
            s,
            "ytmusic_liked_songs",
            [_ytm_raw("ytm_vid", "Rainbow Dreams", ["Artist"])],
        )
        # "Sunshine Road - Into the Light" triggers the "artist - title" heuristic
        # (score >= threshold) so it is classified as a music candidate.
        yt_raw = _yt_raw("yt_vid", "Sunshine Road - Into the Light", "ArtistVEVO")
        if ghost:
            yt_raw["_likesurgeon_video_status"] = {"is_available": False, "reason": "deleted"}
        create_snapshot(
            s,
            "youtube_liked_videos",
            [yt_raw],
        )
        s.commit()
    finally:
        s.close()


def _stage4_canonical_metadata() -> dict:
    """Return stub metadata where both ytm_vid and yt_vid share channel+duration+norm-title."""
    from likesurgeon.compare import CanonicalMetadata

    return {
        "ytm_vid": CanonicalMetadata(
            video_id="ytm_vid",
            title="Common Song Title",
            channel_id="UCxxx",
            duration_seconds=200,
        ),
        "yt_vid": CanonicalMetadata(
            video_id="yt_vid",
            title="Common Song Title",
            channel_id="UCxxx",
            duration_seconds=200,
        ),
    }


def test_compare_likes_stage4_promotes_drift_with_stub_metadata(
    fake_home: Path,
    patch_stage4_client: type[_FakeYouTubeStage4],
) -> None:
    """Stage 4 promotes the drift pair; reason contains 'normalized title match'."""
    from sqlalchemy import select as _select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POINTER_DRIFT, ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import DiagnosisItem

    _seed_stage4_snapshots(fake_home, ghost=True)
    patch_stage4_client.canonical_metadata_return = _stage4_canonical_metadata()

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])
    assert result.exit_code == 0, result.output

    s = _open_db(fake_home)
    try:
        drift_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_POINTER_DRIFT)
            ).all()
        )
        ghost_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_UNAVAILABLE_VIDEO)
            ).all()
        )
    finally:
        s.close()

    assert len(drift_rows) == 1, f"expected 1 drift row, got {len(drift_rows)}"
    assert "normalized title match" in drift_rows[0].reason
    # yt_vid was consumed by Stage 4 — must NOT also appear as unavailable_video.
    ghost_vids = {r.source_track_id for r in ghost_rows}
    drift_yt_track = drift_rows[0].source_track_id
    assert drift_yt_track not in ghost_vids, "yt_vid must not be double-counted as ghost"


def test_compare_likes_stage4_skips_when_no_yt_auth(
    fake_home: Path,
    patch_stage4_client: type[_FakeYouTubeStage4],
) -> None:
    """AuthorizationRequiredError → exit 0; output mentions 'Stage 4 enrichment skipped'."""
    from sqlalchemy import select as _select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POINTER_DRIFT
    from likesurgeon.models import DiagnosisItem

    _seed_stage4_snapshots(fake_home)
    # Default canonical_metadata_return=None → raises AuthorizationRequiredError.

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])

    assert result.exit_code == 0, result.output
    assert "Stage 4 enrichment skipped" in result.output

    s = _open_db(fake_home)
    try:
        drift_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_POINTER_DRIFT)
            ).all()
        )
    finally:
        s.close()

    assert drift_rows == [], "no Stage 4 drift rows expected when auth fails"


def test_compare_likes_stage4_recovers_from_http_error(
    fake_home: Path,
    patch_stage4_client: type[_FakeYouTubeStage4],
) -> None:
    """A generic exception → exit 0; output mentions the error class; existing stages intact."""
    from sqlalchemy import select as _select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POINTER_DRIFT, ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC
    from likesurgeon.models import DiagnosisItem

    _seed_stage4_snapshots(fake_home)
    patch_stage4_client.raise_instead = RuntimeError("network boom")

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])

    assert result.exit_code == 0, result.output
    assert "Stage 4 enrichment skipped" in result.output
    assert "RuntimeError" in result.output

    s = _open_db(fake_home)
    try:
        drift_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_POINTER_DRIFT)
            ).all()
        )
        pm_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(
                    DiagnosisItem.issue_type == ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC
                )
            ).all()
        )
    finally:
        s.close()

    # No Stage 4 drift promoted.
    assert drift_rows == []
    # Stages 1-3 results persisted: yt_vid was a music candidate not matched → possibly_missing.
    assert len(pm_rows) == 1


def test_compare_likes_stage4_consumed_unavailable_yt_is_not_also_ghost(
    fake_home: Path,
    patch_stage4_client: type[_FakeYouTubeStage4],
) -> None:
    """A ghost yt_vid promoted by Stage 4 must NOT appear as unavailable_video."""
    from sqlalchemy import select as _select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POINTER_DRIFT, ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import DiagnosisItem

    _seed_stage4_snapshots(fake_home, ghost=True)
    patch_stage4_client.canonical_metadata_return = _stage4_canonical_metadata()

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])
    assert result.exit_code == 0, result.output

    s = _open_db(fake_home)
    try:
        drift_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_POINTER_DRIFT)
            ).all()
        )
        ghost_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_UNAVAILABLE_VIDEO)
            ).all()
        )
    finally:
        s.close()

    assert len(drift_rows) == 1
    # The consumed yt_vid must not also appear as a ghost.
    assert len(ghost_rows) == 0, f"expected 0 ghost rows, got {len(ghost_rows)}"


def test_compare_likes_stage4_dedupes_yt_row_in_both_buckets(
    fake_home: Path,
    patch_stage4_client: type[_FakeYouTubeStage4],
) -> None:
    """A yt_vid in both possibly_missing AND ghost enters Stage 4 exactly once → 1 drift row."""
    from sqlalchemy import select as _select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POINTER_DRIFT, ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import DiagnosisItem

    # ghost=True makes yt_vid is_available=False (ghost bucket)
    # AND yt_vid will land in possibly_missing_from_ytmusic because it's a music candidate
    # with no stages 1-3 match.
    _seed_stage4_snapshots(fake_home, ghost=True)
    patch_stage4_client.canonical_metadata_return = _stage4_canonical_metadata()

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])
    assert result.exit_code == 0, result.output

    s = _open_db(fake_home)
    try:
        drift_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_POINTER_DRIFT)
            ).all()
        )
        ghost_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_UNAVAILABLE_VIDEO)
            ).all()
        )
    finally:
        s.close()

    # Exactly one promotion (not doubled).
    assert len(drift_rows) == 1
    # Consumed row excluded from ghost findings.
    assert len(ghost_rows) == 0


def test_compare_likes_stage4_skips_yt_row_already_matched_by_stage_1(
    fake_home: Path,
    patch_stage4_client: type[_FakeYouTubeStage4],
) -> None:
    """A yt_vid matched by stage 1 (same video_id in both sources) is excluded from Stage 4."""
    from sqlalchemy import select as _select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POINTER_DRIFT
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

    s = _open_db(fake_home)
    try:
        # Both sources share the same video_id "shared_vid" → stage 1 exact match.
        # The yt title uses "Artist - Song" so it is classified as a music candidate.
        create_snapshot(
            s,
            "ytmusic_liked_songs",
            [_ytm_raw("shared_vid", "Artist - Shared Song", ["Artist"])],
        )
        yt_raw = _yt_raw("shared_vid", "Artist - Shared Song", "ArtistVEVO")
        yt_raw["_likesurgeon_video_status"] = {"is_available": False, "reason": "deleted"}
        create_snapshot(s, "youtube_liked_videos", [yt_raw])
        s.commit()
    finally:
        s.close()

    # Provide metadata for shared_vid — but Stage 4 should never see it since
    # stage 1 already consumed the track_id from both sides.
    from likesurgeon.compare import CanonicalMetadata

    patch_stage4_client.canonical_metadata_return = {
        "shared_vid": CanonicalMetadata(
            video_id="shared_vid",
            title="Artist - Shared Song",
            channel_id="UCxxx",
            duration_seconds=180,
        ),
    }

    runner = CliRunner()
    result = runner.invoke(app, ["compare-likes"])
    assert result.exit_code == 0, result.output

    s = _open_db(fake_home)
    try:
        drift_rows = list(
            s.scalars(
                _select(DiagnosisItem).where(DiagnosisItem.issue_type == ISSUE_POINTER_DRIFT)
            ).all()
        )
    finally:
        s.close()

    # Stage 1 matched it; Stage 4 must not also promote it as drift.
    assert drift_rows == [], f"expected no drift rows, got {drift_rows}"
