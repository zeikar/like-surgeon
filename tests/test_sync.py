"""Tests for the planner + dispatcher in ``likesurgeon.sync``."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from likesurgeon.db import make_session_factory
from likesurgeon.diagnosis import (
    ISSUE_DUPLICATE_IN_SOURCE,
    ISSUE_METADATA_DRIFT,
    ISSUE_POINTER_DRIFT,
    ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
    ISSUE_UNAVAILABLE_VIDEO,
    ISSUE_YTMUSIC_ONLY,
)
from likesurgeon.models import Diagnosis, DiagnosisItem, SyncAttempt, Track
from likesurgeon.sync import (
    ExecResult,
    PlannedAction,
    SkipRecord,
    _DuplicateReason,
    _parse_duplicate_in_source_reason,
    execute,
    plan,
    resolve_video_ids,
    summarize,
)
from likesurgeon.youtube_client import YouTubeWriteError
from likesurgeon.ytmusic_client import YTMusicWriteError

# ---------------------------------------------------------------------------
# Fakes — minimal stubs that record calls and (optionally) raise.
# ---------------------------------------------------------------------------


class FakeYouTube:
    """Records every ``rate_video`` call. Configure ``raise_on`` to inject
    a ``YouTubeWriteError`` for a specific (video_id, rating) pair."""

    def __init__(self, raise_on: set[tuple[str, str]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raise_on = raise_on or set()

    def rate_video(self, video_id: str, rating: str) -> None:
        self.calls.append((video_id, rating))
        if (video_id, rating) in self._raise_on:
            raise YouTubeWriteError(video_id, rating, "boom")


class FakeYTMusic:
    def __init__(
        self,
        raise_on: set[str] | None = None,
        raise_on_unlike: set[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._raise_on = raise_on or set()
        self.unlike_calls: list[str] = []
        self.raise_on_unlike: set[str] = raise_on_unlike or set()

    def like_song(self, video_id: str) -> None:
        self.calls.append(video_id)
        if video_id in self._raise_on:
            raise YTMusicWriteError(video_id, "boom")

    def unlike_song(self, video_id: str) -> None:
        self.unlike_calls.append(video_id)
        if video_id in self.raise_on_unlike:
            raise YTMusicWriteError(video_id, "boom")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_track(session: Session, video_id: str | None, *, suffix: str) -> Track:
    """Create a minimal Track row. ``video_id`` may be None to model the
    "track exists but no video_id" case the planner must skip."""
    track = Track(
        source="youtube_liked_videos",
        video_id=video_id,
        title=f"t-{suffix}",
        artists="[]",
        canonical_key=f"k-{suffix}",
        dedupe_key=f"d-{suffix}",
    )
    session.add(track)
    session.flush()
    return track


def _make_diagnosis(session: Session) -> Diagnosis:
    diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
    session.add(diag)
    session.flush()
    return diag


def _make_item(
    session: Session,
    diag: Diagnosis,
    *,
    issue_type: str,
    confidence: float = 1.0,
    source_track: Track | None = None,
    related_track: Track | None = None,
    status: str = "open",
    reason: str = "diagnosis-time evidence",
) -> DiagnosisItem:
    it = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type=issue_type,
        confidence=confidence,
        reason=reason,
        source_track_id=source_track.id if source_track else None,
        related_track_id=related_track.id if related_track else None,
        status=status,
    )
    session.add(it)
    session.flush()
    return it


# ---------------------------------------------------------------------------
# resolve_video_ids
# ---------------------------------------------------------------------------


def test_resolve_video_ids_happy_path(session: Session) -> None:
    diag = _make_diagnosis(session)
    t1 = _make_track(session, "vid_a", suffix="a")
    t2 = _make_track(session, "vid_b", suffix="b")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_POINTER_DRIFT,
        source_track=t1,
        related_track=t2,
    )
    session.commit()

    out = resolve_video_ids(session, [item])

    assert out == {t1.id: "vid_a", t2.id: "vid_b"}


def test_resolve_video_ids_excludes_tracks_without_video_id(session: Session) -> None:
    """A track row that exists but has video_id=NULL/empty must NOT appear in
    the result — the planner relies on that absence to emit a SkipRecord."""
    diag = _make_diagnosis(session)
    t_present = _make_track(session, "vid_x", suffix="x")
    t_missing = _make_track(session, None, suffix="y")
    t_empty = _make_track(session, "", suffix="z")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_UNAVAILABLE_VIDEO,
        source_track=t_present,
    )
    item2 = _make_item(
        session,
        diag,
        issue_type=ISSUE_UNAVAILABLE_VIDEO,
        source_track=t_missing,
    )
    item3 = _make_item(
        session,
        diag,
        issue_type=ISSUE_UNAVAILABLE_VIDEO,
        source_track=t_empty,
    )
    session.commit()

    out = resolve_video_ids(session, [item, item2, item3])

    assert out == {t_present.id: "vid_x"}


def test_resolve_video_ids_chunks_above_sqlite_limit(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite caps `IN(...)` at 999 params on older builds. Force a small
    batch size and feed enough tracks that we cross multiple chunk boundaries."""
    from likesurgeon import sync as _sync_mod

    monkeypatch.setattr(_sync_mod, "_TRACK_LOOKUP_BATCH_SIZE", 50)

    diag = _make_diagnosis(session)
    n = 130  # > 2 × batch — at least 3 chunks
    tracks = [_make_track(session, f"vid_{i:04d}", suffix=str(i)) for i in range(n)]
    items = [
        _make_item(
            session,
            diag,
            issue_type=ISSUE_UNAVAILABLE_VIDEO,
            source_track=t,
        )
        for t in tracks
    ]
    session.commit()

    out = resolve_video_ids(session, items)

    assert len(out) == n
    for i, t in enumerate(tracks):
        assert out[t.id] == f"vid_{i:04d}"


