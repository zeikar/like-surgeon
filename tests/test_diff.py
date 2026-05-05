import pytest
from sqlalchemy.orm import Session

from likesurgeon.diff import diff_snapshots
from likesurgeon.snapshot import create_snapshot


def _item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def test_diff_detects_added_and_removed(session: Session):
    a = _item("v1", "Song A", ["X"])
    b = _item("v2", "Song B", ["Y"])
    c = _item("v3", "Song C", ["Z"])

    old = create_snapshot(session, "ytmusic", [a, b])
    session.commit()
    new = create_snapshot(session, "ytmusic", [b, c])
    session.commit()

    result = diff_snapshots(session, old.id, new.id)
    assert {t.title for t in result.added} == {"Song C"}
    assert {t.title for t in result.removed} == {"Song A"}
    assert result.common_count == 1


def test_diff_identical_snapshots_yields_no_changes(session: Session):
    a = _item("v1", "Song A", ["X"])
    s1 = create_snapshot(session, "ytmusic", [a])
    session.commit()
    s2 = create_snapshot(session, "ytmusic", [a])
    session.commit()

    result = diff_snapshots(session, s1.id, s2.id)
    assert result.added == []
    assert result.removed == []
    assert result.common_count == 1


def test_diff_unknown_snapshot_raises(session: Session):
    with pytest.raises(ValueError):
        diff_snapshots(session, 999, 1000)


def test_diff_falls_back_to_canonical_key_when_video_id_missing(session: Session):
    # Same song, different decoration, no video id → upsert should merge them.
    a = {"title": "Song (Official Audio)", "artists": [{"name": "X"}]}
    b = {"title": "song", "artists": [{"name": "x"}]}
    s1 = create_snapshot(session, "ytmusic", [a])
    session.commit()
    s2 = create_snapshot(session, "ytmusic", [b])
    session.commit()

    result = diff_snapshots(session, s1.id, s2.id)
    assert result.added == []
    assert result.removed == []
    assert result.common_count == 1


def test_diff_uses_point_in_time_metadata(session: Session):
    """The Removed table should show the title from the snapshot, not the
    latest Track row — even after the same video is re-liked under a new
    title (which overwrites Track.title via upsert).
    """
    s1 = create_snapshot(session, "ytmusic", [_item("v1", "Old Title", ["A"])])
    session.commit()
    s2 = create_snapshot(session, "ytmusic", [])
    session.commit()
    # Re-like the same video with a renamed title — this upsert overwrites
    # the master Track row's title to "New Title".
    create_snapshot(session, "ytmusic", [_item("v1", "New Title", ["A"])])
    session.commit()

    result = diff_snapshots(session, s1.id, s2.id)
    assert {t.title for t in result.removed} == {"Old Title"}


def test_diff_reports_duplicate_count_shrink(session: Session):
    """Snapshots can carry duplicate rows (Task 6 policy). When the new
    snapshot has fewer rows for the same track_id than the old one, the
    surplus old rows must be reported as removed.
    """
    same = _item("v1", "Same Song", ["A"])
    s1 = create_snapshot(session, "ytmusic", [same, same])
    session.commit()
    s2 = create_snapshot(session, "ytmusic", [same])
    session.commit()

    result = diff_snapshots(session, s1.id, s2.id)
    assert len(result.added) == 0
    assert len(result.removed) == 1
    assert result.removed[0].title == "Same Song"
    assert result.common_count == 1


def test_diff_reports_duplicate_count_growth(session: Session):
    """The reverse direction: a track that gained a duplicate row."""
    same = _item("v1", "Same Song", ["A"])
    s1 = create_snapshot(session, "ytmusic", [same])
    session.commit()
    s2 = create_snapshot(session, "ytmusic", [same, same])
    session.commit()

    result = diff_snapshots(session, s1.id, s2.id)
    assert len(result.added) == 1
    assert result.added[0].title == "Same Song"
    assert len(result.removed) == 0
    assert result.common_count == 1
