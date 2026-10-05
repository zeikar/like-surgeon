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
# is module-level on cli.py, so it's monkeypatched directly there.
# ---------------------------------------------------------------------------


class _FakeYouTubeWrite:
    """Stub for the `sync` write path. Records rate_video calls and
    has_write_scope queries; configurable to fail on specific (video_id, rating)
    pairs or to report a missing scope.

    ``in_liked_videos`` controls which video ids are returned as present by
    ``is_in_liked_videos`` (set class-level between tests)."""

    instances: list[_FakeYouTubeWrite] = []
    has_write_scope_return: bool = True
    rate_raise_on: set[tuple[str, str]] = set()
    rate_quota_on: set[tuple[str, str]] = set()
    in_liked_videos: set[str] = set()

    def __init__(self, **kwargs: Any) -> None:
        self.scope_calls: int = 0
        self.rate_calls: list[tuple[str, str]] = []
        self.is_in_liked_videos_calls: list[str] = []
        type(self).instances.append(self)

    def has_write_scope(self) -> bool:
        self.scope_calls += 1
        return type(self).has_write_scope_return

    def rate_video(self, video_id: str, rating: str) -> None:
        from likesurgeon.youtube_client import YouTubeWriteError

        self.rate_calls.append((video_id, rating))
        if (video_id, rating) in type(self).rate_quota_on:
            from likesurgeon.youtube_client import YouTubeQuotaExceededError

            raise YouTubeQuotaExceededError(video_id, rating, "quotaExceeded")
        if (video_id, rating) in type(self).rate_raise_on:
            raise YouTubeWriteError(video_id, rating, "boom")

    def is_in_liked_videos(self, video_id: str) -> bool:
        self.is_in_liked_videos_calls.append(video_id)
        return video_id in type(self).in_liked_videos


class _FakeYTMusicWrite:
    """Stub for the YT Music half of `sync`. Records like_song and unlike_song calls."""

    instances: list[_FakeYTMusicWrite] = []
    probe_error: Exception | None = None
    raise_on: set[str] = set()
    raise_on_unlike: set[str] = set()
    in_library: set[str] = set()

    def __init__(self, **kwargs: Any) -> None:
        self.like_song_calls: list[str] = []
        self.unlike_calls: list[str] = []
        self.is_in_liked_songs_calls: list[tuple[str, int]] = []
        type(self).instances.append(self)

    def fetch_liked_songs(self, limit: int = 5000) -> list[dict[str, Any]]:
        """The sync pre-flight auth probe."""
        if type(self).probe_error is not None:
            raise type(self).probe_error
        return []

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
    _FakeYouTubeWrite.rate_quota_on = set()
    _FakeYouTubeWrite.in_liked_videos = set()
    _FakeYTMusicWrite.instances = []
    _FakeYTMusicWrite.probe_error = None
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


_STAGE4_REASON = "enriched: channel=UCabcdef… + duration=200s + normalized title match"


