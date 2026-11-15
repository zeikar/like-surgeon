"""Tests for the read-only ``sync`` pre-flight checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from likesurgeon.models import (
    Diagnosis,
    DiagnosisItem,
    Snapshot,
    SnapshotItem,
    SyncAttempt,
    Track,
)
from likesurgeon.snapshot import create_snapshot
from likesurgeon.sync_dispatch import QUOTA_REJECTED
from likesurgeon.sync_preflight import (
    diagnosis_staleness,
    stranded_unliked_video_ids,
    sync_attempts_since,
    sync_attempts_since_older_scan,
    unsettled_playlist_actions,
)


def _yt(video_id: str) -> dict:
    return {
        "snippet": {"title": "T", "channelTitle": "A", "resourceId": {"videoId": video_id}},
        "contentDetails": {"videoId": video_id},
    }


def _ytm_track(session: Session, video_id: str, *, source: str = "ytmusic_liked_songs") -> Track:
    t = Track(
        source=source,
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


def test_stranded_covers_canonical_histories(session: Session) -> None:
    """``canonical_unlike`` settles like the other unlikes; the canonical verify
    and rollback rows don't (spec canonicalize §6.6)."""
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    stranded = {
        "unlike_last": [("canonical_unlike", "applied")],
        "unlike_verified_only": [
            ("canonical_unlike", "applied"),
            ("canonical_unlike_verify", "applied"),
        ],
        "restore_failed": [
            ("canonical_unlike", "applied"),
            ("lm_check", "failed"),
            ("restore_like", "failed"),
            ("canonical_rollback", "applied"),
            ("canonical_rollback_verify", "applied"),
        ],
    }
    fine = {
        "quota_rejected": [("canonical_unlike", "failed", f"{QUOTA_REJECTED}: quotaExceeded")],
        "settled": [
            ("canonical_unlike", "applied"),
            ("canonical_unlike_verify", "applied"),
            ("lm_check", "applied"),
        ],
        "restored_rollback_failed": [
            ("canonical_unlike", "applied"),
            ("lm_check", "failed"),
            ("restore_like", "applied"),
            ("restore_verify", "applied"),
            ("canonical_rollback", "failed"),
            ("canonical_rollback_verify", "failed"),
        ],
    }
    for vid, rows in {**stranded, **fine}.items():
        track = _ytm_track(session, vid)
        for kind, status, *reason in rows:
            _relike_attempt(session, track, status, kind=kind, at=t, reason="".join(reason))

    assert stranded_unliked_video_ids(session) == set(stranded)


def test_stranded_keeps_the_newest_settling_row_per_video_across_sources(
    session: Session,
) -> None:
    """An alignment A is a YouTube track, a canonical A a YT Music track; both
    rate the same video, so the newest row about that video decides."""
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    unconfirmed_restore = [
        ("repoint_unlike", "applied"),
        ("lm_check", "failed"),
        ("restore_like", "applied"),
        ("restore_verify", "failed"),
    ]
    canonical_settled = [("canonical_unlike", "applied"), ("lm_check", "applied")]
    canonical_unsettled = [("canonical_unlike", "applied")]
    confirmed_restore = [
        ("unlike_shadow_unlike", "applied"),
        ("lm_check", "failed"),
        ("restore_like", "applied"),
        ("restore_verify", "applied"),
    ]
    histories = {
        # (source of the older history, its rows, source of the newer, its rows)
        "healed": (
            "youtube_liked_videos",
            unconfirmed_restore,
            "ytmusic_liked_songs",
            canonical_settled,
        ),
        "restored_later": (
            "ytmusic_liked_songs",
            canonical_unsettled,
            "youtube_liked_videos",
            confirmed_restore,
        ),
        "failing_newest": (
            "ytmusic_liked_songs",
            canonical_settled,
            "youtube_liked_videos",
            unconfirmed_restore,
        ),
    }
    for vid, (old_src, old_rows, new_src, new_rows) in histories.items():
        for source, rows in ((old_src, old_rows), (new_src, new_rows)):
            track = _ytm_track(session, vid, source=source)
            for kind, status in rows:
                _relike_attempt(session, track, status, kind=kind, at=t)

    assert stranded_unliked_video_ids(session) == {"failing_newest"}


