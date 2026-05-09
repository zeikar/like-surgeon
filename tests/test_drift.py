"""Tests for drift detection — snapshot-pair metadata comparison."""

from __future__ import annotations

import json
from typing import Any


class _FakeSnapshotItem:
    """Duck-typed stand-in for SnapshotItem in pure-function tests."""

    def __init__(
        self,
        *,
        id: int,
        video_id: str | None,
        title: str,
        artists: list[str],
    ) -> None:
        self.id = id
        self.video_id = video_id
        self.title = title
        self.artists = json.dumps(artists)


def test_detect_drift_flags_significant_title_change() -> None:
    """token_sort_ratio < 0.90 → drift finding."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id="v", title="Blueming (Official MV)", artists=["IU"])]
    curr = [
        _FakeSnapshotItem(
            id=2, video_id="v", title="아이유 - Blueming Remastered 2024 [4K]", artists=["IU"]
        )
    ]

    findings = detect_drift(prev, curr, source="youtube_liked_videos")
    assert len(findings) == 1
    f = findings[0]
    assert f.video_id == "v"
    assert f.prev_snapshot_item_id == 1
    assert f.curr_snapshot_item_id == 2
    assert f.title_similarity < 0.9


def test_detect_drift_flags_artists_change_even_with_same_title() -> None:
    """Channel rename / `- Topic` migration keeps title but changes artists."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id="v", title="Same", artists=["1theK"])]
    curr = [_FakeSnapshotItem(id=2, video_id="v", title="Same", artists=["IU - Topic"])]

    findings = detect_drift(prev, curr, source="youtube_liked_videos")
    assert len(findings) == 1
    assert findings[0].artists_changed is True


def test_detect_drift_ignores_cosmetic_changes() -> None:
    """token_sort_ratio >= 0.90 AND artists unchanged → no finding."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id="v", title="Hello World", artists=["A"])]
    curr = [_FakeSnapshotItem(id=2, video_id="v", title="Hello, World!", artists=["A"])]

    findings = detect_drift(prev, curr, source="youtube_liked_videos")
    assert findings == []


def test_detect_drift_ignores_artist_order() -> None:
    """Artists list order changes alone (e.g. ['A', 'B'] → ['B', 'A']) is not drift —
    use set-based comparison after normalisation."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id="v", title="Same", artists=["A", "B"])]
    curr = [_FakeSnapshotItem(id=2, video_id="v", title="Same", artists=["B", "A"])]

    findings = detect_drift(prev, curr, source="youtube_liked_videos")
    assert findings == []


def test_detect_drift_silently_skips_unmatched_video_ids() -> None:
    """Items in curr without a matching prev (new likes) are not drift."""
    from likesurgeon.drift import detect_drift

    prev: list[Any] = []
    curr = [_FakeSnapshotItem(id=1, video_id="new", title="X", artists=["Y"])]

    assert detect_drift(prev, curr, source="youtube_liked_videos") == []


def test_detect_drift_ignores_items_without_video_id() -> None:
    """SnapshotItem.video_id may be None (rare YT Music edge case) — those
    can't be drift-keyed."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id=None, title="X", artists=["Y"])]
    curr = [_FakeSnapshotItem(id=2, video_id=None, title="Z", artists=["W"])]

    assert detect_drift(prev, curr, source="ytmusic_liked_songs") == []


def test_detect_drift_finding_carries_source_and_artists_lists() -> None:
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=10, video_id="v", title="Old", artists=["A"])]
    curr = [_FakeSnapshotItem(id=20, video_id="v", title="New Title Entirely", artists=["B"])]

    findings = detect_drift(prev, curr, source="ytmusic_liked_songs")
    assert findings[0].source == "ytmusic_liked_songs"
    assert findings[0].prev_artists == ("A",)
    assert findings[0].curr_artists == ("B",)
