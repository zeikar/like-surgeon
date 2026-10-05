"""Read-only DB checks the ``sync`` command runs before planning/writing.

Kept apart from ``sync.py`` so the planner stays pure: these query the DB and
hand ``sync.plan`` plain values (a video-id set) or the CLI warning strings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Diagnosis, DiagnosisItem, Snapshot, SnapshotItem, SyncAttempt
from .snapshot import YOUTUBE_LIKED_VIDEOS, YTMUSIC_LIKED_SONGS, latest_snapshot
from .sync import _video_ids_for_tracks

STALE_AFTER = timedelta(hours=1)

# Attempt kinds whose success means "we liked this video on YouTube".
_YT_LIKE_KINDS = ("yt_like_yt_rate", "yt_relike_like")


def youtube_liked_video_ids(session: Session, diag: Diagnosis) -> frozenset[str] | None:
    """Video ids the DB knows to be liked on YouTube, or ``None`` if unknown.

    Union of the diagnosis's YouTube snapshot and every video a past
    ``sync`` liked on YouTube (``yt_like`` / ``yt_relike``) — the latter
    covers likes made after that snapshot was taken, which is exactly when
    the ``yt_like`` → ``duplicate_in_source`` → ``ytm_dedupe`` loop starts.
    ``None`` when the diagnosis has no YouTube snapshot (deleted): the set
    would be incomplete, and the planner then treats every dedupe as unsafe.
    Likes made by hand after the scan are invisible here — hence the
    scan-age warning in ``diagnosis_staleness``.
    """
    if diag.youtube_snapshot_id is None:
        return None
    snapshot_vids = session.scalars(
        select(SnapshotItem.video_id).where(SnapshotItem.snapshot_id == diag.youtube_snapshot_id)
    )
    ids = {vid for vid in snapshot_vids if vid}
    rows = session.execute(
        select(SyncAttempt.kind, DiagnosisItem.source_track_id, DiagnosisItem.related_track_id)
        .join(DiagnosisItem, SyncAttempt.diagnosis_item_id == DiagnosisItem.id)
        .where(
            SyncAttempt.status == "applied",
            SyncAttempt.kind.in_(_YT_LIKE_KINDS),
        )
    ).all()
    # yt_like likes its ytmusic_only source track; yt_relike likes the
    # drift's related (YT Music side) track.
    track_ids = {src if kind == "yt_like_yt_rate" else rel for kind, src, rel in rows}
    track_ids.discard(None)
    ids.update(vid for vid in _video_ids_for_tracks(session, track_ids).values() if vid)
    return frozenset(ids)


def stranded_unliked_video_ids(session: Session) -> frozenset[str]:
    """Videos a ``ytm_like`` unliked on YouTube whose most recent re-like failed.

    These are liked nowhere until a re-like lands (typically the quota ran
    out between the two calls). Keyed by track across all diagnoses, so the
    state survives a re-run of ``compare-likes``.
    """
    last_relike_failed: dict[int, bool] = {}
    for track_id, status in session.execute(
        select(DiagnosisItem.source_track_id, SyncAttempt.status)
        .join(DiagnosisItem, SyncAttempt.diagnosis_item_id == DiagnosisItem.id)
        .where(SyncAttempt.kind == "ytm_like_yt_relike")
        .order_by(SyncAttempt.id)
    ):
        if track_id is not None:
            last_relike_failed[track_id] = status == "failed"
    stranded = {tid for tid, failed in last_relike_failed.items() if failed}
    return frozenset(vid for vid in _video_ids_for_tracks(session, stranded).values() if vid)


def diagnosis_staleness(session: Session, diag: Diagnosis, *, now: datetime) -> list[str]:
    """Human-readable reasons ``diag`` may no longer reflect the accounts.

    Per source: a newer scan exists than the one the diagnosis used (the
    user re-scanned but didn't re-run ``compare-likes``), or the scan it
    used is older than ``STALE_AFTER`` (likes may have changed on the
    provider since — including a hand-made YouTube like the dedupe guard
    can't see). Warning-only; blocking is left to the user.
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
        scanned = used.created_at
        # SQLite hands DateTime(timezone=True) back naive; values are stored as UTC.
        if scanned.tzinfo is None:
            scanned = scanned.replace(tzinfo=UTC)
        age = now - scanned
        if age > STALE_AFTER:
            warnings.append(
                f"the {source} scan diagnosis #{diag.id} used (#{used_id}) is "
                f"{age.total_seconds() / 3600:.1f}h old — likes may have changed since; "
                "re-scan + compare-likes before a big sync"
            )
    return warnings
