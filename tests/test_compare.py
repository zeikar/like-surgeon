"""Tests for the cross-source compare engine."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from likesurgeon.compare import (
    CanonicalMetadata,
    CompareInput,
    CompareResult,
    Match,
    MatchKind,
    Stage4Candidate,
    Stage4Evidence,
    Stage4Match,
    Stage4Result,
    UnmatchedItem,
    apply_stage4_result,
    compare_likes,
    dedupe_by_video_id,
    stage4_enrich_drift,
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
    is_available: bool | None = None


def _yt(track_id, video_id, title, artists, music=True, available=None):
    return _FakeItem(
        track_id=track_id,
        video_id=video_id,
        title=title,
        artists=tuple(artists),
        canonical_key=f"{', '.join(sorted(a.lower() for a in artists))}|{title.lower()}",
        is_music_candidate=music,
        is_available=available,
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
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt, fuzzy_threshold=80))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.FUZZY
    assert res.matched[0].confidence >= 0.8


def test_ghost_yt_video_excluded_from_possibly_missing_from_ytmusic():
    """Regression for 0.4-0.6 silent cross-prop bug.

    A YouTube ghost vid (``is_available=False``, e.g. region-blocked /
    deleted / private) that matcher missed on ytmusic side was previously
    surfaced as BOTH ``unavailable_video`` AND
    ``possibly_missing_from_ytmusic``. The 0.4 sync then ran:
        1. ``yt_unlike(vid)`` (unavailable_video action) → YouTube state none
        2. ``ytm_like(vid)`` (possibly_missing action) → ytmusic adds vid,
           which cross-propagates back to YouTube as like → revert.

    Net effect: the ghost stayed liked AND ytmusic acquired a dead entry.
    Fix: exclude ghosts from ``possibly_missing_from_ytmusic`` (they're
    not meaningfully missing — they're dead). ``unavailable_video``
    finding (built separately by ``build_unavailable_video_items``)
    remains the sole source of truth for ghosts.
    """
    ytm: list[_FakeItem] = []
    yt = [_yt(2, "ghostvid", "Dead Song", ["Artist"], available=False)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.possibly_missing_from_ytmusic) == 0


def test_unknown_availability_still_surfaces_as_possibly_missing():
    """Defensive — ``is_available=None`` (e.g. status unchecked, ytmusic
    items without availability info) must NOT be filtered out."""
    ytm: list[_FakeItem] = []
    yt = [_yt(2, "unknownvid", "Song", ["Artist"], available=None)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.possibly_missing_from_ytmusic) == 1
    assert res.possibly_missing_from_ytmusic[0].video_id == "unknownvid"


def test_available_yt_video_surfaces_as_possibly_missing():
    """Sanity — explicit ``is_available=True`` behaves the same as
    None (current behavior — both surface)."""
    ytm: list[_FakeItem] = []
    yt = [_yt(2, "livevid", "Song", ["Artist"], available=True)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.possibly_missing_from_ytmusic) == 1


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


def test_video_id_match_when_yt_not_music_candidate():
    """Stage 1 matches a non-music-candidate YT row via video_id.

    Real example: S.I.D-Sound - 겨울요정 — classifier says not music, but
    the YTM side has the same video_id so it should pair regardless.
    """
    ytm = [_ytm(1, "v1", "겨울요정", ["S.I.D-Sound"])]
    yt = [_yt(2, "v1", "겨울요정 (MV)", ["S.I.D-Sound"], music=False)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.VIDEO_ID
    assert res.ytmusic_only_likes == []
    assert res.youtube_music_count == 0
    assert res.possibly_missing_from_ytmusic == []


def test_canonical_key_match_when_yt_not_music_candidate():
    """Stage 2 matches a non-music-candidate YT row via canonical_key."""
    ytm = [_ytm(1, "vmusic", "Song A", ["X"])]
    yt = [_yt(2, "vyoutube", "Song A", ["X"], music=False)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.CANONICAL_KEY


def test_canonical_key_collision_design_choice():
    """Two YT rows share the same canonical_key; first row wins regardless of classifier.

    This test pins the design decision: Stage 2 claims rows in enumerate(inp.youtube)
    order — not classifier-preference order. The music=False row is first and wins;
    the music=True row is unmatched and falls into possibly_missing_from_ytmusic.
    """
    ytm = [_ytm(1, "vmusic", "Song A", ["X"])]
    yt_non_music = _yt(2, "vy_non_music", "Song A", ["X"], music=False)
    yt_music = _yt(3, "vy_music", "Song A", ["X"], music=True)
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=[yt_non_music, yt_music]))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.CANONICAL_KEY
    # The music=False row (first) claimed the match.
    assert res.matched[0].youtube_track_id == 2
    assert res.ytmusic_only_likes == []
    # The music=True row is unmatched → possibly_missing_from_ytmusic.
    assert len(res.possibly_missing_from_ytmusic) == 1
    assert res.possibly_missing_from_ytmusic[0].track_id == 3


def test_fuzzy_stage_still_filters_non_music_yt():
    """Stage 3 (fuzzy) does NOT match a non-music-candidate YT row.

    "Imagine cover" vs "Imagine" with same artist would score well above 80
    threshold via token_set_ratio, but since the YT row has music=False it
    must be excluded from the fuzzy stage entirely.
    """
    ytm = [_ytm(1, "vm", "Imagine", ["John Lennon"])]
    yt = [_yt(2, "vy", "Imagine cover", ["John Lennon"], music=False)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt, fuzzy_threshold=80))
    assert res.matched == []
    assert len(res.ytmusic_only_likes) == 1
    assert res.ytmusic_only_likes[0].track_id == 1
    assert res.pointer_drift_candidates == []


def test_non_music_yt_excluded_from_possibly_missing():
    """A non-music-candidate YT row with no YTM counterpart stays out of
    possibly_missing_from_ytmusic and does not count toward youtube_music_count."""
    yt = [_yt(2, "vy", "Some random vlog", ["VlogChannel"], music=False)]
    res = compare_likes(CompareInput(ytmusic=[], youtube=yt))
    assert res.possibly_missing_from_ytmusic == []
    assert res.youtube_music_count == 0


def test_pointer_drift_candidates_collect_fuzzy_matches():
    """Fuzzy matches should also surface as pointer_drift_candidates."""
    ytm = [_ytm(1, "vm", "Imagine", ["John Lennon"])]
    yt = [_yt(2, "vy", "Imagine - John Lennon", ["RandomChannel"])]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt, fuzzy_threshold=80))
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


def test_compare_likes_persists_unavailable_video_findings(session) -> None:
    """One youtube_liked_videos snapshot with one is_available=False item
    → DiagnosisItem(issue_type='unavailable_video') is persisted."""
    from likesurgeon.cli import _compare_and_persist  # introduced in this task
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

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
    create_snapshot(
        session,
        "youtube_liked_videos",
        [
            {
                "snippet": {"title": "T", "channelTitle": "A", "resourceId": {"videoId": "v1"}},
                "contentDetails": {"videoId": "v1"},
                "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
            }
        ],
    )

    diagnosis_id = _compare_and_persist(session).diagnosis_id
    rows = (
        session.query(DiagnosisItem)
        .filter_by(diagnosis_id=diagnosis_id, issue_type=ISSUE_UNAVAILABLE_VIDEO)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].confidence == 1.0
    assert "video unavailable: deleted" in rows[0].reason


def test_compare_likes_persists_metadata_drift_findings(session) -> None:
    """Two YouTube snapshots of the same source with same video_id but
    different title → DiagnosisItem(issue_type='metadata_drift')."""
    from likesurgeon.cli import _compare_and_persist
    from likesurgeon.diagnosis import ISSUE_METADATA_DRIFT
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

    # Two YT snapshots, same video_id, different title.
    create_snapshot(
        session,
        "youtube_liked_videos",
        [
            {
                "snippet": {
                    "title": "Original Title",
                    "channelTitle": "C",
                    "resourceId": {"videoId": "v"},
                },
                "contentDetails": {"videoId": "v"},
            }
        ],
    )
    create_snapshot(
        session,
        "youtube_liked_videos",
        [
            {
                "snippet": {
                    "title": "Completely Different Now [Remastered 2024]",
                    "channelTitle": "C",
                    "resourceId": {"videoId": "v"},
                },
                "contentDetails": {"videoId": "v"},
            }
        ],
    )
    # YT Music snapshot so compare-likes has both sources to compare.
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

    diagnosis_id = _compare_and_persist(session).diagnosis_id
    rows = (
        session.query(DiagnosisItem)
        .filter_by(diagnosis_id=diagnosis_id, issue_type=ISSUE_METADATA_DRIFT)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].confidence == 1.0
    # Reason carries source, snapshot-item ids, sim, and artists.
    assert "source=youtube_liked_videos" in rows[0].reason
    assert "prev_item=" in rows[0].reason
    assert "curr_item=" in rows[0].reason
    assert "title:" in rows[0].reason


def test_compare_likes_drift_silently_skips_when_only_one_snapshot(session) -> None:
    """Source with only 1 snapshot → no drift finding for it (no error)."""
    from likesurgeon.cli import _compare_and_persist
    from likesurgeon.diagnosis import ISSUE_METADATA_DRIFT
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

    create_snapshot(
        session,
        "youtube_liked_videos",
        [
            {
                "snippet": {"title": "T", "channelTitle": "C", "resourceId": {"videoId": "v"}},
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

    diagnosis_id = _compare_and_persist(session).diagnosis_id
    rows = (
        session.query(DiagnosisItem)
        .filter_by(diagnosis_id=diagnosis_id, issue_type=ISSUE_METADATA_DRIFT)
        .all()
    )
    assert rows == []


def test_compare_likes_fails_when_a_source_has_no_snapshot(session) -> None:
    """If either source has zero snapshots, _compare_and_persist must exit
    with code 2 instead of building an empty diagnosis."""
    import typer

    from likesurgeon.cli import _compare_and_persist
    from likesurgeon.snapshot import create_snapshot

    # Only the YT Music side has a snapshot; YouTube side has none.
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

    with pytest.raises((typer.Exit, SystemExit)) as exc_info:
        _compare_and_persist(session)

    code = getattr(exc_info.value, "exit_code", None) or getattr(exc_info.value, "code", None)
    assert code == 2


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


def test_compare_likes_no_ghost_findings_for_pre_0_3_data(session) -> None:
    """Pre-0.3 snapshots have `is_available=None` (unknown). The ghost
    finder must produce zero findings for them, not flag every row.
    Regression guard: a code change that flipped the predicate to
    `is not True` would mis-classify all legacy data as ghosts."""
    from likesurgeon.cli import _compare_and_persist
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

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
    # YouTube snapshot WITHOUT `_likesurgeon_video_status` — translator
    # leaves both columns NULL, mirroring a 0.2 snapshot.
    create_snapshot(
        session,
        "youtube_liked_videos",
        [
            {
                "snippet": {
                    "title": "Legacy",
                    "channelTitle": "C",
                    "resourceId": {"videoId": "v1"},
                },
                "contentDetails": {"videoId": "v1"},
            }
        ],
    )

    diagnosis_id = _compare_and_persist(session).diagnosis_id
    rows = (
        session.query(DiagnosisItem)
        .filter_by(diagnosis_id=diagnosis_id, issue_type=ISSUE_UNAVAILABLE_VIDEO)
        .all()
    )
    assert rows == []


def test_match_evidence_defaults_to_none():
    """Match constructed without evidence kwarg has evidence=None."""
    m = Match(
        ytmusic_track_id=1,
        youtube_track_id=2,
        kind=MatchKind.VIDEO_ID,
        confidence=1.0,
        ytmusic_title="Song A",
        youtube_title="Song A (Official MV)",
    )
    assert m.evidence is None


def test_match_evidence_carries_stage4_data():
    """Match constructed with evidence round-trips Stage4Evidence fields."""
    ev = Stage4Evidence(
        channel_id="UC123",
        duration_seconds=271,
        normalized_title="title",
    )
    m = Match(
        ytmusic_track_id=1,
        youtube_track_id=2,
        kind=MatchKind.STAGE4_ENRICHMENT,
        confidence=1.0,
        ytmusic_title="Title",
        youtube_title="Title",
        evidence=ev,
    )
    assert m.evidence is not None
    assert m.evidence.channel_id == "UC123"
    assert m.evidence.duration_seconds == 271
    assert m.evidence.normalized_title == "title"


# ---------------------------------------------------------------------------
# Stage 4 pure-function tests
# ---------------------------------------------------------------------------

_META_EGAO = {
    "Gvey12GFPwU": CanonicalMetadata("Gvey12GFPwU", "えがお、み〜っけた！", "UCxxx", 271),
    "32pGBZGk4kU": CanonicalMetadata("32pGBZGk4kU", "えがお、み～っけた！", "UCxxx", 271),
}


def test_stage4_empty_metadata_returns_empty_result():
    ytm = [Stage4Candidate(0, 1, "Gvey12GFPwU", "えがお")]
    yt = [Stage4Candidate(0, 2, "32pGBZGk4kU", "えがお")]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata={})
    assert result.new_pairs == []
    assert result.consumed_original_ytm_indices == frozenset()
    assert result.consumed_original_yt_indices == frozenset()


def test_stage4_no_ytm_candidates_returns_empty_result():
    result = stage4_enrich_drift(
        ytm_candidates=[],
        yt_candidates=[Stage4Candidate(0, 2, "32pGBZGk4kU", "えがお")],
        metadata=_META_EGAO,
    )
    assert result.new_pairs == []
    assert result.consumed_original_ytm_indices == frozenset()
    assert result.consumed_original_yt_indices == frozenset()


def test_stage4_full_triple_match_promotes():
    ytm = [Stage4Candidate(0, 1, "Gvey12GFPwU", "えがお、み〜っけた！ - I found a smile!")]
    yt = [Stage4Candidate(0, 2, "32pGBZGk4kU", "えがお、み～っけた！")]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=_META_EGAO)
    assert len(result.new_pairs) == 1
    pair = result.new_pairs[0]
    assert pair.ytmusic_track_id == 1
    assert pair.youtube_track_id == 2
    assert pair.ytmusic_video_id == "Gvey12GFPwU"
    assert pair.youtube_video_id == "32pGBZGk4kU"
    assert pair.ytmusic_title == "えがお、み〜っけた！ - I found a smile!"
    assert pair.youtube_title == "えがお、み～っけた！"
    assert pair.evidence.channel_id == "UCxxx"
    assert pair.evidence.duration_seconds == 271
    assert result.consumed_original_ytm_indices == frozenset({0})
    assert result.consumed_original_yt_indices == frozenset({0})


def test_stage4_nonzero_original_indices_preserved():
    meta = {
        "vid_ytm": CanonicalMetadata("vid_ytm", "Song Title", "UCabc", 200),
        "vid_yt": CanonicalMetadata("vid_yt", "Song Title", "UCabc", 200),
    }
    ytm = [Stage4Candidate(original_index=7, track_id=10, video_id="vid_ytm", title="Song Title")]
    yt = [Stage4Candidate(original_index=42, track_id=20, video_id="vid_yt", title="Song Title")]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=meta)
    assert len(result.new_pairs) == 1
    assert result.new_pairs[0].original_ytm_index == 7
    assert result.new_pairs[0].original_yt_index == 42
    assert result.consumed_original_ytm_indices == frozenset({7})
    assert result.consumed_original_yt_indices == frozenset({42})


def test_stage4_channel_match_duration_match_but_title_mismatch_does_not_promote():
    meta = {
        "vid_ytm": CanonicalMetadata("vid_ytm", "Song Alpha", "UCabc", 200),
        "vid_yt": CanonicalMetadata("vid_yt", "Song Beta", "UCabc", 200),
    }
    ytm = [Stage4Candidate(0, 1, "vid_ytm", "Song Alpha")]
    yt = [Stage4Candidate(0, 2, "vid_yt", "Song Beta")]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=meta)
    assert result.new_pairs == []


def test_stage4_channel_match_title_match_but_duration_outside_tolerance_does_not_promote():
    meta = {
        "vid_ytm": CanonicalMetadata("vid_ytm", "Song Title", "UCabc", 271),
        "vid_yt": CanonicalMetadata("vid_yt", "Song Title", "UCabc", 280),
    }
    ytm = [Stage4Candidate(0, 1, "vid_ytm", "Song Title")]
    yt = [Stage4Candidate(0, 2, "vid_yt", "Song Title")]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=meta)
    assert result.new_pairs == []


def test_stage4_duration_within_tolerance_plus_two_promotes():
    meta = {
        "vid_ytm": CanonicalMetadata("vid_ytm", "Song Title", "UCabc", 271),
        "vid_yt": CanonicalMetadata("vid_yt", "Song Title", "UCabc", 273),
    }
    ytm = [Stage4Candidate(0, 1, "vid_ytm", "Song Title")]
    yt = [Stage4Candidate(0, 2, "vid_yt", "Song Title")]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=meta)
    assert len(result.new_pairs) == 1


def test_stage4_duration_within_tolerance_minus_two_promotes():
    meta = {
        "vid_ytm": CanonicalMetadata("vid_ytm", "Song Title", "UCabc", 273),
        "vid_yt": CanonicalMetadata("vid_yt", "Song Title", "UCabc", 271),
    }
    ytm = [Stage4Candidate(0, 1, "vid_ytm", "Song Title")]
    yt = [Stage4Candidate(0, 2, "vid_yt", "Song Title")]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=meta)
    assert len(result.new_pairs) == 1


def test_stage4_collision_two_yt_rows_same_key_does_not_promote():
    """Two yt candidates share the same (channel_id, normalized_title, duration) key — ambiguous."""
    meta = {
        "vid_ytm": CanonicalMetadata("vid_ytm", "Song Title", "UCabc", 271),
        "vid_yt1": CanonicalMetadata("vid_yt1", "Song Title", "UCabc", 271),
        "vid_yt2": CanonicalMetadata("vid_yt2", "Song Title", "UCabc", 271),
    }
    ytm = [Stage4Candidate(0, 1, "vid_ytm", "Song Title")]
    yt = [
        Stage4Candidate(0, 2, "vid_yt1", "Song Title"),
        Stage4Candidate(1, 3, "vid_yt2", "Song Title"),
    ]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=meta)
    assert result.new_pairs == []


def test_stage4_two_distinct_yt_durations_within_tolerance_window_does_not_promote():
    """yt rows at 270 and 272 same channel/title; ytm at 271 — both within ±2s — ambiguous."""
    meta = {
        "vid_ytm": CanonicalMetadata("vid_ytm", "Song Title", "UCabc", 271),
        "vid_yt1": CanonicalMetadata("vid_yt1", "Song Title", "UCabc", 270),
        "vid_yt2": CanonicalMetadata("vid_yt2", "Song Title", "UCabc", 272),
    }
    ytm = [Stage4Candidate(0, 1, "vid_ytm", "Song Title")]
    yt = [
        Stage4Candidate(0, 2, "vid_yt1", "Song Title"),
        Stage4Candidate(1, 3, "vid_yt2", "Song Title"),
    ]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=meta)
    assert result.new_pairs == []


def test_stage4_two_ytm_rows_collide_first_consumes_yt_row():
    """Two ytm rows both map to the same yt row; first promotes, second is excluded."""
    meta = {
        "vid_ytm1": CanonicalMetadata("vid_ytm1", "Song Title", "UCabc", 271),
        "vid_ytm2": CanonicalMetadata("vid_ytm2", "Song Title", "UCabc", 271),
        "vid_yt": CanonicalMetadata("vid_yt", "Song Title", "UCabc", 271),
    }
    ytm = [
        Stage4Candidate(0, 1, "vid_ytm1", "Song Title"),
        Stage4Candidate(1, 2, "vid_ytm2", "Song Title"),
    ]
    yt = [Stage4Candidate(0, 3, "vid_yt", "Song Title")]
    result = stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=meta)
    assert len(result.new_pairs) == 1
    assert result.new_pairs[0].ytmusic_track_id == 1
    assert result.consumed_original_ytm_indices == frozenset({0})
    assert result.consumed_original_yt_indices == frozenset({0})


def test_stage4_does_not_mutate_input_lists():
    ytm = [Stage4Candidate(0, 1, "Gvey12GFPwU", "えがお、み〜っけた！ - I found a smile!")]
    yt = [Stage4Candidate(0, 2, "32pGBZGk4kU", "えがお、み～っけた！")]
    original_ytm = list(ytm)
    original_yt = list(yt)
    stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=_META_EGAO)
    stage4_enrich_drift(ytm_candidates=ytm, yt_candidates=yt, metadata=_META_EGAO)
    assert ytm == original_ytm
    assert yt == original_yt


# ---------------------------------------------------------------------------
# apply_stage4_result tests
# ---------------------------------------------------------------------------


def _make_stage4_match(
    ytm_vid: str = "Gvey12GFPwU",
    yt_vid: str = "32pGBZGk4kU",
    ytm_track: int = 1,
    yt_track: int = 2,
    ytm_title: str = "YTM Title",
    yt_title: str = "YT Title",
    original_ytm_index: int = 0,
    original_yt_index: int = 0,
) -> Stage4Match:
    ev = Stage4Evidence(channel_id="UCxxx", duration_seconds=271, normalized_title="title")
    return Stage4Match(
        ytmusic_track_id=ytm_track,
        youtube_track_id=yt_track,
        ytmusic_video_id=ytm_vid,
        youtube_video_id=yt_vid,
        ytmusic_title=ytm_title,
        youtube_title=yt_title,
        evidence=ev,
        original_ytm_index=original_ytm_index,
        original_yt_index=original_yt_index,
    )


def _empty_compare_result() -> CompareResult:
    return CompareResult(ytmusic_count=0, youtube_total_count=0, youtube_music_count=0)


def test_apply_stage4_result_returns_fresh_compare_result():
    pair = _make_stage4_match()
    s4 = Stage4Result(
        new_pairs=[pair],
        consumed_original_ytm_indices=frozenset({0}),
        consumed_original_yt_indices=frozenset({0}),
    )
    cr = _empty_compare_result()
    out = apply_stage4_result(cr, s4)
    # Input unchanged.
    assert cr.matched == []
    assert cr.pointer_drift_candidates == []
    # Output is a new object.
    assert out is not cr
    assert isinstance(out, CompareResult)


def test_apply_stage4_result_constructs_match_from_stage4_match():
    pair = _make_stage4_match(ytm_track=10, yt_track=20, ytm_title="YTM", yt_title="YT")
    s4 = Stage4Result(
        new_pairs=[pair],
        consumed_original_ytm_indices=frozenset({0}),
        consumed_original_yt_indices=frozenset({0}),
    )
    out = apply_stage4_result(_empty_compare_result(), s4)

    assert len(out.matched) == 1
    assert len(out.pointer_drift_candidates) == 1
    m = out.matched[0]
    assert m.kind is MatchKind.STAGE4_ENRICHMENT
    assert m.confidence == 0.95
    assert m.ytmusic_track_id == 10
    assert m.youtube_track_id == 20
    assert m.ytmusic_title == "YTM"
    assert m.youtube_title == "YT"
    assert m.evidence is not None
    assert m.evidence.channel_id == "UCxxx"
    # Same object in both lists.
    assert out.matched[0] is out.pointer_drift_candidates[0]


def test_apply_stage4_result_removes_consumed_video_ids_from_unmatched_buckets():
    pair = _make_stage4_match(ytm_vid="vid_ytm", yt_vid="vid_yt")
    s4 = Stage4Result(
        new_pairs=[pair],
        consumed_original_ytm_indices=frozenset({0}),
        consumed_original_yt_indices=frozenset({0}),
    )
    unmatched_ytm_consumed = UnmatchedItem(1, "vid_ytm", "T", [], "k")
    unmatched_ytm_other = UnmatchedItem(2, "other_vid", "T2", [], "k2")
    unmatched_yt_consumed = UnmatchedItem(3, "vid_yt", "T3", [], "k3")
    unmatched_yt_other = UnmatchedItem(4, "another_vid", "T4", [], "k4")

    cr = CompareResult(
        ytmusic_count=2,
        youtube_total_count=2,
        youtube_music_count=2,
        ytmusic_only_likes=[unmatched_ytm_consumed, unmatched_ytm_other],
        possibly_missing_from_ytmusic=[unmatched_yt_consumed, unmatched_yt_other],
    )
    out = apply_stage4_result(cr, s4)

    # Consumed video_ids removed from each bucket.
    assert len(out.ytmusic_only_likes) == 1
    assert out.ytmusic_only_likes[0].video_id == "other_vid"
    assert len(out.possibly_missing_from_ytmusic) == 1
    assert out.possibly_missing_from_ytmusic[0].video_id == "another_vid"
    # Original unchanged.
    assert len(cr.ytmusic_only_likes) == 2
    assert len(cr.possibly_missing_from_ytmusic) == 2
