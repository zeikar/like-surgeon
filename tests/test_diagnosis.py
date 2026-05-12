"""Tests for diagnosis persistence.

These tests use real ``create_snapshot`` calls (rather than fake Track ids)
so that the ``DiagnosisItem.source_track_id`` / ``related_track_id`` foreign
keys resolve under FK enforcement (Task 1 enabled ``PRAGMA foreign_keys=ON``).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.compare import (
    CompareResult,
    Match,
    MatchKind,
    Stage4Evidence,
    UnmatchedItem,
)
from likesurgeon.diagnosis import (
    ISSUE_DUPLICATE_IN_SOURCE,
    ISSUE_POINTER_DRIFT,
    ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
    ISSUE_YTMUSIC_ONLY,
    DiagnosisInput,
    _match_reason,
    build_duplicate_in_source_items,
    create_diagnosis,
    diagnosis_items,
    latest_diagnosis,
)
from likesurgeon.models import Diagnosis
from likesurgeon.snapshot import create_snapshot, get_snapshot_items


def _ytm_item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def _yt_item(video_id: str, title: str, channel: str = "ArtistVEVO") -> dict:
    return {
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "description": "",
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }


def _summary(item) -> UnmatchedItem:
    import json as _json

    return UnmatchedItem(
        track_id=item.track_id,
        video_id=item.video_id,
        title=item.title,
        artists=_json.loads(item.artists) if item.artists else [],
        canonical_key=item.canonical_key,
    )


def test_create_diagnosis_persists_each_bucket(session: Session):
    """Real Track rows, real ids in the synthetic CompareResult, FK happy."""
    ytm_snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("v_match", "Imagine", ["Lennon"]),
            _ytm_item("v_ytm_only", "YT Music Only", ["Artist"]),
        ],
    )
    yt_snap = create_snapshot(
        session,
        "youtube_liked_videos",
        [
            _yt_item("v_yt_match", "Imagine - John Lennon (Official MV)", "Lennon"),
            _yt_item("v_yt_only", "Cover Song (Official Audio)", "CoverArtist"),
        ],
    )
    session.commit()

    ytm_items = get_snapshot_items(session, ytm_snap.id)
    yt_items = get_snapshot_items(session, yt_snap.id)

    drift_match = Match(
        ytmusic_track_id=ytm_items[0].track_id,
        youtube_track_id=yt_items[0].track_id,
        kind=MatchKind.FUZZY,
        confidence=0.88,
        ytmusic_title=ytm_items[0].title,
        youtube_title=yt_items[0].title,
    )

    result = CompareResult(
        ytmusic_count=2,
        youtube_total_count=2,
        youtube_music_count=2,
        matched=[drift_match],
        possibly_missing_from_ytmusic=[_summary(yt_items[1])],
        ytmusic_only_likes=[_summary(ytm_items[1])],
        pointer_drift_candidates=[drift_match],
    )

    diag = create_diagnosis(
        session,
        DiagnosisInput(
            ytmusic_snapshot_id=ytm_snap.id,
            youtube_snapshot_id=yt_snap.id,
            result=result,
        ),
    )
    session.commit()

    items = diagnosis_items(session, diag.id)
    by_type: dict[str, list] = {}
    for it in items:
        by_type.setdefault(it.issue_type, []).append(it)

    assert ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC in by_type
    assert ISSUE_POINTER_DRIFT in by_type
    assert ISSUE_YTMUSIC_ONLY in by_type
    assert len(by_type[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC]) == 1
    assert len(by_type[ISSUE_POINTER_DRIFT]) == 1
    assert len(by_type[ISSUE_YTMUSIC_ONLY]) == 1
    # Pointer-drift item should reference both tracks (real ids).
    drift = by_type[ISSUE_POINTER_DRIFT][0]
    assert drift.source_track_id == yt_items[0].track_id
    assert drift.related_track_id == ytm_items[0].track_id
    # All items default to status='open'.
    assert all(it.status == "open" for it in items)


def _make_empty_diagnosis(session: Session) -> Diagnosis:
    diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
    session.add(diag)
    session.flush()
    return diag


def test_build_duplicate_in_source_items_emits_one_per_duplicated_video_id(
    session: Session,
):
    """Snapshot with two items sharing the same video_id yields exactly ONE
    DiagnosisItem per duplicated video_id — never additive on top of
    existing buckets."""
    snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("dup_vid", "Day by Day", ["X"]),
            _ytm_item("unique_vid", "Other", ["Y"]),
            _ytm_item("dup_vid", "Day by Day", ["X"]),
        ],
    )
    session.commit()
    diag = _make_empty_diagnosis(session)
    items = get_snapshot_items(session, snap.id)

    result = build_duplicate_in_source_items(diag.id, items, "ytmusic_liked_songs")

    assert len(result) == 1
    finding = result[0]
    assert finding.issue_type == ISSUE_DUPLICATE_IN_SOURCE
    assert finding.confidence == 1.0
    # source_track_id resolves to the lowest-position row (position=0).
    dup_rows_sorted = sorted(
        [it for it in items if it.video_id == "dup_vid"], key=lambda it: it.position
    )
    assert finding.source_track_id == dup_rows_sorted[0].track_id
    assert finding.related_track_id is None
    assert finding.status == "open"
    assert "appears 2 times" in finding.reason
    # Positions show in ascending order.
    expected_positions = ", ".join(str(it.position) for it in dup_rows_sorted)
    assert expected_positions in finding.reason


def test_build_duplicate_in_source_items_no_duplicates_returns_empty(
    session: Session,
):
    snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("a", "A", ["X"]),
            _ytm_item("b", "B", ["Y"]),
        ],
    )
    session.commit()
    diag = _make_empty_diagnosis(session)
    items = get_snapshot_items(session, snap.id)

    assert build_duplicate_in_source_items(diag.id, items, "ytmusic_liked_songs") == []


def test_build_duplicate_in_source_items_skips_null_video_ids(session: Session):
    """Rows whose video_id is NULL share no identity — they can't be
    duplicates of each other."""
    # ytmusic items without a videoId still translate to SnapshotItem
    # rows (the translator falls back to ``canon:<key>`` for dedupe_key
    # but leaves SnapshotItem.video_id = None).
    snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            {"title": "Untitled A", "artists": [{"name": "X"}]},
            {"title": "Untitled B", "artists": [{"name": "Y"}]},
        ],
    )
    session.commit()
    diag = _make_empty_diagnosis(session)
    items = get_snapshot_items(session, snap.id)
    # Sanity: both rows have NULL video_id.
    assert all(it.video_id is None for it in items)

    assert build_duplicate_in_source_items(diag.id, items, "ytmusic_liked_songs") == []


def test_build_duplicate_in_source_items_picks_lowest_position_first(
    session: Session,
):
    """Three rows with the same video_id at distinct positions: builder
    must internally sort by position so ``source_track_id`` resolves to
    the lowest-position row's track and the reason lists positions in
    ascending order regardless of caller's input order."""
    snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("v", "A", ["X"]),  # position 0
            _ytm_item("v", "A", ["X"]),  # position 1
            _ytm_item("v", "A", ["X"]),  # position 2
        ],
    )
    session.commit()
    diag = _make_empty_diagnosis(session)
    items = get_snapshot_items(session, snap.id)
    # Shuffle the caller-provided list so we can prove the builder
    # doesn't trust input order.
    shuffled = [items[2], items[0], items[1]]

    result = build_duplicate_in_source_items(diag.id, shuffled, "ytmusic_liked_songs")

    assert len(result) == 1
    finding = result[0]
    # source_track_id == track_id of the position-0 row.
    by_position = sorted(items, key=lambda it: it.position)
    assert finding.source_track_id == by_position[0].track_id
    # Positions render in ascending order in the reason text.
    assert "positions: 0, 1, 2" in finding.reason
    assert "appears 3 times" in finding.reason


