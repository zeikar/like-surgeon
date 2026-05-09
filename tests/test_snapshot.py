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


def test_youtube_translator_reads_likesurgeon_video_status() -> None:
    """Raw item with `_likesurgeon_video_status` augmentation → rec carries
    `is_available` / `unavailable_reason`."""
    from likesurgeon.snapshot import _youtube_to_record

    raw = {
        "snippet": {
            "title": "Song",
            "channelTitle": "Artist",
            "resourceId": {"videoId": "vid1"},
        },
        "contentDetails": {"videoId": "vid1"},
        "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
    }
    rec = _youtube_to_record(raw)
    assert rec["is_available"] is False
    assert rec["unavailable_reason"] == "deleted"


def test_youtube_translator_strips_likesurgeon_namespace_from_raw() -> None:
    """rec["raw"] must NOT contain any `_likesurgeon_*` key — those are
    our internal augmentation and would (a) leak into raw_json and (b)
    crash json.dumps if they were dataclass instances."""
    from likesurgeon.snapshot import _youtube_to_record

    raw = {
        "snippet": {
            "title": "T",
            "channelTitle": "C",
            "resourceId": {"videoId": "vid"},
        },
        "contentDetails": {"videoId": "vid"},
        "_likesurgeon_video_status": {"is_available": True, "reason": None},
    }
    rec = _youtube_to_record(raw)
    assert "_likesurgeon_video_status" not in rec["raw"]
    # Genuine API fields survive.
    assert "snippet" in rec["raw"]


def test_youtube_translator_handles_missing_augmentation() -> None:
    """No injection → rec[is_available] / rec[unavailable_reason] default
    to None. Backward compat with tests/fixtures that don't set the key."""
    from likesurgeon.snapshot import _youtube_to_record

    raw = {
        "snippet": {"title": "T", "channelTitle": "C", "resourceId": {"videoId": "v"}},
        "contentDetails": {"videoId": "v"},
    }
    rec = _youtube_to_record(raw)
    assert rec["is_available"] is None
    assert rec["unavailable_reason"] is None


def test_create_snapshot_persists_is_available_columns(session) -> None:
    """End-to-end: scan-shaped raw item with augmentation → SnapshotItem
    rows have is_available / unavailable_reason set, raw_json is clean."""
    import json as json_lib

    from likesurgeon.snapshot import create_snapshot, get_snapshot_items

    items = [
        {
            "snippet": {
                "title": "Gone",
                "channelTitle": "Owner",
                "resourceId": {"videoId": "vGone"},
            },
            "contentDetails": {"videoId": "vGone"},
            "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
        }
    ]
    snap = create_snapshot(session, "youtube_liked_videos", items)
    rows = get_snapshot_items(session, snap.id)

    assert len(rows) == 1
    assert rows[0].is_available is False
    assert rows[0].unavailable_reason == "deleted"
    # raw_json must not carry the injection.
    assert "_likesurgeon_video_status" not in json_lib.loads(rows[0].raw_json)


def test_create_snapshot_ytmusic_translator_leaves_columns_null(session) -> None:
    """YT Music translator does not set is_available; the columns stay NULL
    even if (somehow) the injection field is present on a YT Music item."""
    from likesurgeon.snapshot import create_snapshot, get_snapshot_items

    items = [
        {
            "videoId": "ytm1",
            "title": "Track",
            "artists": [{"name": "Artist"}],
            "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
        }
    ]
    snap = create_snapshot(session, "ytmusic_liked_songs", items)
    rows = get_snapshot_items(session, snap.id)
    assert rows[0].is_available is None
    assert rows[0].unavailable_reason is None


def test_latest_snapshots_for_source_returns_most_recent_first(session) -> None:
    """Helper returns up to `limit` snapshots of the given source ordered
    most-recent first."""
    from likesurgeon.snapshot import create_snapshot, latest_snapshots_for_source

    yt_items = [
        {
            "snippet": {"title": "T", "channelTitle": "C", "resourceId": {"videoId": "v1"}},
            "contentDetails": {"videoId": "v1"},
        }
    ]
    create_snapshot(session, "youtube_liked_videos", yt_items)
    snap_b = create_snapshot(session, "youtube_liked_videos", yt_items)
    snap_c = create_snapshot(session, "youtube_liked_videos", yt_items)

    out = latest_snapshots_for_source(session, "youtube_liked_videos", limit=2)
    assert [s.id for s in out] == [snap_c.id, snap_b.id]


def test_latest_snapshots_for_source_filters_by_source(session) -> None:
    """Only snapshots of the requested source are returned."""
    from likesurgeon.snapshot import create_snapshot, latest_snapshots_for_source

    create_snapshot(
        session,
        "youtube_liked_videos",
        [
            {
                "snippet": {"title": "Y", "channelTitle": "C", "resourceId": {"videoId": "v"}},
                "contentDetails": {"videoId": "v"},
            }
        ],
    )
    create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            {
                "videoId": "ytm",
                "title": "T",
                "artists": [{"name": "A"}],
            }
        ],
    )

    out = latest_snapshots_for_source(session, "ytmusic_liked_songs", limit=5)
    assert len(out) == 1
    assert out[0].source == "ytmusic_liked_songs"


def test_latest_snapshots_for_source_returns_empty_when_no_snapshots(session) -> None:
    from likesurgeon.snapshot import latest_snapshots_for_source

    assert latest_snapshots_for_source(session, "youtube_liked_videos", limit=2) == []