def test_a_later_canonical_action_settles_a_relike_left_unverified(session: Session) -> None:
    """A ``relike`` killed before its verify leaves A possibly unliked. A later
    canonical action on the same video read it liked at its precheck, unliked it
    on purpose and passed its LM check, so that newer row decides. The other
    way round, a ``relike`` unlike left last strands the video."""
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    relike_unverified = [("relike_unlike", "applied"), ("relike_like", "applied")]
    canonical_settled = [("canonical_unlike", "applied"), ("lm_check", "applied")]
    histories = {
        "canonical_later": [
            ("youtube_liked_videos", relike_unverified),
            ("ytmusic_liked_songs", canonical_settled),
        ],
        "relike_later": [
            ("ytmusic_liked_songs", canonical_settled),
            ("youtube_liked_videos", relike_unverified),
        ],
    }
    for vid, parts in histories.items():
        for source, rows in parts:
            track = _ytm_track(session, vid, source=source)
            for kind, status in rows:
                _relike_attempt(session, track, status, kind=kind, at=t)

    assert stranded_unliked_video_ids(session) == {"relike_later"}


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


def test_canonical_writes_count_but_its_reads_do_not(session: Session) -> None:
    t = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    ll = create_snapshot(session, "youtube_liked_videos", [])
    lm = create_snapshot(session, "ytmusic_liked_songs", [])
    lm.created_at = ll.created_at = t
    diag = Diagnosis(youtube_snapshot_id=ll.id, ytmusic_snapshot_id=lm.id)
    session.add(diag)
    session.flush()

    for kind in (
        "canonical_precheck_lm",
        "canonical_precheck_a",
        "canonical_precheck_b",
        "canonical_verify",
        "canonical_unlike_verify",
        "canonical_rollback_verify",
    ):
        _write_at(session, t + timedelta(hours=1), status="failed", kind=kind)
    assert sync_attempts_since_older_scan(session, diag) == 0
    for kind in ("canonical_like", "canonical_unlike", "canonical_rollback"):
        _write_at(session, t + timedelta(hours=1), status="failed", kind=kind)
    assert sync_attempts_since_older_scan(session, diag) == 3


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


# --- unsettled playlist actions -------------------------------------------------

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _pl_snapshot(
    session: Session, source: str, playlist_id: str, at: datetime, *, title: str = "Mix"
) -> Snapshot:
    snap = Snapshot(
        source=source, raw_count=0, playlist_id=playlist_id, playlist_title=title, created_at=at
    )
    session.add(snap)
    session.flush()
    return snap


def _pl_item(session: Session, *, playlist_id: str = "PL1", position: int = 2) -> DiagnosisItem:
    """A playlist finding: A on the YouTube side at ``position`` (0-based), B on YT Music."""
    track = _ytm_track(session, f"a-{playlist_id}-{position}", source="youtube_playlist")
    rows = []
    for source, vid in (("youtube_playlist", "A"), ("ytmusic_playlist", "B")):
        snap = _pl_snapshot(session, source, playlist_id, T0 - timedelta(days=1))
        row = SnapshotItem(
            snapshot_id=snap.id,
            track_id=track.id,
            position=position,
            video_id=vid,
            title="t",
            artists="[]",
            canonical_key="k",
        )
        session.add(row)
        rows.append(row)
    session.flush()
    diag = Diagnosis(kind="playlists")
    session.add(diag)
    session.flush()
    item = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type="playlist_relinked",
        confidence=1.0,
        reason="r",
        source_item_id=rows[0].id,
        related_item_id=rows[1].id,
    )
    session.add(item)
    session.flush()
    return item


def _rows(session: Session, item: DiagnosisItem, *rows: tuple, start: datetime = T0) -> None:
    """Add attempts in order: ``(kind, status)`` or ``(kind, status, reason)``."""
    for n, (kind, status, *reason) in enumerate(rows):
        session.add(
            SyncAttempt(
                diagnosis_item_id=item.id,
                kind=kind,
                status=status,
                reason=reason[0] if reason else "",
                created_at=start + timedelta(minutes=n),
            )
        )
    session.flush()


CLEAN = [
    ("playlist_precheck", "applied"),
    ("playlist_insert", "applied"),
    ("playlist_insert_verify", "applied"),
    ("playlist_ytm_verify", "applied"),
    ("playlist_delete", "applied"),
    ("playlist_delete_verify", "applied"),
    ("playlist_check", "applied"),
]


