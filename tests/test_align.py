"""Tests for the pure LL→LM order alignment."""

from __future__ import annotations

from dataclasses import dataclass

from likesurgeon.align import AlignmentResult, align, pair_passes_sanity
from likesurgeon.compare import CanonicalMetadata


@dataclass(frozen=True)
class _Item:
    """Minimal stand-in for a ``SnapshotItem`` row (LL or LM)."""

    video_id: str | None
    position: int
    track_id: int
    title: str
    is_available: bool | None = True
    unavailable_reason: str | None = None
    is_music_candidate: bool | None = True


def _items(*video_ids: str | None) -> list[_Item]:
    return [
        _Item(video_id=v, position=n, track_id=n, title=f"title {v}")
        for n, v in enumerate(video_ids)
    ]


def _pairs(result: AlignmentResult) -> list[tuple[str | None, str | None, str]]:
    """``(LM video_id, backing LL video_id, kind)`` per LM entry, in LM order."""
    return [
        (b.lm_item.video_id, b.ll_item.video_id if b.ll_item else None, b.kind)
        for b in result.backings
    ]


def test_identical_lists_are_all_self():
    ll = _items("v1", "v2", "v3")
    res = align(ll, _items("v1", "v2", "v3"))
    assert _pairs(res) == [("v1", "v1", "self"), ("v2", "v2", "self"), ("v3", "v3", "self")]
    assert [b.lm_index for b in res.backings] == [0, 1, 2]
    assert [b.ll_index for b in res.backings] == [0, 1, 2]
    assert res.backings[1].ll_item is ll[1]
    assert res.rendered_ll == frozenset({0, 1, 2})
    assert res.ambiguous_ll == frozenset()
    assert res.anchor_count == 3
    assert res.unrendered_ll() == []


def test_single_relink_in_the_middle_pairs_with_its_ll_video():
    res = align(_items("v1", "A", "v3"), _items("v1", "B", "v3"))
    assert _pairs(res) == [("v1", "v1", "self"), ("B", "A", "rendered"), ("v3", "v3", "self")]
    assert res.backings[1].ll_index == 1
    assert res.rendered_ll == frozenset({0, 1, 2})
    assert res.anchor_count == 2


def test_consecutive_relinks_pair_in_order():
    res = align(_items("v1", "A1", "A2", "A3", "v5"), _items("v1", "B1", "B2", "B3", "v5"))
    assert _pairs(res) == [
        ("v1", "v1", "self"),
        ("B1", "A1", "rendered"),
        ("B2", "A2", "rendered"),
        ("B3", "A3", "rendered"),
        ("v5", "v5", "self"),
    ]
    assert res.ambiguous_ll == frozenset()


def test_ll_only_videos_with_no_lm_entries_in_their_gap_are_unrendered():
    ll = _items("v1", "vlog1", "vlog2", "v4")
    res = align(ll, _items("v1", "v4"))
    assert _pairs(res) == [("v1", "v1", "self"), ("v4", "v4", "self")]
    assert res.rendered_ll == frozenset({0, 3})
    # Nothing in LM could render them, so they are definitely unrendered, not ambiguous.
    assert res.ambiguous_ll == frozenset()
    assert res.unrendered_ll() == [ll[1], ll[2]]


def test_gap_with_fewer_lm_entries_than_ll_videos_is_unbacked_and_ambiguous():
    res = align(_items("v1", "A1", "A2", "v4"), _items("v1", "B", "v4"))
    assert _pairs(res) == [("v1", "v1", "self"), ("B", None, "unbacked"), ("v4", "v4", "self")]
    assert res.backings[1].ll_index is None
    assert res.ambiguous_ll == frozenset({1, 2})
    assert res.rendered_ll == frozenset({0, 3})
    assert res.unrendered_ll() == []


def test_gap_with_more_lm_entries_than_ll_videos_is_unbacked():
    res = align(_items("v1", "A", "v3"), _items("v1", "B1", "B2", "v3"))
    assert _pairs(res) == [
        ("v1", "v1", "self"),
        ("B1", None, "unbacked"),
        ("B2", None, "unbacked"),
        ("v3", "v3", "self"),
    ]
    assert res.ambiguous_ll == frozenset({1})
    assert res.rendered_ll == frozenset({0, 2})


def test_duplicate_with_shadow_older_than_own_like_anchors_the_earlier_copy():
    # B was liked recently (top of LL), and an older like A is also rendered as B,
    # so LM shows B twice. Anchoring the later copy would leave B and R2 unbacked.
    res = align(_items("B", "R", "A", "v4"), _items("B", "R2", "B", "v4"))
    assert _pairs(res) == [
        ("B", "B", "self"),
        ("R2", "R", "rendered"),
        ("B", "A", "rendered"),
        ("v4", "v4", "self"),
    ]
    assert res.anchor_count == 2
    assert res.rendered_ll == frozenset({0, 1, 2, 3})
    assert res.ambiguous_ll == frozenset()


