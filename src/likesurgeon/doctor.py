"""Health summary across stored snapshots and the latest diagnosis.

Read-only. No external calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .diagnosis import ISSUE_UNBACKED_LM_ENTRY, count_findings, latest_diagnosis
from .diff import DiffResult, diff_snapshots
from .models import Snapshot
from .snapshot import latest_snapshot

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
    counts: dict[str, int]  # ``FindingCounts.total``


@dataclass(frozen=True)
class MultiSourceHealthReport:
    ytmusic: SourceHealth
    youtube: SourceHealth
    latest_diagnosis: DiagnosisSummary | None
    # Share of the diagnosed LM entries traced to a YouTube like:
    # (1 − unbacked_lm_entry / LM snapshot rows) * 100. ``None`` without a
    # diagnosis, or when its LM snapshot is gone or empty.
    lm_backed_percent: float | None


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
    diag = latest_diagnosis(session)
    if diag is None:
        return None, None

    counts = count_findings(session, diag.id).total
    summary = DiagnosisSummary(diagnosis_id=diag.id, counts=counts)

    if diag.ytmusic_snapshot_id is None:
        return summary, None
    lm_snap = session.get(Snapshot, diag.ytmusic_snapshot_id)
    if lm_snap is None or lm_snap.raw_count == 0:
        return summary, None
    return summary, (1 - counts[ISSUE_UNBACKED_LM_ENTRY] / lm_snap.raw_count) * 100


def health_summary(session: Session) -> MultiSourceHealthReport:
    """Multi-source health snapshot. No source argument — always covers both."""
    summary, lm_backed = _latest_diagnosis_summary(session)
    return MultiSourceHealthReport(
        ytmusic=_source_health(session, YTMUSIC),
        youtube=_source_health(session, YOUTUBE),
        latest_diagnosis=summary,
        lm_backed_percent=lm_backed,
    )