@pytest.mark.parametrize(
    "rows",
    [
        CLEAN[:2],  # died right after the insert call
        CLEAN[:3],
        CLEAN[:4],
        CLEAN[:5],  # died after the delete call
        CLEAN[:6],
        [*CLEAN[:5], ("playlist_interrupted", "failed")],
        [*CLEAN[:5], ("playlist_delete_verify", "failed")],  # unconfirmed delete
        [*CLEAN[:6], ("playlist_restore", "failed"), ("playlist_restore_verify", "failed")],
        [*CLEAN[:3], ("playlist_rollback", "failed")],
        [*CLEAN[:3], ("playlist_rollback", "failed", f"{QUOTA_REJECTED}: x")],
        [("playlist_precheck", "applied"), ("playlist_insert", "failed", "boom")],
        # B removed but never confirmed gone
        [*CLEAN[:3], ("playlist_rollback", "applied"), ("playlist_rollback_verify", "failed")],
    ],
)
def test_unsettled_playlist_action_is_listed(session: Session, rows: list) -> None:
    item = _pl_item(session)
    _rows(session, item, *rows)
    assert unsettled_playlist_actions(session) == [("Mix", 3, "A", "B")]


@pytest.mark.parametrize(
    "rows",
    [
        CLEAN,
        [  # restored: A put back, B removed and confirmed
            *CLEAN[:6],
            ("playlist_restore", "applied"),
            ("playlist_restore_verify", "applied"),
            ("playlist_rollback", "applied"),
            ("playlist_rollback_verify", "applied"),
        ],
        [*CLEAN[:3], ("playlist_rollback", "applied"), ("playlist_rollback_verify", "applied")],
        [("playlist_precheck", "applied"), ("playlist_insert", "failed", f"{QUOTA_REJECTED}: x")],
        [("playlist_precheck", "failed")],
        # an errored delete that landed passes both checks: still settled
        [*CLEAN[:4], ("playlist_delete", "failed", "boom"), *CLEAN[5:]],
    ],
)
def test_settled_playlist_action_is_not_listed(session: Session, rows: list) -> None:
    item = _pl_item(session)
    _rows(session, item, *rows)
    assert unsettled_playlist_actions(session) == []


def test_playlist_precheck_of_a_later_run_does_not_settle(session: Session) -> None:
    item = _pl_item(session)
    _rows(
        session, item, *CLEAN[:5], ("playlist_precheck", "applied"), ("playlist_precheck", "failed")
    )
    assert unsettled_playlist_actions(session) == [("Mix", 3, "A", "B")]


def test_action_settled_then_unsettled_again_is_listed(session: Session) -> None:
    item = _pl_item(session)
    _rows(session, item, *CLEAN, ("playlist_precheck", "applied"), ("playlist_insert", "applied"))
    assert unsettled_playlist_actions(session) == [("Mix", 3, "A", "B")]


def test_unsettled_action_retired_by_scans_of_both_sides(session: Session) -> None:
    item = _pl_item(session)
    _rows(session, item, *CLEAN[:5])  # last write at T0 + 4 min
    after = T0 + timedelta(hours=1)
    _pl_snapshot(session, "ytmusic_playlist", "PL1", after)
    assert unsettled_playlist_actions(session) != []  # YT Music side alone
    _pl_snapshot(session, "youtube_playlist", "PL1", after)
    assert unsettled_playlist_actions(session) == []


def test_unsettled_action_stays_after_scan_of_other_playlists(session: Session) -> None:
    item = _pl_item(session)
    _rows(session, item, *CLEAN[:5])
    after = T0 + timedelta(hours=1)
    for source in ("ytmusic_playlist", "youtube_playlist"):
        _pl_snapshot(session, source, "PL-other", after)
    assert unsettled_playlist_actions(session) == [("Mix", 3, "A", "B")]


def test_unsettled_action_stays_when_scans_predate_its_last_write(session: Session) -> None:
    item = _pl_item(session)
    _rows(session, item, *CLEAN[:5])
    before = T0 + timedelta(minutes=2)
    for source in ("ytmusic_playlist", "youtube_playlist"):
        _pl_snapshot(session, source, "PL1", before)
    assert unsettled_playlist_actions(session) == [("Mix", 3, "A", "B")]


def test_playlist_rows_do_not_count_as_likes_state(session: Session) -> None:
    """``playlist_*`` rows, interrupted and restore included, never strand a
    liked video or count toward the sync refusal."""
    item = _pl_item(session)
    item.source_track_id = _ytm_track(session, "vidA").id
    _rows(
        session,
        item,
        *CLEAN[:5],
        ("playlist_interrupted", "failed"),
        ("playlist_restore", "failed"),
        ("playlist_rollback", "failed"),
    )
    assert stranded_unliked_video_ids(session) == frozenset()
    assert sync_attempts_since(session, T0 - timedelta(days=1)) == 0
