"""Tests for the cross-source compare engine."""

from __future__ import annotations

from dataclasses import dataclass

from likesurgeon.compare import (
    CompareInput,
    MatchKind,
    compare_likes,
)


@dataclass(frozen=True)
class _FakeItem:
    """A minimal stand-in for SnapshotItem with the fields compare_likes reads."""

    track_id: int
    video_id: str | None
    title: str
    artists: tuple[str, ...]
    canonical_key: str
    is_music_candidate: bool | None = None


def _yt(track_id, video_id, title, artists, music=True):
    return _FakeItem(
        track_id=track_id,
        video_id=video_id,
        title=title,
        artists=tuple(artists),
        canonical_key=f"{', '.join(sorted(a.lower() for a in artists))}|{title.lower()}",
        is_music_candidate=music,
    )


def _ytm(track_id, video_id, title, artists):
    return _FakeItem(
        track_id=track_id,
        video_id=video_id,
        title=title,
        artists=tuple(artists),
        canonical_key=f"{', '.join(sorted(a.lower() for a in artists))}|{title.lower()}",
        is_music_candidate=None,  # ytmusic source: classifier not run
    )


def test_video_id_exact_match():
    ytm = [_ytm(1, "v1", "Song A", ["X"])]
    yt = [_yt(2, "v1", "Song A (Official MV)", ["X"])]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    m = res.matched[0]
    assert m.kind is MatchKind.VIDEO_ID
    assert m.ytmusic_track_id == 1
    assert m.youtube_track_id == 2


def test_canonical_key_match_when_video_id_differs():
    ytm = [_ytm(1, "vmusic", "Song A", ["X"])]
    yt = [_yt(2, "vyoutube", "song a", ["x"])]  # canonical key matches
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.CANONICAL_KEY


def test_fuzzy_match_when_neither_video_id_nor_canonical_match():
    """RapidFuzz fallback for slight misspellings / decoration."""
    ytm = [_ytm(1, "vm", "Imagine", ["John Lennon"])]
    yt = [_yt(2, "vy", "Imagine - John Lennon", ["RandomChannel"])]
    res = compare_likes(
        CompareInput(ytmusic=ytm, youtube=yt, fuzzy_threshold=80)
    )
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.FUZZY
    assert res.matched[0].confidence >= 0.8


def test_possibly_missing_from_ytmusic_when_yt_video_unmatched():
    """Music-candidate YT videos with no match anywhere → possibly_missing_from_ytmusic."""
    ytm: list[_FakeItem] = []
    yt = [_yt(2, "vy", "Cover Song (Official Audio)", ["Cover Artist"])]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert res.matched == []
    assert len(res.possibly_missing_from_ytmusic) == 1


def test_non_music_youtube_videos_excluded():
    """Non-music YT likes shouldn't appear in any output bucket."""
    ytm: list[_FakeItem] = []
    yt = [_yt(2, "vy", "Some random vlog", ["VlogChannel"], music=False)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert res.matched == []
    assert res.possibly_missing_from_ytmusic == []
    assert res.ytmusic_only_likes == []
    assert res.pointer_drift_candidates == []


def test_pointer_drift_candidates_collect_fuzzy_matches():
    """Fuzzy matches should also surface as pointer_drift_candidates."""
    ytm = [_ytm(1, "vm", "Imagine", ["John Lennon"])]
    yt = [_yt(2, "vy", "Imagine - John Lennon", ["RandomChannel"])]
    res = compare_likes(
        CompareInput(ytmusic=ytm, youtube=yt, fuzzy_threshold=80)
    )
    assert len(res.pointer_drift_candidates) == 1
    assert res.pointer_drift_candidates[0].ytmusic_track_id == 1


def test_duplicate_ytmusic_rows_yield_surplus_in_ytmusic_only_likes():
    """0.1 lets a snapshot keep duplicate rows for the same identity.
    When YT Music has two such rows and YouTube has one, only ONE pair
    matches; the surplus YT Music row falls into ytmusic_only_likes.
    Pre-row-identity bug: the surplus would silently disappear because
    ``used_ytm`` was keyed on track_id."""
    ytm = [
        _ytm(1, "v1", "A", ["X"]),
        _ytm(1, "v1", "A", ["X"]),  # duplicate row, identical identity
    ]
    yt = [_yt(2, "v1", "A (Official MV)", ["X"], music=True)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.VIDEO_ID
    assert len(res.ytmusic_only_likes) == 1
    assert res.possibly_missing_from_ytmusic == []


def test_duplicate_youtube_rows_yield_surplus_in_possibly_missing():
    """The other direction: YouTube has the same music identity twice,
    YT Music has it once. One match, one surplus YT row in
    possibly_missing_from_ytmusic."""
    ytm = [_ytm(1, "v1", "A", ["X"])]
    yt = [
        _yt(2, "v1", "A (Official MV)", ["X"], music=True),
        _yt(2, "v1", "A (Official MV)", ["X"], music=True),  # duplicate
    ]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.VIDEO_ID
    assert len(res.possibly_missing_from_ytmusic) == 1
    assert res.ytmusic_only_likes == []


def test_handles_json_encoded_artists_from_snapshot_item():
    """SnapshotItem stores artists as a JSON-encoded string. compare_likes
    must accept that shape directly without an adapter step in the CLI."""
    import json as _json
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _SnapshotItemish:
        track_id: int
        video_id: str | None
        title: str
        artists: str  # JSON-encoded list, like real SnapshotItem.artists
        canonical_key: str
        is_music_candidate: bool | None

    ytm = [
        _SnapshotItemish(
            track_id=1,
            video_id="v1",
            title="Song A",
            artists=_json.dumps(["X"]),
            canonical_key="x|song a",
            is_music_candidate=None,
        )
    ]
    yt = [
        _SnapshotItemish(
            track_id=2,
            video_id="v1",
            title="Song A (Official MV)",
            artists=_json.dumps(["X"]),
            canonical_key="x|song a",
            is_music_candidate=True,
        )
    ]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.VIDEO_ID


def test_counts_summary():
    ytm = [
        _ytm(1, "v1", "A", ["X"]),
        _ytm(2, "v2", "B", ["Y"]),
    ]
    yt = [
        _yt(3, "v1", "A (Official MV)", ["X"], music=True),
        _yt(4, "vN", "Random vlog", ["Vlog"], music=False),
        _yt(5, "vK", "Some music I love", ["RandomChannel"], music=True),
    ]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert res.ytmusic_count == 2
    assert res.youtube_total_count == 3
    assert res.youtube_music_count == 2
    assert len(res.matched) == 1  # v1 matched by video_id
    assert len(res.possibly_missing_from_ytmusic) == 1  # vK has no ytmusic match