# ---------------------------------------------------------------------------
# plan — finding-type mapping
# ---------------------------------------------------------------------------


def test_plan_unavailable_video_maps_to_yt_unlike(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "ghost_vid", suffix="g")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_UNAVAILABLE_VIDEO,
        source_track=t,
    )
    session.commit()

    actions, skips = plan([item], {t.id: "ghost_vid"}, drift_min_confidence=0.95)

    assert skips == []
    assert actions == [
        PlannedAction(
            item_id=item.id,
            kind="yt_unlike",
            primary_video_id="ghost_vid",
            secondary_video_id=None,
        )
    ]


def test_plan_possibly_missing_maps_to_ytm_like(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "missing_vid", suffix="m")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        source_track=t,
    )
    session.commit()

    actions, skips = plan([item], {t.id: "missing_vid"}, drift_min_confidence=0.95)

    assert skips == []
    assert actions == [
        PlannedAction(
            item_id=item.id,
            kind="ytm_like",
            primary_video_id="missing_vid",
            secondary_video_id=None,
        )
    ]


def test_plan_drift_above_threshold_maps_to_yt_relike(session: Session) -> None:
    """Drift action: primary=related (LIKE target), secondary=source (UNLIKE target)."""
    diag = _make_diagnosis(session)
    src = _make_track(session, "src_vid", suffix="s")
    rel = _make_track(session, "rel_vid", suffix="r")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_POINTER_DRIFT,
        confidence=0.97,
        source_track=src,
        related_track=rel,
    )
    session.commit()

    actions, skips = plan(
        [item],
        {src.id: "src_vid", rel.id: "rel_vid"},
        drift_min_confidence=0.95,
    )

    assert skips == []
    assert actions == [
        PlannedAction(
            item_id=item.id,
            kind="yt_relike",
            primary_video_id="rel_vid",
            secondary_video_id="src_vid",
        )
    ]


def test_plan_drift_threshold_boundary(session: Session) -> None:
    """``confidence >= threshold`` is the inclusive boundary."""
    diag = _make_diagnosis(session)
    src = _make_track(session, "src_vid", suffix="s")
    rel = _make_track(session, "rel_vid", suffix="r")
    # Exactly at threshold — included.
    at = _make_item(
        session,
        diag,
        issue_type=ISSUE_POINTER_DRIFT,
        confidence=0.95,
        source_track=src,
        related_track=rel,
    )
    # Just below — skipped.
    src2 = _make_track(session, "src2", suffix="s2")
    rel2 = _make_track(session, "rel2", suffix="r2")
    below = _make_item(
        session,
        diag,
        issue_type=ISSUE_POINTER_DRIFT,
        confidence=0.94999,
        source_track=src2,
        related_track=rel2,
    )
    session.commit()

    actions, skips = plan(
        [at, below],
        {src.id: "src_vid", rel.id: "rel_vid", src2.id: "src2", rel2.id: "rel2"},
        drift_min_confidence=0.95,
    )

    assert [a.item_id for a in actions] == [at.id]
    assert len(skips) == 1
    assert skips[0].item_id == below.id
    assert skips[0].kind == "yt_relike"
    assert "0.94999" in skips[0].reason
    assert "0.95" in skips[0].reason


