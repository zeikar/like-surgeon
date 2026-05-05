"""Snapshot diffing.

Compares two snapshots and reports which tracks were added, removed, and
shared. Identity is based on ``SnapshotItem.track_id`` — two snapshots that
reference the same master track row are considered to share that track
regardless of position. Display fields (title, artists, …) come from each
snapshot's *point-in-time* row, so the "Removed" table shows the metadata
the user saw in the old snapshot — not whatever the master Track currently
holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import SnapshotItem
from .snapshot import get_snapshot, get_snapshot_items


@dataclass(frozen=True)
class TrackSummary:
    """Light view of a snapshot item for diff output."""

    track_id: int
    title: str
    artists: list[str]
    canonical_key: str
    video_id: str | None


@dataclass(frozen=True)
class DiffResult:
    added: list[TrackSummary]
    removed: list[TrackSummary]
    common_count: int


def _summarize(item: SnapshotItem) -> TrackSummary:
    return TrackSummary(
        track_id=item.track_id,
        title=item.title,
        artists=json.loads(item.artists) if item.artists else [],
        canonical_key=item.canonical_key,
        video_id=item.video_id,
    )


def diff_snapshots(session: Session, old_id: int, new_id: int) -> DiffResult:
    """Diff two snapshots by master-track identity, using point-in-time metadata for display."""
    if get_snapshot(session, old_id) is None:
        raise ValueError(f"Snapshot {old_id} not found")
    if get_snapshot(session, new_id) is None:
        raise ValueError(f"Snapshot {new_id} not found")

    old_items = {it.track_id: it for it in get_snapshot_items(session, old_id)}
    new_items = {it.track_id: it for it in get_snapshot_items(session, new_id)}

    added = [_summarize(it) for tid, it in new_items.items() if tid not in old_items]
    removed = [_summarize(it) for tid, it in old_items.items() if tid not in new_items]
    common = len(set(old_items) & set(new_items))
    return DiffResult(added=added, removed=removed, common_count=common)
