"""Tests for the read-only ``sync`` pre-flight checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from likesurgeon.diagnosis import ISSUE_POINTER_DRIFT, ISSUE_YTMUSIC_ONLY
from likesurgeon.models import Diagnosis, DiagnosisItem, SyncAttempt, Track
from likesurgeon.snapshot import create_snapshot
from likesurgeon.sync_preflight import (
    diagnosis_staleness,
    stranded_unliked_video_ids,
    youtube_liked_video_ids,
)


def _yt(video_id: str) -> dict:
    return {
        "snippet": {"title": "T", "channelTitle": "A", "resourceId": {"videoId": video_id}},
        "contentDetails": {"videoId": video_id},
    }


def _ytm_track(session: Session, video_id: str) -> Track:
    t = Track(
        source="ytmusic_liked_songs",
        video_id=video_id,
        title="t",
        artists="[]",
        canonical_key=f"k-{video_id}",
        dedupe_key=video_id,
    )
    session.add(t)
    session.flush()
    return t


def test_liked_ids_include_the_diagnosis_youtube_snapshot(session: Session) -> None:
    snap = create_snapshot(session, "youtube_liked_videos", [_yt("a"), _yt("b")])
    diag = Diagnosis(youtube_snapshot_id=snap.id)
    session.add(diag)
    session.flush()

    assert youtube_liked_video_ids(session, diag) == {"a", "b"}


def test_liked_ids_include_videos_a_past_sync_liked(session: Session) -> None:
    """A like made by sync after the YouTube scan isn't in any snapshot yet —
    the attempt history must still count it (the self-revert loop's start)."""
    snap = create_snapshot(session, "youtube_liked_videos", [])
    diag = Diagnosis(youtube_snapshot_id=snap.id)
    session.add(diag)
    session.flush()
    b = _ytm_track(session, "B")
    b2 = _ytm_track(session, "B2")
    failed = _ytm_track(session, "F")
    src = _ytm_track(session, "A")
    yt_like = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type=ISSUE_YTMUSIC_ONLY,
        confidence=0.5,
        reason="r",
        source_track_id=b.id,
    )
    relike = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type=ISSUE_POINTER_DRIFT,
        confidence=0.95,
        reason="r",
        source_track_id=src.id,
        related_track_id=b2.id,
    )
    not_landed = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type=ISSUE_YTMUSIC_ONLY,
        confidence=0.5,
        reason="r",
        source_track_id=failed.id,
    )
    session.add_all([yt_like, relike, not_landed])
    session.flush()
    session.add_all(
        [
            SyncAttempt(
                diagnosis_item_id=yt_like.id, kind="yt_like_yt_rate", status="applied", reason="ok"
            ),
            SyncAttempt(
                diagnosis_item_id=relike.id, kind="yt_relike_like", status="applied", reason="ok"
            ),
            SyncAttempt(
                diagnosis_item_id=not_landed.id,
                kind="yt_like_yt_rate",
                status="failed",
                reason="boom",
            ),
        ]
    )
    session.flush()

    # The relike's *unlike* target (A) is not a liked video.
    assert youtube_liked_video_ids(session, diag) == {"B", "B2"}


def test_staleness_flags_newer_scan_than_diagnosis_used(session: Session) -> None:
    old = create_snapshot(session, "ytmusic_liked_songs", [])
    newer = create_snapshot(session, "ytmusic_liked_songs", [])
    diag = Diagnosis(ytmusic_snapshot_id=old.id)
    session.add(diag)
    session.flush()

    warnings = diagnosis_staleness(session, diag, now=datetime.now(UTC))
    assert len(warnings) == 1
    assert f"#{newer.id}" in warnings[0]
    assert "compare-likes" in warnings[0]


def test_staleness_flags_old_scan_and_handles_naive_timestamps(session: Session) -> None:
    """The age that matters is the scan's: a hand-made YouTube like after an
    old scan is invisible to the dedupe guard even if compare-likes is fresh."""
    snap = create_snapshot(session, "youtube_liked_videos", [])
    # SQLite returns naive datetimes; treat them as UTC.
    snap.created_at = datetime(2026, 1, 1, 12, 0)
    diag = Diagnosis(youtube_snapshot_id=snap.id)
    session.add(diag)
    session.flush()

    assert diagnosis_staleness(session, diag, now=datetime(2026, 1, 1, 12, 30, tzinfo=UTC)) == []
    warnings = diagnosis_staleness(session, diag, now=datetime(2026, 1, 1, 15, 0, tzinfo=UTC))
    assert len(warnings) == 1
    assert "youtube_liked_videos scan" in warnings[0]
    assert "3.0h old" in warnings[0]


def test_staleness_is_quiet_for_a_fresh_diagnosis_on_latest_scans(session: Session) -> None:
    ytm = create_snapshot(session, "ytmusic_liked_songs", [])
    yt = create_snapshot(session, "youtube_liked_videos", [])
    diag = Diagnosis(ytmusic_snapshot_id=ytm.id, youtube_snapshot_id=yt.id)
    session.add(diag)
    session.flush()

    now = datetime.now(UTC) + timedelta(minutes=5)
    assert diagnosis_staleness(session, diag, now=now) == []


def test_liked_ids_unknown_without_youtube_snapshot(session: Session) -> None:
    diag = Diagnosis(youtube_snapshot_id=None)
    session.add(diag)
    session.flush()
    assert youtube_liked_video_ids(session, diag) is None


def _relike_attempt(session: Session, track: Track, status: str) -> None:
    diag = Diagnosis()
    session.add(diag)
    session.flush()
    item = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type="possibly_missing_from_ytmusic",
        confidence=0.9,
        reason="r",
        source_track_id=track.id,
    )
    session.add(item)
    session.flush()
    session.add(
        SyncAttempt(diagnosis_item_id=item.id, kind="ytm_like_yt_relike", status=status, reason="")
    )
    session.flush()


def test_stranded_is_the_last_relike_failing_across_diagnoses(session: Session) -> None:
    healed = _ytm_track(session, "healed")
    stuck = _ytm_track(session, "stuck")
    fine = _ytm_track(session, "fine")
    _relike_attempt(session, healed, "failed")
    _relike_attempt(session, healed, "applied")  # a later run re-liked it
    _relike_attempt(session, stuck, "applied")
    _relike_attempt(session, stuck, "failed")  # a later run stranded it
    _relike_attempt(session, fine, "applied")

    assert stranded_unliked_video_ids(session) == {"stuck"}