def test_plan_skips_already_applied_items_silently(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "vid", suffix="x")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_UNAVAILABLE_VIDEO,
        source_track=t,
        status="applied",
    )
    session.commit()

    actions, skips = plan([item], {t.id: "vid"}, drift_min_confidence=0.95)

    assert actions == []
    assert skips == []


def test_plan_skips_manually_skipped_items_silently(session: Session) -> None:
    """``DiagnosisItem.status='skipped'`` is a permanent manual override
    (e.g. a private/deleted YouTube ghost that ``videos.rate`` can't unlike
    anyway). The planner must treat it as terminal — same as 'applied' —
    rather than re-emitting the action every run."""
    diag = _make_diagnosis(session)
    t = _make_track(session, "vid", suffix="m")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_UNAVAILABLE_VIDEO,
        source_track=t,
        status="skipped",
    )
    session.commit()

    actions, skips = plan([item], {t.id: "vid"}, drift_min_confidence=0.95)

    assert actions == []
    assert skips == []


def test_plan_missing_video_id_emits_skip_with_action_kind(session: Session) -> None:
    """A finding whose required video_id isn't in the lookup must produce a
    SkipRecord whose ``kind`` matches the action it WOULD have been."""
    diag = _make_diagnosis(session)
    t1 = _make_track(session, None, suffix="g")  # ghost without video_id
    t2 = _make_track(session, None, suffix="m")  # missing without video_id
    src = _make_track(session, None, suffix="s")  # drift without source video_id
    rel = _make_track(session, "rel_vid", suffix="r")
    ghost = _make_item(
        session,
        diag,
        issue_type=ISSUE_UNAVAILABLE_VIDEO,
        source_track=t1,
    )
    missing = _make_item(
        session,
        diag,
        issue_type=ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        source_track=t2,
    )
    drift = _make_item(
        session,
        diag,
        issue_type=ISSUE_POINTER_DRIFT,
        confidence=0.99,
        source_track=src,
        related_track=rel,
    )
    session.commit()

    actions, skips = plan(
        [ghost, missing, drift],
        {rel.id: "rel_vid"},  # only rel has a video_id
        drift_min_confidence=0.95,
    )

    assert actions == []
    by_id = {s.item_id: s for s in skips}
    assert by_id[ghost.id].kind == "yt_unlike"
    assert by_id[missing.id].kind == "ytm_like"
    assert by_id[drift.id].kind == "yt_relike"
    for s in skips:
        assert "no video_id" in s.reason


def test_plan_ignores_ytmusic_only_and_metadata_drift(session: Session) -> None:
    """Informational findings: planner must produce neither action nor skip."""
    diag = _make_diagnosis(session)
    t1 = _make_track(session, "v1", suffix="1")
    t2 = _make_track(session, "v2", suffix="2")
    only = _make_item(
        session,
        diag,
        issue_type=ISSUE_YTMUSIC_ONLY,
        source_track=t1,
    )
    drift = _make_item(
        session,
        diag,
        issue_type=ISSUE_METADATA_DRIFT,
        source_track=t2,
    )
    session.commit()

    actions, skips = plan([only, drift], {t1.id: "v1", t2.id: "v2"}, drift_min_confidence=0.95)

    assert actions == []
    assert skips == []


def test_plan_duplicate_in_source_ytmusic_n2_emits_ytm_dedupe_alongside_actionable(
    session: Session,
) -> None:
    """A ytmusic-source N=2 duplicate emits a ytm_dedupe action, while an
    actionable finding in the same diagnosis still produces its own action."""
    diag = _make_diagnosis(session)
    dup_t = _make_track(session, "dup", suffix="dup")
    ghost_t = _make_track(session, "ghost", suffix="g")
    dup = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=dup_t,
        reason="appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1)",
    )
    ghost = _make_item(
        session,
        diag,
        issue_type=ISSUE_UNAVAILABLE_VIDEO,
        source_track=ghost_t,
    )
    session.commit()

    actions, skips = plan(
        [dup, ghost],
        {dup_t.id: "dup", ghost_t.id: "ghost"},
        drift_min_confidence=0.95,
    )

    assert skips == []
    assert len(actions) == 2
    assert actions[0].item_id == dup.id
    assert actions[0].kind == "ytm_dedupe"
    assert actions[0].primary_video_id == "dup"
    assert actions[0].secondary_video_id is None
    assert actions[1].item_id == ghost.id
    assert actions[1].kind == "yt_unlike"


