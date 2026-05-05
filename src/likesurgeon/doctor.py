"""Health summary across stored snapshots.

Read-only. No external calls. Used by ``likesurgeon doctor``.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .diff import DiffResult, diff_snapshots
from .models import Snapshot
from .snapshot import latest_snapshot


@dataclass(frozen=True)
class HealthReport:
    total_snapshots: int
    latest_source: str | None
    latest_count: int | None
    last_diff: DiffResult | None


def health_summary(session: Session, source: str = "ytmusic") -> HealthReport:
    total = session.scalar(select(func.count(Snapshot.id))) or 0
    latest = latest_snapshot(session, source=source)
    if latest is None:
        return HealthReport(
            total_snapshots=total,
            latest_source=None,
            latest_count=None,
            last_diff=None,
        )

    # raw_count == # of SnapshotItem rows by construction (one per scan item).
    count = latest.raw_count

    prev_stmt = (
        select(Snapshot)
        .where(Snapshot.source == source, Snapshot.id != latest.id)
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(1)
    )
    prev = session.scalar(prev_stmt)
    diff = diff_snapshots(session, prev.id, latest.id) if prev is not None else None

    return HealthReport(
        total_snapshots=total,
        latest_source=latest.source,
        latest_count=count,
        last_diff=diff,
    )
