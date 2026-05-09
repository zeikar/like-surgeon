"""Health summary across stored snapshots and the latest diagnosis.

Read-only. No external calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .diff import DiffResult, diff_snapshots
from .models import Snapshot
from .snapshot import get_snapshot_items, latest_snapshot

YTMUSIC = "ytmusic_liked_songs"
YOUTUBE = "youtube_liked_videos"


@dataclass(frozen=True)
class SourceHealth:
    """Per-source view used inside the multi-source report."""

    source: str
    snapshot_count: int
    latest_count: int | None
    last_diff: DiffResult | None


@dataclass(frozen=True)
class DiagnosisSummary:
    diagnosis_id: int
    possibly_missing_from_ytmusic: int
    pointer_drift: int
    ytmusic_only: int
    unavailable_videos: int
    metadata_drift: int


@dataclass(frozen=True)
class MultiSourceHealthReport:
    ytmusic: SourceHealth
    youtube: SourceHealth
    latest_diagnosis: DiagnosisSummary | None
    # match_rate = matched_youtube_music / max(youtube_music_count, 1) * 100.
    # ``None`` when the latest diagnosis is missing or has no music candidates.
    match_rate_percent: float | None


def _source_health(session: Session, source: str) -> SourceHealth:
    snapshot_count = (
        session.scalar(select(func.count(Snapshot.id)).where(Snapshot.source == source)) or 0
    )
    latest = latest_snapshot(session, source=source)
    if latest is None:
        return SourceHealth(
            source=source,
            snapshot_count=snapshot_count,
            latest_count=None,
            last_diff=None,
        )

    prev_stmt = (
        select(Snapshot)
        .where(Snapshot.source == source, Snapshot.id != latest.id)
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(1)
    )
    prev = session.scalar(prev_stmt)
    diff = diff_snapshots(session, prev.id, latest.id) if prev is not None else None

    return SourceHealth(
        source=source,
        snapshot_count=snapshot_count,
        latest_count=latest.raw_count,
        last_diff=diff,
    )


def _latest_diagnosis_summary(session: Session) -> tuple[DiagnosisSummary | None, float | None]:
    from .diagnosis import (
        ISSUE_METADATA_DRIFT,
        ISSUE_POINTER_DRIFT,
        ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        ISSUE_UNAVAILABLE_VIDEO,
        ISSUE_YTMUSIC_ONLY,
        diagnosis_items,
        latest_diagnosis,
    )

    diag = latest_diagnosis(session)
    if diag is None:
        return None, None

    items = diagnosis_items(session, diag.id)
    counts = {
        ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC: 0,
        ISSUE_POINTER_DRIFT: 0,
        ISSUE_YTMUSIC_ONLY: 0,
        ISSUE_UNAVAILABLE_VIDEO: 0,
        ISSUE_METADATA_DRIFT: 0,
    }
    for it in items:
        if it.issue_type in counts:
            counts[it.issue_type] += 1
    summary = DiagnosisSummary(
        diagnosis_id=diag.id,
        possibly_missing_from_ytmusic=counts[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC],
        pointer_drift=counts[ISSUE_POINTER_DRIFT],
        ytmusic_only=counts[ISSUE_YTMUSIC_ONLY],
        unavailable_videos=counts[ISSUE_UNAVAILABLE_VIDEO],
        metadata_drift=counts[ISSUE_METADATA_DRIFT],
    )

    # Health: match_rate = (yt_music_candidates - unmatched) / yt_music_candidates.
    # The "unmatched" count is exactly the high-priority bucket from this run.
    if diag.youtube_snapshot_id is None:
        return summary, None
    yt_items = get_snapshot_items(session, diag.youtube_snapshot_id)
    yt_music_count = sum(1 for it in yt_items if it.is_music_candidate)
    if yt_music_count == 0:
        return summary, None
    matched_music = yt_music_count - counts[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC]
    return summary, max(0.0, min(100.0, matched_music / yt_music_count * 100))


def health_summary(session: Session) -> MultiSourceHealthReport:
    """Multi-source health snapshot. No source argument — always covers both."""
    summary, match_rate = _latest_diagnosis_summary(session)
    return MultiSourceHealthReport(
        ytmusic=_source_health(session, YTMUSIC),
        youtube=_source_health(session, YOUTUBE),
        latest_diagnosis=summary,
        match_rate_percent=match_rate,
    )