def test_plan_duplicate_in_source_ytmusic_n3_emits_skip(session: Session) -> None:
    """N=3 duplicates are not auto-handled in 0.5 — planner emits a SkipRecord
    with a reason containing 'count=3'."""
    diag = _make_diagnosis(session)
    dup_t = _make_track(session, "dup3", suffix="dup3")
    dup = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=dup_t,
        reason="appears 3 times in ytmusic_liked_songs snapshot (positions: 0, 1, 5)",
    )
    session.commit()

    actions, skips = plan(
        [dup],
        {dup_t.id: "dup3"},
        drift_min_confidence=0.95,
    )

    assert actions == []
    assert len(skips) == 1
    assert skips[0].item_id == dup.id
    assert skips[0].kind == "ytm_dedupe"
    assert "count=3" in skips[0].reason


def test_plan_duplicate_in_source_youtube_emits_skip(session: Session) -> None:
    """Duplicates in the youtube_liked_videos source are not handled — planner
    emits a SkipRecord with a reason containing 'youtube_liked_videos source'."""
    diag = _make_diagnosis(session)
    dup_t = _make_track(session, "ytdup", suffix="ytdup")
    dup = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=dup_t,
        reason="appears 2 times in youtube_liked_videos snapshot (positions: 0, 5)",
    )
    session.commit()

    actions, skips = plan(
        [dup],
        {dup_t.id: "ytdup"},
        drift_min_confidence=0.95,
    )

    assert actions == []
    assert len(skips) == 1
    assert skips[0].item_id == dup.id
    assert skips[0].kind == "ytm_dedupe"
    assert "youtube_liked_videos source" in skips[0].reason


def test_plan_duplicate_in_source_count_mismatch_emits_skip(session: Session) -> None:
    """count=2 but 3 positions — the v3 regex hole. Parser returns None,
    planner emits a SkipRecord with reason containing 'unparseable'."""
    diag = _make_diagnosis(session)
    dup_t = _make_track(session, "mismatch", suffix="mismatch")
    dup = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=dup_t,
        reason="appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1, 5)",
    )
    session.commit()

    actions, skips = plan(
        [dup],
        {dup_t.id: "mismatch"},
        drift_min_confidence=0.95,
    )

    assert actions == []
    assert len(skips) == 1
    assert skips[0].kind == "ytm_dedupe"
    assert "unparseable" in skips[0].reason


def test_plan_duplicate_in_source_missing_video_id_emits_skip(session: Session) -> None:
    """Valid ytmusic N=2 reason, but the source track has no video_id in the
    lookup map — planner emits a SkipRecord with reason containing 'no video_id'."""
    diag = _make_diagnosis(session)
    dup_t = _make_track(session, "dup_novid", suffix="novid")
    dup = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=dup_t,
        reason="appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1)",
    )
    session.commit()

    # dup_t.id intentionally omitted from video_ids
    actions, skips = plan(
        [dup],
        {},
        drift_min_confidence=0.95,
    )

    assert actions == []
    assert len(skips) == 1
    assert skips[0].kind == "ytm_dedupe"
    assert "no video_id" in skips[0].reason


