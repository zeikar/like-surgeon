"""Tests for snapshot export — including the point-in-time guarantee."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from likesurgeon.export import export_snapshot_json
from likesurgeon.snapshot import create_snapshot


def _item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def test_export_returns_snapshot_and_tracks(session: Session):
    snap = create_snapshot(session, "ytmusic", [_item("v1", "A", ["X"])])
    session.commit()

    payload = json.loads(export_snapshot_json(session, snap.id))
    assert payload["snapshot"]["id"] == snap.id
    assert payload["snapshot"]["raw_count"] == 1
    assert payload["snapshot"]["source"] == "ytmusic"
    assert len(payload["tracks"]) == 1
    assert payload["tracks"][0]["title"] == "A"
    assert payload["tracks"][0]["video_id"] == "v1"
    assert payload["tracks"][0]["artists"] == ["X"]


def test_export_preserves_point_in_time_metadata(session: Session):
    """The whole reason the SnapshotItem table carries metadata: an old
    snapshot's exported title must not change just because the same video
    was later re-liked under a new title.
    """
    s_old = create_snapshot(session, "ytmusic", [_item("v1", "Old Title", ["A"])])
    session.commit()
    # Same video re-liked with renamed metadata — the upsert overwrites
    # Track.title to "New Title". SnapshotItem of s_old must not move.
    s_new = create_snapshot(session, "ytmusic", [_item("v1", "New Title", ["A"])])
    session.commit()

    old_payload = json.loads(export_snapshot_json(session, s_old.id))
    new_payload = json.loads(export_snapshot_json(session, s_new.id))
    assert old_payload["tracks"][0]["title"] == "Old Title"
    assert new_payload["tracks"][0]["title"] == "New Title"


def test_export_unknown_snapshot_raises(session: Session):
    with pytest.raises(ValueError):
        export_snapshot_json(session, 999)
