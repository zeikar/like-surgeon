"""Snapshot diffing.

Compares two snapshots and reports which tracks were added, removed, and
shared. Identity is based on ``SnapshotItem.track_id`` — two snapshots that
reference the same master track row are considered to share that track
regardless of position. Display fields (title, artists, …) come from each
snapshot's *point-in-time* row, so the "Removed" table shows the metadata
the user saw in the old snapshot — not whatever the master Track currently
holds.

**Multiset semantics.** Snapshots may contain duplicate rows for the same
``track_id`` (Task 6 policy: scans are recorded faithfully). Diff respects
this: if the old snapshot has two rows for video V and the new snapshot has
one, exactly one V row is reported as ``removed`` (and ``common_count``
increments by ``min(2, 1) = 1``). The schema preserves duplicates; diff
preserves the count of changes between them.
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


def _group_by_track(items: list[SnapshotItem]) -> dict[int, list[SnapshotItem]]:
    """Group snapshot items by ``track_id``, preserving scan order within each group."""
    by_track: dict[int, list[SnapshotItem]] = {}
    for it in items:
        by_track.setdefault(it.track_id, []).append(it)
    return by_track


def diff_snapshots(session: Session, old_id: int, new_id: int) -> DiffResult:
    """Diff two snapshots by master-track identity using multiset semantics.

    For each ``track_id`` that appears in either snapshot, the
    ``min(old_count, new_count)`` rows are common; the surplus on either side
    becomes ``added`` (more rows in new) or ``removed`` (more rows in old).
    Display metadata for each surplus row comes from the SnapshotItem's
    frozen point-in-time columns.
    """
    if get_snapshot(session, old_id) is None:
        raise ValueError(f"Snapshot {old_id} not found")
    if get_snapshot(session, new_id) is None:
        raise ValueError(f"Snapshot {new_id} not found")

    old_items = get_snapshot_items(session, old_id)
    new_items = get_snapshot_items(session, new_id)
    old_by_track = _group_by_track(old_items)
    new_by_track = _group_by_track(new_items)

    # Walk each snapshot in scan order so ``added`` / ``removed`` come back
    # deterministic and visually aligned with the user's original ordering.
    # Track-level ``shared`` is settled separately so multiset surplus on
    # either side is reported faithfully.
    common_count = sum(
        min(len(old_by_track.get(tid, [])), len(new_by_track.get(tid, [])))
        for tid in set(old_by_track) | set(new_by_track)
    )

    new_seen: dict[int, int] = {}
    added: list[TrackSummary] = []
    for it in new_items:
        seen = new_seen.get(it.track_id, 0)
        # First ``min(old_count, new_count)`` rows of this track are "common";
        # rows beyond that index are surplus that goes into ``added``.
        if seen >= len(old_by_track.get(it.track_id, [])):
            added.append(_summarize(it))
        new_seen[it.track_id] = seen + 1

    old_seen: dict[int, int] = {}
    removed: list[TrackSummary] = []
    for it in old_items:
        seen = old_seen.get(it.track_id, 0)
        if seen >= len(new_by_track.get(it.track_id, [])):
            removed.append(_summarize(it))
        old_seen[it.track_id] = seen + 1

    return DiffResult(added=added, removed=removed, common_count=common_count)
