"""Read-only DB checks the ``sync`` command runs before planning/writing.

Kept apart from ``sync.py`` so the planner stays pure: these query the DB and
hand the CLI plain values (video ids, counts) or warning strings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Diagnosis, DiagnosisItem, Snapshot, SnapshotItem, SyncAttempt, Track
from .snapshot import (
    YOUTUBE_LIKED_VIDEOS,
    YOUTUBE_PLAYLIST,
    YTMUSIC_LIKED_SONGS,
    YTMUSIC_PLAYLIST,
    latest_snapshot,
)
from .sync_dispatch import QUOTA_REJECTED

STALE_AFTER = timedelta(hours=1)


def _as_utc(dt: datetime) -> datetime:
    # SQLite hands DateTime(timezone=True) back naive; values are stored as UTC.
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# Once A's unlike call has started, the newest of these rows per video says
# whether A may be liked nowhere. Pre-alignment rows: the ``ytm_like`` re-like.
_UNLIKE_KINDS = ("repoint_unlike", "unlike_shadow_unlike", "relike_unlike", "canonical_unlike")
# ``relike_like`` is left out: only its verify proves the like landed, so a kill
# before that verify keeps ``relike_unlike`` the newest settling row.
# ``relike_lm_check`` is left out too: A is confirmed liked before it.
# ``canonical_verify`` and the B-rollback rows are about B; like
# ``repoint_unlike_verify``, ``canonical_unlike_verify`` is always followed by
# the LM check or a restore, which settle. Keying on the newest row per video is
# safe because every action prechecks ``getRating(A) = like`` before it writes a
# settling row, so a newer settling row means A was confirmed liked after the
# older one; the dispatcher must keep that order.
_SETTLING_KINDS = (
    *_UNLIKE_KINDS,
    "relike_verify",
    "interrupted",
    "lm_check",
    "restore_like",
    "restore_verify",
    "ytm_like_yt_relike",
)


# Rows of a ``videos.rate`` call or an interrupted action: each may have
# changed a like, even when it failed. Prechecks, verifies and LM checks only read.
_WRITE_KINDS = (
    "repoint_like",
    "repoint_unlike",
    "unlike_shadow_unlike",
    "restore_like",
    "repoint_rollback",
    "relike_unlike",
    "relike_like",
    "canonical_like",
    "canonical_unlike",
    "canonical_rollback",
    "interrupted",
    # Writes recorded by versions before the LL -> LM alignment; their rows stay
    # in existing databases. (``ytm_like`` / ``yt_relike`` / ``yt_like`` alone were
    # plan-time skips, which the 'skipped' status already excludes.)
    "yt_unlike",
    "yt_relike_like",
    "yt_relike_unlike",
    "ytm_like",
    "ytm_like_yt_unlike",
    "ytm_like_yt_relike",
    "yt_like_yt_rate",
    "ytm_dedupe",
)


def _may_be_unliked(kind: str, status: str, reason: str) -> bool:
    if kind in _UNLIKE_KINDS:
        # Nothing recorded after the unlike: the process died before its check.
        # A quota-rejected unlike sent nothing.
        return not reason.startswith(QUOTA_REJECTED)
    # ``interrupted`` rows are always 'failed'. Otherwise the LM check or a
    # re-like step came last: a failed re-like left A unliked, and a failed
    # check that comes last never got its restore.
    return status == "failed"


def stranded_unliked_video_ids(session: Session) -> frozenset[str]:
    """Videos ``sync`` or ``canonicalize --apply`` unliked on YouTube that may be liked nowhere now.

    The newest settling row per video (``_may_be_unliked``) decides: an
    ``interrupted`` row, an unlike with nothing after it (a hard kill), or a
    failed re-like / restore. Keyed by the source track's video id across all
    diagnoses, so the state survives a re-run of ``compare-likes`` and an
    alignment finding (a YouTube track) and a canonical finding (a YT Music
    track) about the same video share one history. Rows whose track has no
    video id are left out. A video found in a YouTube scan taken after that
    row has been re-liked since (by hand) and is dropped.
    """
    last_row: dict[str, tuple[bool, datetime]] = {}
    for vid, kind, status, reason, at in session.execute(
        select(
            Track.video_id,
            SyncAttempt.kind,
            SyncAttempt.status,
            SyncAttempt.reason,
            SyncAttempt.created_at,
        )
        .join(DiagnosisItem, SyncAttempt.diagnosis_item_id == DiagnosisItem.id)
        .join(Track, DiagnosisItem.source_track_id == Track.id)
        .where(SyncAttempt.kind.in_(_SETTLING_KINDS))
        .order_by(SyncAttempt.id)
    ):
        if vid:
            last_row[vid] = (_may_be_unliked(kind, status, reason), at)
    stranded = {vid: at for vid, (unliked, at) in last_row.items() if unliked}
    if not stranded:
        return frozenset()
    last_scanned_liked = dict(
        session.execute(
            select(SnapshotItem.video_id, func.max(Snapshot.created_at))
            .join(Snapshot, SnapshotItem.snapshot_id == Snapshot.id)
            .where(
                Snapshot.source == YOUTUBE_LIKED_VIDEOS,
                SnapshotItem.video_id.in_(list(stranded)),
            )
            .group_by(SnapshotItem.video_id)
        )
        .tuples()
        .all()
    )
    return frozenset(
        vid
        for vid, at in stranded.items()
        if vid not in last_scanned_liked or _as_utc(last_scanned_liked[vid]) <= _as_utc(at)
    )


# Only these playlist rows settle an action: B in and A out with both sides
# confirmed, or B out again with A in place. Every other row of an action may
# leave the playlist changed or half-changed.
_PLAYLIST_SETTLING = (("playlist_check", "applied"), ("playlist_rollback_verify", "applied"))
_PLAYLIST_WRITE_KINDS = (
    "playlist_insert",
    "playlist_delete",
    "playlist_restore",
    "playlist_rollback",
    "playlist_interrupted",
)


def unsettled_playlist_actions(session: Session) -> list[tuple[str, int, str, str]]:
    """Playlist repoints an earlier run left possibly half-done.

    Each is (playlist title, A's 1-based position, A, B).

    Per item, in row order, only an applied ``playlist_check`` or
    ``playlist_rollback_verify`` settles it. An item is listed when, after its
    last settling row, a write row was sent (any ``_PLAYLIST_WRITE_KINDS`` row
    but a quota-rejected one, ``playlist_interrupted`` included). Prechecks,
    verifies and reads change nothing, so they never settle. An action is
    dropped once a YT Music and a YouTube scan of the same playlist are both
    newer than its last write row: the next report shows what the playlist holds.
    """
    last_write: dict[int, datetime | None] = {}
    for item_id, kind, status, reason, at in session.execute(
        select(
            SyncAttempt.diagnosis_item_id,
            SyncAttempt.kind,
            SyncAttempt.status,
            SyncAttempt.reason,
            SyncAttempt.created_at,
        )
        .where(SyncAttempt.kind.startswith("playlist_", autoescape=True))
        .order_by(SyncAttempt.id)
    ):
        if (kind, status) in _PLAYLIST_SETTLING:
            last_write[item_id] = None
        elif kind in _PLAYLIST_WRITE_KINDS and not (reason or "").startswith(QUOTA_REJECTED):
            last_write[item_id] = at
    listed: list[tuple[str, int, str, str]] = []
    for item_id, at in last_write.items():
        if at is None:
            continue
        item = session.get(DiagnosisItem, item_id)
        a = session.get(SnapshotItem, item.source_item_id) if item.source_item_id else None
        if a is None:
            continue
        b = session.get(SnapshotItem, item.related_item_id) if item.related_item_id else None
        snap = session.get(Snapshot, a.snapshot_id)
        rescanned = set(
            session.scalars(
                select(Snapshot.source).where(
                    Snapshot.playlist_id == snap.playlist_id,
                    Snapshot.source.in_((YTMUSIC_PLAYLIST, YOUTUBE_PLAYLIST)),
                    Snapshot.created_at > _as_utc(at),
                )
            )
        )
        if len(rescanned) == 2:
            continue
        listed.append(
            (snap.playlist_title or "", a.position + 1, a.video_id or "", b.video_id if b else "")
        )
    return sorted(listed)


def diagnosis_staleness(session: Session, diag: Diagnosis, *, now: datetime) -> list[str]:
    """Human-readable reasons ``diag`` may no longer reflect the accounts.

    Per source: a newer scan exists than the one the diagnosis used (the
    user re-scanned but didn't re-run ``compare-likes``), or the scan it
    used is older than ``STALE_AFTER`` (likes may have changed on the
    provider since). Warning-only; blocking on our own writes is
    ``sync_attempts_since_older_scan``.
    """
    warnings: list[str] = []
    for source, used_id in (
        (YTMUSIC_LIKED_SONGS, diag.ytmusic_snapshot_id),
        (YOUTUBE_LIKED_VIDEOS, diag.youtube_snapshot_id),
    ):
        if used_id is None:
            continue
        latest = latest_snapshot(session, source=source)
        if latest is not None and latest.id != used_id:
            warnings.append(
                f"a newer {source} scan (#{latest.id}) exists than the one diagnosis "
                f"#{diag.id} used (#{used_id}) — re-run compare-likes"
            )
            continue
        used = session.get(Snapshot, used_id)
        if used is None:
            continue
        age = now - _as_utc(used.created_at)
        if age > STALE_AFTER:
            warnings.append(
                f"the {source} scan diagnosis #{diag.id} used (#{used_id}) is "
                f"{age.total_seconds() / 3600:.1f}h old — likes may have changed since; "
                "re-scan + compare-likes before a big sync"
            )
    return warnings


def sync_attempts_since(session: Session, since: datetime) -> int:
    """How many of our own ``sync`` write attempts were recorded strictly after ``since``.

    A diagnosis is only trustworthy when no such write happened after the
    older of its two scans (spec §5.1): one between the scans misaligns them,
    one after both means the lists have changed since. Only ``_WRITE_KINDS``
    rows count — reads (prechecks, verifies, LM checks) and skips change
    nothing — and a 'failed' write may still have landed, so it does.
    """
    return (
        session.scalar(
            select(func.count(SyncAttempt.id)).where(
                SyncAttempt.kind.in_(_WRITE_KINDS),
                SyncAttempt.status != "skipped",
                # Bound as UTC wall-clock, which is how the column is stored.
                SyncAttempt.created_at > _as_utc(since),
            )
        )
        or 0
    )


def sync_attempts_since_older_scan(session: Session, diag: Diagnosis) -> int:
    """``sync_attempts_since`` the older of the two scans ``diag`` was built from.

    A diagnosis whose scans are both gone (deleted) falls back to its own
    creation time — after both scans, so a weaker check, but the one left.
    """
    scan_times = [
        _as_utc(session.get(Snapshot, snap_id).created_at)
        for snap_id in (diag.youtube_snapshot_id, diag.ytmusic_snapshot_id)
        if snap_id is not None
    ]
    return sync_attempts_since(session, min(scan_times, default=_as_utc(diag.created_at)))