def test_plan_skips_failed_ytm_dedupe_item_in_same_diagnosis(session: Session) -> None:
    """A DiagnosisItem with status='applied' is silently dropped regardless of
    issue_type — pins the non-idempotent retry guard for ytm_dedupe."""
    diag = _make_diagnosis(session)
    dup_t = _make_track(session, "dup_done", suffix="done")
    dup = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=dup_t,
        status="applied",
        reason="appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1)",
    )
    session.commit()

    actions, skips = plan(
        [dup],
        {dup_t.id: "dup_done"},
        drift_min_confidence=0.95,
    )

    assert actions == []
    assert skips == []


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_counts_and_quota() -> None:
    actions = [
        PlannedAction(item_id=1, kind="yt_unlike", primary_video_id="a", secondary_video_id=None),
        PlannedAction(item_id=2, kind="yt_unlike", primary_video_id="b", secondary_video_id=None),
        PlannedAction(item_id=3, kind="ytm_like", primary_video_id="c", secondary_video_id=None),
        PlannedAction(item_id=4, kind="yt_relike", primary_video_id="d", secondary_video_id="e"),
    ]
    skips = [
        SkipRecord(item_id=5, kind="yt_relike", reason="confidence 0.5 < 0.95"),
    ]

    s = summarize(actions, skips)

    assert "yt_unlike: 2" in s
    assert "ytm_like: 1" in s
    assert "yt_relike: 1" in s
    assert "skipped: 1" in s
    # Quota: 2 unlikes (100) + 0 ytm + 1 relike worst-case (100) = 200.
    assert "200" in s


def test_summarize_includes_ytm_dedupe() -> None:
    """ytm_dedupe action and skip both appear in summarize output.
    ytm_dedupe has quota cost 0, so YouTube quota is unchanged."""
    actions = [
        PlannedAction(item_id=1, kind="ytm_dedupe", primary_video_id="d", secondary_video_id=None),
    ]
    skips = [
        SkipRecord(item_id=2, kind="ytm_dedupe", reason="count=3; only N=2 is auto-handled"),
    ]

    s = summarize(actions, skips)

    # Action line for ytm_dedupe
    assert "ytm_dedupe: 1" in s
    # Skip total and per-kind breakdown
    assert "skipped: 1" in s
    assert "ytm_dedupe: 1" in s
    # Quota: ytm_dedupe cost=0, so 0 units
    assert "0" in s
    # The existing non-ytm_dedupe action kinds show 0
    assert "yt_unlike: 0" in s
    assert "ytm_like: 0" in s
    assert "yt_relike: 0" in s


# ---------------------------------------------------------------------------
# execute — happy paths
# ---------------------------------------------------------------------------


def _attempts_for(session: Session, item_id: int) -> list[SyncAttempt]:
    return list(
        session.scalars(
            select(SyncAttempt)
            .where(SyncAttempt.diagnosis_item_id == item_id)
            .order_by(SyncAttempt.id)
        ).all()
    )


