"""Tests for the multi-source doctor health report."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from likesurgeon.align import align
from likesurgeon.diagnosis import (
    ALIGNMENT_ISSUE_TYPES,
    ISSUE_RELINKED,
    ISSUE_UNBACKED_LM_ENTRY,
    create_alignment_diagnosis,
)
from likesurgeon.doctor import health_summary
from likesurgeon.models import Diagnosis, DiagnosisItem
from likesurgeon.snapshot import create_snapshot, get_snapshot_items


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
    assert report.lm_backed_percent is None


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


def _alignment_diagnosis(session: Session, *, lm_ids: tuple[str, ...]) -> Diagnosis:
    """Align LL [a1, A, a3, C, a5] (A deleted) against ``lm_ids`` and persist it."""
    ytm = create_snapshot(session, "ytmusic_liked_songs", [_ytm(v, v, ["X"]) for v in lm_ids])
    ll = [_yt(v, v) for v in ("a1", "A", "a3", "C", "a5")]
    ll[1]["_likesurgeon_video_status"] = {"is_available": False, "reason": "deleted"}
    yt = create_snapshot(session, "youtube_liked_videos", ll)
    result = align(get_snapshot_items(session, yt.id), get_snapshot_items(session, ytm.id))
    return create_alignment_diagnosis(
        session,
        ytmusic_snapshot_id=ytm.id,
        youtube_snapshot_id=yt.id,
        result=result,
        metadata={},
    )


def test_doctor_counts_alignment_findings_and_lm_backed_share(session: Session):
    # A renders as B; the a3–a5 gap shows two LM entries for one LL video, so
    # neither can be backed.
    diag = _alignment_diagnosis(session, lm_ids=("a1", "B", "a3", "D1", "D2", "a5"))
    # Issue types from a pre-alignment diagnosis aren't counted.
    session.add(
        DiagnosisItem(diagnosis_id=diag.id, issue_type="ytmusic_only", confidence=0.5, reason="r")
    )
    session.commit()

    report = health_summary(session)

    assert report.latest_diagnosis is not None
    assert report.latest_diagnosis.diagnosis_id == diag.id
    assert report.latest_diagnosis.counts == {
        **dict.fromkeys(ALIGNMENT_ISSUE_TYPES, 0),
        ISSUE_RELINKED: 1,
        ISSUE_UNBACKED_LM_ENTRY: 2,
    }
    # 1 − 2 unbacked / 6 LM entries.
    assert report.lm_backed_percent == pytest.approx(100 * 4 / 6)


def test_doctor_lm_backed_share_is_none_without_lm_snapshot_or_entries(session: Session):
    diag = _alignment_diagnosis(session, lm_ids=())
    session.commit()
    # An empty LM has nothing to back.
    assert health_summary(session).lm_backed_percent is None

    diag.ytmusic_snapshot_id = None  # LM snapshot deleted (FK is SET NULL)
    session.commit()
    report = health_summary(session)
    assert report.latest_diagnosis is not None
    assert report.lm_backed_percent is None
