"""Tests for the read-only ``sync`` pre-flight checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from likesurgeon.models import Diagnosis, DiagnosisItem, SyncAttempt, Track
from likesurgeon.snapshot import create_snapshot
from likesurgeon.sync_dispatch import QUOTA_REJECTED
from likesurgeon.sync_preflight import (
    diagnosis_staleness,
    stranded_unliked_video_ids,
    sync_attempts_since,
    sync_attempts_since_older_scan,
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
    """The age that matters is the scan's: a hand-made like after an old scan
    is invisible to the diagnosis even if compare-likes is fresh."""
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


def _relike_attempt(
    session: Session,
    track: Track,
    status: str,
    *,
    kind: str = "ytm_like_yt_relike",
    at: datetime | None = None,
    reason: str = "",
) -> None:
    """One attempt in a diagnosis of its own, so the history spans diagnoses."""
    diag = Diagnosis()
    session.add(diag)
    session.flush()
    item = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type="relinked",
        confidence=1.0,
        reason="r",
        source_track_id=track.id,
    )
    session.add(item)
    session.flush()
    attempt = SyncAttempt(diagnosis_item_id=item.id, kind=kind, status=status, reason=reason)
    if at is not None:
        attempt.created_at = at
    session.add(attempt)
    session.flush()


def test_stranded_is_the_last_relike_failing_across_diagnoses(session: Session) -> None:
    """Pre-alignment rows: the ``ytm_like`` re-like."""
    healed = _ytm_track(session, "healed")
    stuck = _ytm_track(session, "stuck")
    fine = _ytm_track(session, "fine")
    _relike_attempt(session, healed, "failed")
    _relike_attempt(session, healed, "applied")  # a later run re-liked it
    _relike_attempt(session, stuck, "applied")
    _relike_attempt(session, stuck, "failed")  # a later run stranded it
    _relike_attempt(session, fine, "applied")

    assert stranded_unliked_video_ids(session) == {"stuck"}


def test_stranded_includes_restores_that_failed_or_went_unconfirmed(session: Session) -> None:
    def restore(track: Track, *steps: tuple[str, str]) -> None:
        for kind, status in steps:
            _relike_attempt(session, track, status, kind=kind)

    rejected = _ytm_track(session, "rejected")
    unconfirmed = _ytm_track(session, "unconfirmed")
    restored = _ytm_track(session, "restored")
    healed = _ytm_track(session, "healed")
    restore(rejected, ("restore_like", "failed"))
    restore(unconfirmed, ("restore_like", "applied"), ("restore_verify", "failed"))
    restore(restored, ("restore_like", "applied"), ("restore_verify", "applied"))
    restore(
        healed,
        ("restore_like", "failed"),
        ("restore_like", "applied"),  # a later restore of the same A landed
        ("restore_verify", "applied"),
    )

    assert stranded_unliked_video_ids(session) == {"rejected", "unconfirmed"}


def test_stranded_covers_unlikes_that_never_settled(session: Session) -> None:
    """A hard kill after the unlike's own commit leaves that row last; Ctrl-C
    leaves ``interrupted``; a quota-rejected unlike sent nothing. A later
    YouTube scan with the video clears it like any other."""
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def history(vid: str, *rows: tuple[str, str] | tuple[str, str, str]) -> None:
        track = _ytm_track(session, vid)
        for kind, status, *reason in rows:
            _relike_attempt(session, track, status, kind=kind, at=t, reason="".join(reason))

    history("killed", ("repoint_unlike", "applied"))
    history("killed_after_error", ("unlike_shadow_unlike", "failed", "timed out"))
    history("interrupted", ("repoint_unlike", "applied"), ("interrupted", "failed"))
    history("quota_rejected", ("repoint_unlike", "failed", f"{QUOTA_REJECTED}: quotaExceeded"))
    history("settled", ("repoint_unlike", "applied"), ("lm_check", "applied"))
    history(
        "restored",
        ("unlike_shadow_unlike", "applied"),
        ("lm_check", "failed"),
        ("restore_like", "applied"),
        ("restore_verify", "applied"),
    )
    history("check_without_restore", ("repoint_unlike", "applied"), ("lm_check", "failed"))
    history("interrupted_then_rescanned", ("interrupted", "failed"))
    rescan = create_snapshot(session, "youtube_liked_videos", [_yt("interrupted_then_rescanned")])
    rescan.created_at = t + timedelta(hours=1)
    session.flush()

    assert stranded_unliked_video_ids(session) == {
        "killed",
        "killed_after_error",
        "interrupted",
        "check_without_restore",
    }


def test_stranded_covers_relike_histories(session: Session) -> None:
    """``relike_like`` and ``relike_lm_check`` never settle A: a 2xx like can land
    nothing, and A is confirmed liked before the LM check. Only ``relike_verify``
    does."""
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    stranded = {
        "unlike_last": [("relike_unlike", "applied")],
        "like_killed_before_verify": [("relike_unlike", "applied"), ("relike_like", "applied")],
        "like_failed": [("relike_unlike", "applied"), ("relike_like", "failed")],
        "verify_failed": [
            ("relike_unlike", "applied"),
            ("relike_like", "applied"),
            ("relike_verify", "failed"),
        ],
        "interrupted": [("relike_unlike", "applied"), ("interrupted", "failed")],
    }
    fine = {
        "quota_rejected": [("relike_unlike", "failed", f"{QUOTA_REJECTED}: quotaExceeded")],
        "verified": [
            ("relike_unlike", "applied"),
            ("relike_like", "applied"),
            ("relike_verify", "applied"),
        ],
        "lm_check_failed_after_verify": [
            ("relike_unlike", "applied"),
            ("relike_verify", "applied"),
            ("relike_lm_check", "failed"),
        ],
    }
    for vid, rows in {**stranded, **fine}.items():
        track = _ytm_track(session, vid)
        for kind, status, *reason in rows:
            _relike_attempt(session, track, status, kind=kind, at=t, reason="".join(reason))

    assert stranded_unliked_video_ids(session) == set(stranded)


def test_stranded_clears_once_a_later_youtube_scan_shows_the_video_liked(
    session: Session,
) -> None:
    """The user re-liked it by hand: a YouTube scan after the failure has it."""
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    for vid in ("reliked", "scanned_before", "only_in_ytmusic", "never_scanned"):
        _relike_attempt(session, _ytm_track(session, vid), "failed", kind="restore_like", at=t)

    before = create_snapshot(session, "youtube_liked_videos", [_yt("scanned_before")])
    before.created_at = t - timedelta(hours=1)
    ytm = create_snapshot(
        session, "ytmusic_liked_songs", [{"videoId": "only_in_ytmusic", "title": "T"}]
    )
    ytm.created_at = t + timedelta(hours=1)
    after = create_snapshot(session, "youtube_liked_videos", [_yt("reliked")])
    after.created_at = (t + timedelta(hours=1)).replace(tzinfo=None)  # as SQLite returns it
    session.flush()

    assert stranded_unliked_video_ids(session) == {
        "scanned_before",
        "only_in_ytmusic",
        "never_scanned",
    }


def test_sync_attempts_since_counts_writes_strictly_after(session: Session) -> None:
    diag = Diagnosis()
    session.add(diag)
    session.flush()
    item = DiagnosisItem(diagnosis_id=diag.id, issue_type="relinked", confidence=1.0, reason="r")
    session.add(item)
    session.flush()
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    for minutes, status in [
        (-1, "applied"),  # before
        (0, "applied"),  # boundary: not after
        (1, "applied"),
        (2, "failed"),  # a failed call may still have landed
        (3, "skipped"),  # plan-time skip / verify miss: no write
    ]:
        session.add(
            SyncAttempt(
                diagnosis_item_id=item.id,
                kind="yt_unlike",
                status=status,
                reason="r",
                created_at=t + timedelta(minutes=minutes),
            )
        )
    session.flush()

    assert sync_attempts_since(session, t) == 2
    # SQLite hands timestamps back naive: treated as UTC, like diagnosis_staleness.
    assert sync_attempts_since(session, t.replace(tzinfo=None)) == 2
    # An aware non-UTC time is the same instant.
    assert sync_attempts_since(session, t.astimezone(timezone(timedelta(hours=9)))) == 2
    assert sync_attempts_since(session, t + timedelta(minutes=2)) == 0


def _write_at(
    session: Session, when: datetime, status: str = "applied", kind: str = "repoint_like"
) -> None:
    diag = Diagnosis()
    session.add(diag)
    session.flush()
    item = DiagnosisItem(diagnosis_id=diag.id, issue_type="relinked", confidence=1.0, reason="r")
    session.add(item)
    session.flush()
    session.add(
        SyncAttempt(
            diagnosis_item_id=item.id,
            kind=kind,
            status=status,
            reason="",
            created_at=when,
        )
    )
    session.flush()


def test_sync_attempts_since_older_scan_counts_from_the_older_of_the_two_scans(
    session: Session,
) -> None:
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    ll = create_snapshot(session, "youtube_liked_videos", [])
    lm = create_snapshot(session, "ytmusic_liked_songs", [])
    # LM is the older scan; LL's time comes back naive, as SQLite returns it.
    lm.created_at = t
    ll.created_at = (t + timedelta(hours=2)).replace(tzinfo=None)
    diag = Diagnosis(youtube_snapshot_id=ll.id, ytmusic_snapshot_id=lm.id)
    session.add(diag)
    session.flush()

    _write_at(session, t - timedelta(hours=1))  # before both scans
    assert sync_attempts_since_older_scan(session, diag) == 0
    _write_at(session, t + timedelta(hours=1))  # between the scans
    _write_at(session, t + timedelta(hours=3))  # after both
    _write_at(session, t + timedelta(hours=3), status="skipped")  # no write
    # A failed read-only check isn't a write; a failed write may have landed.
    _write_at(session, t + timedelta(hours=3), status="failed", kind="repoint_precheck_a")
    _write_at(session, t + timedelta(hours=3), status="failed", kind="repoint_verify")
    _write_at(session, t + timedelta(hours=3), status="failed", kind="lm_check")
    assert sync_attempts_since_older_scan(session, diag) == 2
    _write_at(session, t + timedelta(hours=3), status="failed", kind="repoint_rollback")
    assert sync_attempts_since_older_scan(session, diag) == 3


def test_relike_writes_count_but_its_reads_and_notes_do_not(session: Session) -> None:
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    ll = create_snapshot(session, "youtube_liked_videos", [])
    lm = create_snapshot(session, "ytmusic_liked_songs", [])
    lm.created_at = ll.created_at = t
    diag = Diagnosis(youtube_snapshot_id=ll.id, ytmusic_snapshot_id=lm.id)
    session.add(diag)
    session.flush()

    for kind in (
        "relike_precheck",
        "relike_unlike_verify",
        "relike_verify",
        "relike_lm_check",
    ):
        _write_at(session, t + timedelta(hours=1), status="failed", kind=kind)
    _write_at(session, t + timedelta(hours=1), status="skipped", kind="relike")
    assert sync_attempts_since_older_scan(session, diag) == 0
    for kind in ("relike_unlike", "relike_like"):
        _write_at(session, t + timedelta(hours=1), status="failed", kind=kind)
    assert sync_attempts_since_older_scan(session, diag) == 2


@pytest.mark.parametrize(
    "kind",
    [
        "yt_unlike",
        "yt_relike_like",
        "yt_relike_unlike",
        "ytm_like_yt_unlike",
        "ytm_like_yt_relike",
        "yt_like_yt_rate",
        "ytm_dedupe",
    ],
)
def test_a_legacy_write_between_the_scans_is_counted(session: Session, kind: str) -> None:
    """Rows written before the alignment redesign stay in upgraded databases."""
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    ll = create_snapshot(session, "youtube_liked_videos", [])
    lm = create_snapshot(session, "ytmusic_liked_songs", [])
    lm.created_at = t
    ll.created_at = t + timedelta(hours=2)
    diag = Diagnosis(youtube_snapshot_id=ll.id, ytmusic_snapshot_id=lm.id)
    session.add(diag)
    session.flush()

    _write_at(session, t + timedelta(hours=1), kind=kind)
    _write_at(session, t + timedelta(hours=1), kind="yt_like_verify", status="failed")  # a read

    assert sync_attempts_since_older_scan(session, diag) == 1


def test_sync_attempts_since_older_scan_without_scans_uses_the_diagnosis_time(
    session: Session,
) -> None:
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    diag = Diagnosis(created_at=t)
    session.add(diag)
    session.flush()
    _write_at(session, t - timedelta(minutes=1))
    assert sync_attempts_since_older_scan(session, diag) == 0
    _write_at(session, t + timedelta(minutes=1))
    assert sync_attempts_since_older_scan(session, diag) == 1