def test_latest_diagnosis_orders_by_recency(session: Session):
    """Two empty diagnoses on empty snapshots; latest returns the newer one."""
    ytm_snap = create_snapshot(session, "ytmusic_liked_songs", [])
    yt_snap = create_snapshot(session, "youtube_liked_videos", [])
    session.commit()

    empty = CompareResult(ytmusic_count=0, youtube_total_count=0, youtube_music_count=0)
    inp = DiagnosisInput(
        ytmusic_snapshot_id=ytm_snap.id,
        youtube_snapshot_id=yt_snap.id,
        result=empty,
    )
    first = create_diagnosis(session, inp)
    session.commit()
    second = create_diagnosis(session, inp)
    session.commit()
    assert latest_diagnosis(session).id == second.id
    assert second.id != first.id


def _stage4_match(evidence: Stage4Evidence | None) -> Match:
    """Minimal Match for STAGE4_ENRICHMENT reason-builder tests (no DB needed)."""
    return Match(
        ytmusic_track_id=1,
        youtube_track_id=2,
        kind=MatchKind.STAGE4_ENRICHMENT,
        confidence=1.0,
        ytmusic_title="えがお、み~っけた！",
        youtube_title="えがお、み~っけた！ (Official MV)",
        evidence=evidence,
    )


def test_diagnosis_reason_for_stage4_drift_describes_evidence():
    ev = Stage4Evidence(
        channel_id="UCabcdefghij1234",
        duration_seconds=271,
        normalized_title="えがお、み~っけた！",
    )
    reason = _match_reason(_stage4_match(ev))

    assert "UCabcdef" in reason
    assert "271s" in reason
    assert "normalized title match" in reason
    assert "fuzzy" not in reason


def test_diagnosis_reason_for_stage_2_3_fuzzy_drift_unchanged():
    match = Match(
        ytmusic_track_id=1,
        youtube_track_id=2,
        kind=MatchKind.FUZZY,
        confidence=0.92,
        ytmusic_title="Imagine",
        youtube_title="Imagine - John Lennon (Official MV)",
    )
    reason = _match_reason(match)

    assert reason.startswith("fuzzy match (score ")


def test_diagnosis_reason_for_stage4_without_evidence_falls_back():
    reason = _match_reason(_stage4_match(evidence=None))

    assert reason == "enriched drift match"