def test_duplicate_with_shadow_newer_than_own_like_anchors_the_later_copy():
    # The MV M5 was liked after its audio track T5 and renders as T5, so the first
    # LM copy of T5 is M5's like and the second is T5's own.
    res = align(_items("v1", "M5", "T5", "v9"), _items("v1", "T5", "T5", "v9"))
    assert _pairs(res) == [
        ("v1", "v1", "self"),
        ("T5", "M5", "rendered"),
        ("T5", "T5", "self"),
        ("v9", "v9", "self"),
    ]
    assert res.rendered_ll == frozenset({0, 1, 2, 3})
    # The unanchored T5 copy's own video is rendered, so it isn't ambiguous.
    assert res.ambiguous_ll == frozenset()


def test_lis_anchors_survive_an_out_of_order_entry_at_the_top():
    # A greedy pass would anchor v4 first and then lose v1..v3.
    ll = _items("v1", "v2", "v3", "v4")
    res = align(ll, _items("v4", "v1", "v2", "v3"))
    assert _pairs(res) == [
        ("v4", None, "unbacked"),
        ("v1", "v1", "self"),
        ("v2", "v2", "self"),
        ("v3", "v3", "self"),
    ]
    assert res.anchor_count == 3


def test_ll_video_with_an_unanchored_lm_copy_is_ambiguous_not_unrendered():
    ll = _items("v1", "v2", "v3", "v4", "v5")
    res = align(ll, _items("x", "v4", "v1", "v2", "v3", "v5"))
    assert res.backings[1].kind == "unbacked"
    assert res.ambiguous_ll == frozenset({3})
    assert res.unrendered_ll() == []


def test_leading_and_trailing_gaps_pair_against_list_ends():
    res = align(_items("A0", "v1", "v2", "A3"), _items("B0", "v1", "v2", "B3"))
    assert _pairs(res) == [
        ("B0", "A0", "rendered"),
        ("v1", "v1", "self"),
        ("v2", "v2", "self"),
        ("B3", "A3", "rendered"),
    ]
    assert res.rendered_ll == frozenset({0, 1, 2, 3})


def test_repeated_ll_video_anchors_on_its_first_ll_index():
    ll = _items("X", "v1", "X")
    res = align(ll, _items("X", "v1"))
    assert _pairs(res) == [("X", "X", "self"), ("v1", "v1", "self")]
    assert res.backings[0].ll_item is ll[0]
    assert res.unrendered_ll() == [ll[2]]


def test_lm_entry_without_video_id_never_anchors_or_pairs():
    res = align(_items("v1", None, "v3"), _items("v1", None, "v3"))
    assert _pairs(res) == [("v1", "v1", "self"), (None, None, "unbacked"), ("v3", "v3", "self")]
    assert res.anchor_count == 2
    assert res.rendered_ll == frozenset({0, 2})
    assert res.ambiguous_ll == frozenset({1})


def test_gap_containing_an_lm_entry_without_video_id_is_not_paired():
    res = align(_items("v1", "A1", "A2", "v4"), _items("v1", "B1", None, "v4"))
    assert _pairs(res) == [
        ("v1", "v1", "self"),
        ("B1", None, "unbacked"),
        (None, None, "unbacked"),
        ("v4", "v4", "self"),
    ]
    assert res.ambiguous_ll == frozenset({1, 2})


def _meta(title: str, channel_id: str = "UC1", duration_seconds: int = 200) -> CanonicalMetadata:
    return CanonicalMetadata(
        video_id="x", title=title, channel_id=channel_id, duration_seconds=duration_seconds
    )


def test_sanity_accepts_duration_within_three_seconds_on_same_channel():
    assert pair_passes_sanity(
        _meta("Song", duration_seconds=200), _meta("Other", duration_seconds=203)
    )


def test_sanity_rejects_duration_four_seconds_apart():
    assert not pair_passes_sanity(
        _meta("Song", duration_seconds=200), _meta("Song", duration_seconds=204)
    )


def test_sanity_accepts_different_channel_when_one_title_contains_the_other():
    # Containment only holds after normalization (casefold), in either direction.
    a = _meta("Yume no Uta (Official Music Video)", channel_id="UC_label")
    b = _meta("YUME NO UTA", channel_id="UC_topic")
    assert pair_passes_sanity(a, b)
    assert pair_passes_sanity(b, a)


def test_sanity_rejects_different_channel_and_unrelated_titles():
    assert not pair_passes_sanity(_meta("Song A", "UC1"), _meta("Song B", "UC2"))


def test_sanity_rejects_empty_title_containment():
    assert not pair_passes_sanity(_meta("", "UC1"), _meta("Song", "UC2"))


def test_sanity_rejects_missing_metadata():
    assert not pair_passes_sanity(None, _meta("Song"))
    assert not pair_passes_sanity(_meta("Song"), None)
    assert not pair_passes_sanity(None, None)
