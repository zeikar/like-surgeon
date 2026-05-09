"""Tests for the multi-source doctor health report."""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.compare import (
    CompareResult,
    Match,
    MatchKind,
    UnmatchedItem,
)
from likesurgeon.diagnosis import DiagnosisInput, create_diagnosis
from likesurgeon.doctor import health_summary
from likesurgeon.snapshot import create_snapshot


def _yt(video_id: str, title: str, channel: str = "Some Music VEVO") -> dict:
    return {
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "description": "",
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }


def _ytm(video_id: str, title: str, artists: list[str]) -> dict:
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": a} for a in artists],
    }


def test_doctor_with_no_snapshots(session: Session):
    report = health_summary(session)
    assert report.ytmusic.latest_count is None
    assert report.youtube.latest_count is None
    assert report.latest_diagnosis is None
    assert report.match_rate_percent is None


def test_doctor_aggregates_both_sources(session: Session):
    create_snapshot(
        session,
        "ytmusic_liked_songs",
        [_ytm("v1", "A", ["X"]), _ytm("v2", "B", ["Y"])],
    )
    session.commit()
    create_snapshot(
        session,
        "youtube_liked_videos",
        [_yt("v1", "A (Official Music Video)"), _yt("v3", "C (Lyrics)")],
    )
    session.commit()

    report = health_summary(session)
    assert report.ytmusic.latest_count == 2
    assert report.youtube.latest_count == 2
    assert report.latest_diagnosis is None  # no compare-likes run yet


def test_doctor_match_rate_scores_diagnosis(session: Session):
    """Real ingest path so DiagnosisItem FKs resolve under FK enforcement.

    One YT music match (v1), one YT-only (v3) → match_rate = 1/2 = 50%.
    """
    import json as _json

    from likesurgeon.snapshot import get_snapshot_items

    ytm_snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [_ytm("v1", "A", ["X"])],
    )
    session.commit()
    yt_snap = create_snapshot(
        session,
        "youtube_liked_videos",
        [
            _yt("v1", "A (Official MV)"),  # music + matched by video_id
            _yt("v3", "C (Official Audio)"),  # music + unmatched (yt-only)
        ],
    )
    session.commit()

    ytm_items = get_snapshot_items(session, ytm_snap.id)
    yt_items = get_snapshot_items(session, yt_snap.id)

    fake_result = CompareResult(
        ytmusic_count=1,
        youtube_total_count=2,
        youtube_music_count=2,
        matched=[
            Match(
                ytmusic_track_id=ytm_items[0].track_id,
                youtube_track_id=yt_items[0].track_id,
                kind=MatchKind.VIDEO_ID,
                confidence=1.0,
                ytmusic_title=ytm_items[0].title,
                youtube_title=yt_items[0].title,
            )
        ],
        possibly_missing_from_ytmusic=[
            UnmatchedItem(
                track_id=yt_items[1].track_id,
                video_id=yt_items[1].video_id,
                title=yt_items[1].title,
                artists=_json.loads(yt_items[1].artists) if yt_items[1].artists else [],
                canonical_key=yt_items[1].canonical_key,
            )
        ],
        ytmusic_only_likes=[],
        pointer_drift_candidates=[],
    )
    create_diagnosis(
        session,
        DiagnosisInput(
            ytmusic_snapshot_id=ytm_snap.id,
            youtube_snapshot_id=yt_snap.id,
            result=fake_result,
        ),
    )
    session.commit()

    report = health_summary(session)
    assert report.match_rate_percent is not None
    assert 49 <= report.match_rate_percent <= 51  # ~50%
    assert report.latest_diagnosis is not None
    assert report.latest_diagnosis.possibly_missing_from_ytmusic == 1


def test_doctor_summary_counts_new_issue_types(session) -> None:
    """`unavailable_videos` and `metadata_drift` counts come from the
    DiagnosisSummary aggregation alongside the existing types."""
    from likesurgeon.cli import _compare_and_persist
    from likesurgeon.doctor import health_summary
    from likesurgeon.snapshot import create_snapshot

    # YT Music snapshot.
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
    # Two YT snapshots: first establishes prev, second adds a ghost AND a drift.
    create_snapshot(
        session,
        "youtube_liked_videos",
        [
            {
                "snippet": {
                    "title": "Original",
                    "channelTitle": "C",
                    "resourceId": {"videoId": "v"},
                },
                "contentDetails": {"videoId": "v"},
            },
        ],
    )
    create_snapshot(
        session,
        "youtube_liked_videos",
        [
            {
                "snippet": {
                    "title": "New Title Entirely Different",
                    "channelTitle": "C",
                    "resourceId": {"videoId": "v"},
                },
                "contentDetails": {"videoId": "v"},
                "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
            },
        ],
    )

    _compare_and_persist(session)
    report = health_summary(session)
    assert report.latest_diagnosis is not None
    assert report.latest_diagnosis.unavailable_videos >= 1
    assert report.latest_diagnosis.metadata_drift >= 1
