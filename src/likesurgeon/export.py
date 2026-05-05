"""Snapshot export.

MVP 0.1 supports JSON only. Future formats (CSV, M3U) will plug in as
additional functions; the CLI dispatches by ``--format``.

Reads from ``SnapshotItem`` (point-in-time metadata) — exporting an old
snapshot returns the metadata captured at scan time, not the master Track's
current values.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .dtos import SnapshotDTO, TrackDTO
from .snapshot import get_snapshot, get_snapshot_items


def export_snapshot_json(session: Session, snapshot_id: int) -> str:
    """Return a pretty-printed JSON string for the snapshot."""
    snapshot = get_snapshot(session, snapshot_id)
    if snapshot is None:
        raise ValueError(f"Snapshot {snapshot_id} not found")

    items = get_snapshot_items(session, snapshot_id)
    payload = {
        "snapshot": SnapshotDTO.from_orm_snapshot(snapshot).model_dump(mode="json"),
        "tracks": [
            TrackDTO.from_snapshot_item(item, source=snapshot.source).model_dump(mode="json")
            for item in items
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
