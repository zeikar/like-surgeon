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
    snap = create_snapshot(session, "ytmusic_liked_songs", [duplicated, duplicated])
    session.commit()

    assert snap.raw_count == 2
    items = get_snapshot_items(session, snap.id)
    assert len(items) == 2
    # Both items reference the same upserted master track row.
    assert items[0].track_id == items[1].track_id
    assert {it.position for it in items} == {0, 1}
    # Point-in-time metadata is captured on each row independently.
    assert all(it.title == "Same Song" for it in items)


def test_create_snapshot_unknown_source_raises(session: Session):
    import pytest

    with pytest.raises(ValueError, match="Unknown source"):
        create_snapshot(session, "spotify_likes", [])


def test_youtube_translator_classifies_music_video(session: Session):
    """A YouTube playlistItem with 'Official Music Video' in title should
    land with is_music_candidate=True and a score recording the signal."""
    item = {
        "snippet": {
            "title": "Song Name (Official Music Video)",
            "channelTitle": "ArtistVEVO",
            "description": "",
            "resourceId": {"videoId": "yt1"},
            "thumbnails": {},
        },
        "contentDetails": {"videoId": "yt1"},
    }
    snap = create_snapshot(session, "youtube_liked_videos", [item])
    session.commit()
    items = get_snapshot_items(session, snap.id)
    assert len(items) == 1
    it = items[0]
    assert it.video_id == "yt1"
    assert it.is_music_candidate is True
    assert it.music_candidate_score is not None
    assert it.music_candidate_score >= 2
    assert it.music_candidate_reason is not None
    # Channel name went into artists.
    import json as jsonlib

    assert jsonlib.loads(it.artists) == ["ArtistVEVO"]


def test_youtube_translator_marks_vlog_as_not_music(session: Session):
    item = {
        "snippet": {
            "title": "Daily vlog #42",
            "channelTitle": "VlogChannel",
            "description": "",
            "resourceId": {"videoId": "yt2"},
        },
        "contentDetails": {"videoId": "yt2"},
    }
    snap = create_snapshot(session, "youtube_liked_videos", [item])
    session.commit()
    items = get_snapshot_items(session, snap.id)
    assert items[0].is_music_candidate is False


def test_ytmusic_translator_leaves_classifier_columns_null(session: Session):
    """Sources that are guaranteed-music shouldn't fill classifier columns."""
    item = {
        "videoId": "v1",
        "title": "Song",
        "artists": [{"name": "Artist"}],
    }
    snap = create_snapshot(session, "ytmusic_liked_songs", [item])
    session.commit()
    items = get_snapshot_items(session, snap.id)
    assert items[0].is_music_candidate is None
    assert items[0].music_candidate_score is None
    assert items[0].music_candidate_reason is None
