"""Tests for snapshot ingestion policy.

Pinned: a snapshot must record duplicate identities faithfully — that's the
whole reason ``SnapshotItem`` only has a unique constraint on
``(snapshot_id, position)`` and not on ``(snapshot_id, track_id)``.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.snapshot import create_snapshot, get_snapshot_items


def _item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def test_create_snapshot_allows_duplicate_track_within_snapshot(session: Session):
    """Two identical items in one scan → both stored, sharing one master track."""
    duplicated = _item("v1", "Same Song", ["A"])
    snap = create_snapshot(session, "ytmusic", [duplicated, duplicated])
    session.commit()

    assert snap.raw_count == 2
    items = get_snapshot_items(session, snap.id)
    assert len(items) == 2
    # Both items reference the same upserted master track row.
    assert items[0].track_id == items[1].track_id
    assert {it.position for it in items} == {0, 1}
    # Point-in-time metadata is captured on each row independently.
    assert all(it.title == "Same Song" for it in items)