def _seed_diagnosis(
    home: Path,
    *,
    issue_types: list[str],
    confidences: list[float] | None = None,
    reasons: list[str] | None = None,
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
    rsns = reasons or ["diagnosis-time evidence"] * len(issue_types)

    out: dict[str, int] = {}
    s = factory()
    try:
        diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
        s.add(diag)
        s.flush()
        for i, (issue_type, conf, reason) in enumerate(zip(issue_types, confs, rsns, strict=True)):
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
                reason=reason,
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
        ISSUE_YTMUSIC_ONLY,
    )
    from likesurgeon.models import SyncAttempt

    _seed_diagnosis(
        fake_home,
        issue_types=[
            ISSUE_UNAVAILABLE_VIDEO,
            ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
            ISSUE_POINTER_DRIFT,
            ISSUE_YTMUSIC_ONLY,
        ],
        confidences=[1.0, 0.9, 0.99, 1.0],
        reasons=["r", "r", _STAGE4_REASON, "r"],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Sync plan" in result.output
    assert "yt_unlike: 1" in result.output
    assert "ytm_like: 1" in result.output
    # Action line (2-space indent), not the 4-space skip-breakdown line.
    assert "\n  yt_relike: 1" in result.output
    assert "yt_like: 1" in result.output

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


def test_sync_yt_like_missing_write_scope_blocks_run(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """yt_like is in needs_youtube → missing write scope blocks the run before any rate_video call."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_YTMUSIC_ONLY
    from likesurgeon.models import SyncAttempt

    _seed_diagnosis(fake_home, issue_types=[ISSUE_YTMUSIC_ONLY])
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


def test_sync_yt_like_happy_path(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """yt_like: rate_video(like) called, is_in_liked_videos called, two SyncAttempt rows, item applied."""
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_YTMUSIC_ONLY
    from likesurgeon.models import DiagnosisItem, SyncAttempt

    ids = _seed_diagnosis(fake_home, issue_types=[ISSUE_YTMUSIC_ONLY])
    item_id = ids[ISSUE_YTMUSIC_ONLY]
    # is_in_liked_videos must return True for src_0.
    _FakeYouTubeWrite.in_liked_videos = {"src_0"}

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--yes"])

    assert result.exit_code == 0, result.output
    assert "applied=1" in result.output
    assert "failed=0" in result.output

    yt = _FakeYouTubeWrite.instances[0]
    # rate_video(like) was called.
    assert ("src_0", "like") in yt.rate_calls
    # is_in_liked_videos was called.
    assert "src_0" in yt.is_in_liked_videos_calls

    s = _open_db(fake_home)
    try:
        item = s.get(DiagnosisItem, item_id)
        assert item.status == "applied"
        attempts = list(s.scalars(select(SyncAttempt).order_by(SyncAttempt.id)).all())
        kinds_statuses = [(a.kind, a.status) for a in attempts]
        assert ("yt_like_yt_rate", "applied") in kinds_statuses
        assert ("yt_like_verify", "applied") in kinds_statuses
    finally:
        s.close()


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
    from likesurgeon.models import Diagnosis, DiagnosisItem, Snapshot, Track

    home.mkdir(parents=True, exist_ok=True)
    db_path = home / DEFAULT_DB_FILENAME
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)

    s = factory()
    try:
        # An (empty) YouTube scan behind the diagnosis: without one the
        # dedupe guard can't rule out a YouTube like and skips every dedupe.
        yt_snap = Snapshot(source="youtube_liked_videos", raw_count=0)
        s.add(yt_snap)
        s.flush()
        diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=yt_snap.id)
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
    from likesurgeon.models import Diagnosis, DiagnosisItem, Snapshot, SyncAttempt, Track

    # Seed a diagnosis with two items in one transaction so they share one Diagnosis row.
    fake_home.mkdir(parents=True, exist_ok=True)
    db_path = fake_home / DEFAULT_DB_FILENAME
    engine = make_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)

    s = factory()
    try:
        yt_snap = Snapshot(source="youtube_liked_videos", raw_count=0)
        s.add(yt_snap)
        s.flush()
        diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=yt_snap.id)
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


# ---------------------------------------------------------------------------
# sync: review-fix wiring (fuzzy opt-in, YT Music auth pre-flight, stale
# warning, quota stop, input validation)
# ---------------------------------------------------------------------------

_FUZZY_REASON = "fuzzy match (score 100/100): YT 'Song' ↔ YT Music 'Song (Remix)'"


def test_sync_fuzzy_drift_needs_include_flag(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POINTER_DRIFT

    _seed_diagnosis(fake_home, issue_types=[ISSUE_POINTER_DRIFT], reasons=[_FUZZY_REASON])
    runner = CliRunner()

    result = runner.invoke(app, ["sync", "--yes"])
    assert result.exit_code == 0, result.output
    assert _FakeYouTubeWrite.instances[0].rate_calls == []

    _FakeYouTubeWrite.in_liked_videos = {"rel_0"}  # the like on B lands
    result = runner.invoke(app, ["sync", "--yes", "--include-fuzzy-drift"])
    assert result.exit_code == 0, result.output
    assert _FakeYouTubeWrite.instances[1].rate_calls == [("rel_0", "like"), ("src_0", "none")]


@pytest.mark.parametrize("missing_file", [True, False])
def test_sync_failing_ytmusic_auth_blocks_run(
    fake_home: Path,
    patch_sync_clients: None,
    missing_file: bool,
) -> None:
    """With missing or expired YT Music auth a dedupe would 'fail' and still be
    marked applied (terminal on attempt) — refuse up front instead."""
    from likesurgeon.cli import app
    from likesurgeon.ytmusic_client import AuthFileMissingError, UnexpectedResponseError

    item_id = _seed_dedupe_item(fake_home, video_id="dupvid")
    _FakeYTMusicWrite.probe_error = (
        AuthFileMissingError("No YouTube Music auth file found. Run `likesurgeon auth ytmusic`")
        if missing_file
        else UnexpectedResponseError("ytmusicapi request failed (HTTP 401)")
    )

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    assert "auth check failed" in result.output
    assert _FakeYTMusicWrite.instances[0].unlike_calls == []
    s = _open_db(fake_home)
    try:
        from likesurgeon.models import DiagnosisItem

        assert s.get(DiagnosisItem, item_id).status == "open"
    finally:
        s.close()


def test_sync_warns_when_newer_scan_exists(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    from sqlalchemy import select

    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import Diagnosis
    from likesurgeon.snapshot import create_snapshot

    _seed_diagnosis(fake_home, issue_types=[ISSUE_UNAVAILABLE_VIDEO])
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


def test_sync_quota_exhaustion_stops_and_names_stranded_videos(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC, ISSUE_UNAVAILABLE_VIDEO

    _seed_diagnosis(
        fake_home,
        issue_types=[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC, ISSUE_UNAVAILABLE_VIDEO],
        confidences=[1.0, 0.5],  # keep ytm_like first in plan order
    )
    _FakeYouTubeWrite.rate_quota_on = {("src_0", "like")}

    result = CliRunner().invoke(app, ["sync", "--yes"])

    assert result.exit_code == 1, result.output
    out = " ".join(result.output.split())  # Rich wraps at the runner's 80 columns
    assert "quota exhausted" in out
    assert "1 action(s) left open" in out
    assert "liked nowhere" in out
    assert "src_0" in out
    # The ghost unlike after the quota hit was never attempted.
    assert _FakeYouTubeWrite.instances[0].rate_calls == [("src_0", "none"), ("src_0", "like")]


def test_issues_rejects_unknown_type(fake_home: Path) -> None:
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, ["issues", "--type", "ytmusic-only"])

    assert result.exit_code == 2, result.output
    assert "Unknown issue type" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["sync", "--limit", "-1"],
        ["sync", "--drift-min-confidence", "1.5"],
    ],
)
def test_out_of_range_numeric_options_are_rejected(fake_home: Path, args: list[str]) -> None:
    from likesurgeon.cli import app

    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output


def test_sync_surfaces_and_prioritizes_videos_stranded_by_an_earlier_run(
    fake_home: Path,
    patch_sync_clients: None,
) -> None:
    """A previous quota stop left src_1 unliked: this run must say so and put
    its re-like first, ahead of higher-confidence actions."""
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC, ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import SyncAttempt

    ids = _seed_diagnosis(
        fake_home,
        issue_types=[ISSUE_UNAVAILABLE_VIDEO, ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC],
        confidences=[1.0, 0.9],
    )
    s = _open_db(fake_home)
    try:
        s.add_all(
            [
                SyncAttempt(
                    diagnosis_item_id=ids[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC],
                    kind="ytm_like_yt_unlike",
                    status="applied",
                    reason="rate(none) ok",
                ),
                SyncAttempt(
                    diagnosis_item_id=ids[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC],
                    kind="ytm_like_yt_relike",
                    status="failed",
                    reason="quotaExceeded",
                ),
            ]
        )
        s.commit()
    finally:
        s.close()
    _FakeYTMusicWrite.in_library = {"src_1"}

    result = CliRunner().invoke(app, ["sync", "--yes", "--limit", "1"])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "liked nowhere right now: src_1" in out
    # --limit 1 picked the stranded re-like, not the 1.0-confidence ghost unlike.
    assert _FakeYouTubeWrite.instances[0].rate_calls == [("src_1", "none"), ("src_1", "like")]


def test_skip_and_unskip_flip_latest_findings(fake_home: Path) -> None:
    from likesurgeon.cli import app
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO, ISSUE_YTMUSIC_ONLY
    from likesurgeon.models import DiagnosisItem

    ids = _seed_diagnosis(fake_home, issue_types=[ISSUE_UNAVAILABLE_VIDEO, ISSUE_YTMUSIC_ONLY])
    ghost, ytm_only = ids[ISSUE_UNAVAILABLE_VIDEO], ids[ISSUE_YTMUSIC_ONLY]
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
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO, ISSUE_YTMUSIC_ONLY
    from likesurgeon.models import DiagnosisItem

    ids = _seed_diagnosis(fake_home, issue_types=[ISSUE_UNAVAILABLE_VIDEO, ISSUE_YTMUSIC_ONLY])
    s = _open_db(fake_home)
    try:
        s.get(DiagnosisItem, ids[ISSUE_YTMUSIC_ONLY]).status = "applied"
        s.commit()
    finally:
        s.close()

    result = CliRunner().invoke(
        app, ["skip", str(ids[ISSUE_UNAVAILABLE_VIDEO]), str(ids[ISSUE_YTMUSIC_ONLY]), "9999"]
    )

    assert result.exit_code == 2, result.output
    out = " ".join(result.output.split())
    assert "is 'applied'" in out
    assert "#9999 is not a finding of the latest diagnosis" in out
    s = _open_db(fake_home)
    try:
        assert s.get(DiagnosisItem, ids[ISSUE_UNAVAILABLE_VIDEO]).status == "open"
    finally:
        s.close()
