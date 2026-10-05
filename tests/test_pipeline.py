"""Tests for the compare-likes pipeline (``pipeline.compare_and_persist``)."""

from __future__ import annotations

import pytest


def test_pipeline_persists_metadata_drift_findings(session) -> None:
    """Two YouTube snapshots of the same source with same video_id but
    different title → DiagnosisItem(issue_type='metadata_drift')."""
    from likesurgeon.diagnosis import ISSUE_METADATA_DRIFT
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.pipeline import compare_and_persist
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

    diagnosis_id = compare_and_persist(session).diagnosis_id
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


def test_pipeline_drift_silently_skips_when_only_one_snapshot(session) -> None:
    """Source with only 1 snapshot → no drift finding for it (no error)."""
    from likesurgeon.diagnosis import ISSUE_METADATA_DRIFT
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.pipeline import compare_and_persist
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

    diagnosis_id = compare_and_persist(session).diagnosis_id
    rows = (
        session.query(DiagnosisItem)
        .filter_by(diagnosis_id=diagnosis_id, issue_type=ISSUE_METADATA_DRIFT)
        .all()
    )
    assert rows == []


def test_compare_and_persist_raises_when_a_source_has_no_snapshot(session) -> None:
    """If either source has zero snapshots, compare_and_persist must raise
    instead of building an empty diagnosis."""
    from likesurgeon.pipeline import MissingSnapshotsError, compare_and_persist
    from likesurgeon.snapshot import create_snapshot

    # Only the YT Music side has a snapshot; YouTube side has none.
    create_snapshot(
        session,
        "ytmusic_liked_songs",
        [{"videoId": "ytm", "title": "T", "artists": [{"name": "A"}]}],
    )

    with pytest.raises(MissingSnapshotsError, match="scan youtube-likes"):
        compare_and_persist(session)