def test_execute_yt_unlike_success(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "ghost", suffix="g")
    item = _make_item(session, diag, issue_type=ISSUE_UNAVAILABLE_VIDEO, source_track=t)
    session.commit()
    original_reason = item.reason

    yt = FakeYouTube()
    ytm = FakeYTMusic()
    actions = [
        PlannedAction(
            item_id=item.id, kind="yt_unlike", primary_video_id="ghost", secondary_video_id=None
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    assert yt.calls == [("ghost", "none")]
    session.refresh(item)
    assert item.status == "applied"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [("yt_unlike", "applied")]


def test_execute_ytm_like_success(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "song", suffix="m")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        source_track=t,
    )
    session.commit()
    original_reason = item.reason

    yt = FakeYouTube()
    ytm = FakeYTMusic()
    actions = [
        PlannedAction(
            item_id=item.id, kind="ytm_like", primary_video_id="song", secondary_video_id=None
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    assert ytm.calls == ["song"]
    session.refresh(item)
    assert item.status == "applied"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [("ytm_like", "applied")]


def test_execute_yt_relike_both_succeed(session: Session) -> None:
    diag = _make_diagnosis(session)
    src = _make_track(session, "src", suffix="s")
    rel = _make_track(session, "rel", suffix="r")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_POINTER_DRIFT,
        confidence=0.99,
        source_track=src,
        related_track=rel,
    )
    session.commit()
    original_reason = item.reason

    yt = FakeYouTube()
    ytm = FakeYTMusic()
    actions = [
        PlannedAction(
            item_id=item.id,
            kind="yt_relike",
            primary_video_id="rel",
            secondary_video_id="src",
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    # Order matters: like first, then unlike.
    assert yt.calls == [("rel", "like"), ("src", "none")]
    session.refresh(item)
    assert item.status == "applied"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [
        ("yt_relike_like", "applied"),
        ("yt_relike_unlike", "applied"),
    ]


# ---------------------------------------------------------------------------
# execute — failure paths
# ---------------------------------------------------------------------------


def test_execute_yt_unlike_failure_keeps_status_open(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "ghost", suffix="g")
    item = _make_item(session, diag, issue_type=ISSUE_UNAVAILABLE_VIDEO, source_track=t)
    session.commit()
    original_reason = item.reason

    yt = FakeYouTube(raise_on={("ghost", "none")})
    ytm = FakeYTMusic()
    actions = [
        PlannedAction(
            item_id=item.id, kind="yt_unlike", primary_video_id="ghost", secondary_video_id=None
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=0, failed=1, skipped=0)
    session.refresh(item)
    assert item.status == "open"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert len(rows) == 1
    assert rows[0].kind == "yt_unlike"
    assert rows[0].status == "failed"
    assert "boom" in rows[0].reason


def test_execute_yt_relike_unlike_fails_after_like_success(session: Session) -> None:
    """Critical scenario: like succeeds, unlike fails → 2 SyncAttempt rows
    (like=applied, unlike=failed), item.status STAYS 'open' (not 'applied'),
    and a follow-up plan() picks the item up again (item is still actionable)."""
    diag = _make_diagnosis(session)
    src = _make_track(session, "src", suffix="s")
    rel = _make_track(session, "rel", suffix="r")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_POINTER_DRIFT,
        confidence=0.99,
        source_track=src,
        related_track=rel,
    )
    session.commit()
    original_reason = item.reason

    yt = FakeYouTube(raise_on={("src", "none")})
    ytm = FakeYTMusic()
    actions = [
        PlannedAction(
            item_id=item.id,
            kind="yt_relike",
            primary_video_id="rel",
            secondary_video_id="src",
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=0, failed=1, skipped=0)
    # Both halves were attempted.
    assert yt.calls == [("rel", "like"), ("src", "none")]
    session.refresh(item)
    # Stays open — only fully-applied actions flip the cache.
    assert item.status == "open"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [
        ("yt_relike_like", "applied"),
        ("yt_relike_unlike", "failed"),
    ]

    # Re-plan picks it up again (status is not 'applied' yet).
    actions2, skips2 = plan(
        [item],
        {src.id: "src", rel.id: "rel"},
        drift_min_confidence=0.95,
    )
    assert len(actions2) == 1
    assert actions2[0].item_id == item.id
    assert skips2 == []


def test_execute_yt_relike_like_failure_skips_unlike(session: Session) -> None:
    """When the like half fails, the unlike half MUST NOT run — otherwise the
    user's original like would vanish before any replacement was in place."""
    diag = _make_diagnosis(session)
    src = _make_track(session, "src", suffix="s")
    rel = _make_track(session, "rel", suffix="r")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_POINTER_DRIFT,
        confidence=0.99,
        source_track=src,
        related_track=rel,
    )
    session.commit()
    original_reason = item.reason

    yt = FakeYouTube(raise_on={("rel", "like")})
    ytm = FakeYTMusic()
    actions = [
        PlannedAction(
            item_id=item.id,
            kind="yt_relike",
            primary_video_id="rel",
            secondary_video_id="src",
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=0, failed=1, skipped=0)
    # Only the like was attempted — the unlike did NOT run.
    assert yt.calls == [("rel", "like")]
    session.refresh(item)
    assert item.status == "open"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [("yt_relike_like", "failed")]


def test_execute_ytm_like_failure_keeps_status_open(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "song", suffix="m")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        source_track=t,
    )
    session.commit()
    original_reason = item.reason

    yt = FakeYouTube()
    ytm = FakeYTMusic(raise_on={"song"})
    actions = [
        PlannedAction(
            item_id=item.id, kind="ytm_like", primary_video_id="song", secondary_video_id=None
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=0, failed=1, skipped=0)
    session.refresh(item)
    assert item.status == "open"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert len(rows) == 1
    assert rows[0].kind == "ytm_like"
    assert rows[0].status == "failed"


# ---------------------------------------------------------------------------
# execute — skips
# ---------------------------------------------------------------------------


def test_execute_skips_produce_sync_attempts(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, None, suffix="g")
    item = _make_item(session, diag, issue_type=ISSUE_UNAVAILABLE_VIDEO, source_track=t)
    session.commit()
    original_reason = item.reason

    yt = FakeYouTube()
    ytm = FakeYTMusic()
    skips = [
        SkipRecord(
            item_id=item.id, kind="yt_unlike", reason="no video_id available for source track"
        )
    ]
    res = execute(session, [], skips, ytm=ytm, yt=yt)

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    assert yt.calls == []
    assert ytm.calls == []
    session.refresh(item)
    # Skipped findings stay 'open' — next run re-evaluates them.
    assert item.status == "open"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert len(rows) == 1
    assert rows[0].kind == "yt_unlike"
    assert rows[0].status == "skipped"
    assert "no video_id" in rows[0].reason


# ---------------------------------------------------------------------------
# Per-action commit cadence
# ---------------------------------------------------------------------------


def test_execute_commits_per_action(session: Session) -> None:
    """After action 1 raises mid-action 2, action 1's SyncAttempt + status
    update must be queryable from a fresh session — proving each action
    committed independently rather than rolling back the whole batch."""
    # Fresh session bound to the same in-memory engine so we can observe
    # rows ``execute`` committed without going through ``session``.
    factory = make_session_factory(session.bind)

    # Build two findings sharing the same DB.
    diag = _make_diagnosis(session)
    t1 = _make_track(session, "vid_one", suffix="one")
    t2 = _make_track(session, "vid_two", suffix="two")
    item1 = _make_item(session, diag, issue_type=ISSUE_UNAVAILABLE_VIDEO, source_track=t1)
    item2 = _make_item(session, diag, issue_type=ISSUE_UNAVAILABLE_VIDEO, source_track=t2)
    session.commit()
    item1_id, item2_id = item1.id, item2.id

    class CrashingYouTube(FakeYouTube):
        """Succeeds on the first call, raises a non-write error on the second
        (mimics a transport crash mid-loop — execute() lets it propagate)."""

        def __init__(self) -> None:
            super().__init__()
            self._crashed = False

        def rate_video(self, video_id: str, rating: str) -> None:
            self.calls.append((video_id, rating))
            if self._crashed:
                return  # never reached
            if len(self.calls) == 2:
                self._crashed = True
                raise RuntimeError("transport crash")

    yt = CrashingYouTube()
    ytm = FakeYTMusic()
    actions = [
        PlannedAction(
            item_id=item1_id, kind="yt_unlike", primary_video_id="vid_one", secondary_video_id=None
        ),
        PlannedAction(
            item_id=item2_id, kind="yt_unlike", primary_video_id="vid_two", secondary_video_id=None
        ),
    ]
    with pytest.raises(RuntimeError, match="transport crash"):
        execute(session, actions, [], ytm=ytm, yt=yt)

    # Use a *fresh session* against the same engine — proves the data is on disk
    # (well, in the shared in-memory DB) regardless of the original session's
    # transaction state.
    fresh = factory()
    try:
        item1_fresh = fresh.get(DiagnosisItem, item1_id)
        item2_fresh = fresh.get(DiagnosisItem, item2_id)
        assert item1_fresh.status == "applied"  # action 1 committed
        assert item2_fresh.status == "open"  # action 2 crashed before commit
        attempts1 = _attempts_for(fresh, item1_id)
        attempts2 = _attempts_for(fresh, item2_id)
        assert [(r.kind, r.status) for r in attempts1] == [("yt_unlike", "applied")]
        assert attempts2 == []
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# _parse_duplicate_in_source_reason
# ---------------------------------------------------------------------------


def test_parse_dup_reason_ytmusic_n2() -> None:
    result = _parse_duplicate_in_source_reason(
        "appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1)"
    )
    assert result == _DuplicateReason(count=2, source="ytmusic_liked_songs", positions=(0, 1))


def test_parse_dup_reason_youtube_n2() -> None:
    result = _parse_duplicate_in_source_reason(
        "appears 2 times in youtube_liked_videos snapshot (positions: 3, 7)"
    )
    assert result == _DuplicateReason(count=2, source="youtube_liked_videos", positions=(3, 7))


def test_parse_dup_reason_n3() -> None:
    result = _parse_duplicate_in_source_reason(
        "appears 3 times in ytmusic_liked_songs snapshot (positions: 0, 1, 5)"
    )
    assert result == _DuplicateReason(count=3, source="ytmusic_liked_songs", positions=(0, 1, 5))


def test_parse_dup_reason_count_mismatch_returns_none() -> None:
    # count=2 but 3 positions — mismatch must return None
    result = _parse_duplicate_in_source_reason(
        "appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1, 5)"
    )
    assert result is None


def test_parse_dup_reason_unknown_source_text_returns_none() -> None:
    # surrounding text makes fullmatch fail
    result = _parse_duplicate_in_source_reason("manually edited: appears 2 times in elsewhere")
    assert result is None


def test_parse_dup_reason_random_text_returns_none() -> None:
    result = _parse_duplicate_in_source_reason("random text")
    assert result is None


# ---------------------------------------------------------------------------
# execute — ytm_dedupe terminal-on-attempt semantics
# ---------------------------------------------------------------------------


def test_execute_ytm_dedupe_success_marks_applied(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "vid", suffix="dup")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=t,
        reason="appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1)",
    )
    session.commit()
    original_reason = item.reason

    ytm = FakeYTMusic()
    yt = FakeYouTube()
    actions = [
        PlannedAction(
            item_id=item.id, kind="ytm_dedupe", primary_video_id="vid", secondary_video_id=None
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    assert ytm.unlike_calls == ["vid"]
    session.refresh(item)
    assert item.status == "applied"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert len(rows) == 1
    assert rows[0].kind == "ytm_dedupe"
    assert rows[0].status == "applied"


def test_execute_ytm_dedupe_failure_still_marks_applied(session: Session) -> None:
    """ytm_dedupe is non-idempotent; execute() flips status='applied' regardless to prevent auto-retry within same Diagnosis."""
    diag = _make_diagnosis(session)
    t = _make_track(session, "vid", suffix="dup")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=t,
        reason="appears 2 times in ytmusic_liked_songs snapshot (positions: 0, 1)",
    )
    session.commit()
    original_reason = item.reason

    ytm = FakeYTMusic(raise_on_unlike={"vid"})
    yt = FakeYouTube()
    actions = [
        PlannedAction(
            item_id=item.id, kind="ytm_dedupe", primary_video_id="vid", secondary_video_id=None
        )
    ]
    res = execute(session, actions, [], ytm=ytm, yt=yt)

    assert res == ExecResult(applied=0, failed=1, skipped=0)
    session.refresh(item)
    assert item.status == "applied"
    assert item.reason == original_reason
    rows = _attempts_for(session, item.id)
    assert len(rows) == 1
    assert rows[0].kind == "ytm_dedupe"
    assert rows[0].status == "failed"
    assert "boom" in rows[0].reason


def test_execute_ytm_dedupe_skip_emits_attempt(session: Session) -> None:
    diag = _make_diagnosis(session)
    t = _make_track(session, "vid", suffix="dup")
    item = _make_item(
        session,
        diag,
        issue_type=ISSUE_DUPLICATE_IN_SOURCE,
        source_track=t,
        reason="appears 3 times in ytmusic_liked_songs snapshot (positions: 0, 1, 5)",
    )
    session.commit()

    ytm = FakeYTMusic()
    yt = FakeYouTube()
    skips = [
        SkipRecord(
            item_id=item.id,
            kind="ytm_dedupe",
            reason="count=3; only N=2 is auto-handled in 0.5",
        )
    ]
    res = execute(session, [], skips, ytm=ytm, yt=yt)

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    session.refresh(item)
    assert item.status == "open"
    rows = _attempts_for(session, item.id)
    assert len(rows) == 1
    assert rows[0].kind == "ytm_dedupe"
    assert rows[0].status == "skipped"
    assert rows[0].reason == "count=3; only N=2 is auto-handled in 0.5"


def test_parse_dup_reason_roundtrip_with_builder() -> None:
    from likesurgeon.diagnosis import build_duplicate_in_source_items
    from likesurgeon.models import SnapshotItem
    from likesurgeon.snapshot import YTMUSIC_LIKED_SONGS

    items = [
        SnapshotItem(
            snapshot_id=1,
            track_id=10,
            position=0,
            video_id="vid_abc",
            title="Song",
            artists="[]",
            canonical_key="song",
        ),
        SnapshotItem(
            snapshot_id=1,
            track_id=11,
            position=1,
            video_id="vid_abc",
            title="Song",
            artists="[]",
            canonical_key="song",
        ),
    ]
    diagnosis_items = build_duplicate_in_source_items(
        diagnosis_id=1, snapshot_items=items, source=YTMUSIC_LIKED_SONGS
    )
    assert len(diagnosis_items) == 1
    reason = diagnosis_items[0].reason
    result = _parse_duplicate_in_source_reason(reason)
    assert result == _DuplicateReason(count=2, source="ytmusic_liked_songs", positions=(0, 1))
