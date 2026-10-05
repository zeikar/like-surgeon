"""Tests for the helpers left in compare.py."""

from __future__ import annotations

from dataclasses import dataclass

from likesurgeon.compare import dedupe_by_video_id


@dataclass(frozen=True)
class _PosItem:
    """Minimal stand-in for the ``dedupe_by_video_id`` Protocol."""

    video_id: str | None
    position: int


def test_dedupe_by_video_id_drops_extra_rows_per_video_id():
    """Three rows sharing a video_id at positions 5, 2, 10 → keep position 2."""
    items = [
        _PosItem(video_id="X", position=5),
        _PosItem(video_id="X", position=2),
        _PosItem(video_id="X", position=10),
    ]

    out = dedupe_by_video_id(items)

    assert len(out) == 1
    assert out[0].position == 2


def test_dedupe_by_video_id_preserves_null_video_id_rows():
    """video_id IS NULL rows have no identity — every NULL row passes through."""
    items = [
        _PosItem(video_id=None, position=0),
        _PosItem(video_id="X", position=1),
        _PosItem(video_id="X", position=2),
        _PosItem(video_id=None, position=3),
    ]

    out = dedupe_by_video_id(items)

    assert len(out) == 3
    # Sorted by position ascending; the position-1 X wins.
    positions = [it.position for it in out]
    assert positions == [0, 1, 3]


def test_dedupe_by_video_id_is_idempotent():
    items = [
        _PosItem(video_id="X", position=5),
        _PosItem(video_id="X", position=2),
        _PosItem(video_id="Y", position=1),
        _PosItem(video_id=None, position=7),
    ]

    once = dedupe_by_video_id(items)
    twice = dedupe_by_video_id(once)

    assert once == twice
