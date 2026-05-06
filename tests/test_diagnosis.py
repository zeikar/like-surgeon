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
    UnmatchedItem,
)
from likesurgeon.diagnosis import (
    ISSUE_POINTER_DRIFT,
    ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
    ISSUE_YTMUSIC_ONLY,
    DiagnosisInput,
    create_diagnosis,
    diagnosis_items,
    latest_diagnosis,
)
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
