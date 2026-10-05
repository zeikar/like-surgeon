"""Builders shared by the ``sync`` planner and dispatcher tests: diagnoses with
pair findings (A = the LL video, B = the LM entry) and the actions for them."""

from __future__ import annotations

from itertools import count

from sqlalchemy.orm import Session

from likesurgeon.diagnosis import ISSUE_RELINKED
from likesurgeon.models import Diagnosis, DiagnosisItem, Track
from likesurgeon.sync import PlannedAction

_track_ids = count()


def _make_track(session: Session, video_id: str | None) -> Track:
    """A minimal Track row; ``video_id`` None models a track sync can't address."""
    n = next(_track_ids)
    track = Track(
        source="youtube_liked_videos",
        video_id=video_id,
        title=f"t-{n}",
        artists="[]",
        canonical_key=f"k-{n}",
        dedupe_key=f"d-{n}",
    )
    session.add(track)
    session.flush()
    return track


def make_diagnosis(session: Session) -> Diagnosis:
    diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
    session.add(diag)
    session.flush()
    return diag


def pair(
    session: Session,
    diag: Diagnosis,
    issue_type: str = ISSUE_RELINKED,
    *,
    a: str | None = "A",
    b: str | None = "B",
    confidence: float = 1.0,
    status: str = "open",
) -> DiagnosisItem:
    """A finding pairing LL video ``a`` (source) with the LM entry ``b`` (related)."""
    item = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type=issue_type,
        confidence=confidence,
        reason="diagnosis-time evidence",
        source_track_id=_make_track(session, a).id,
        related_track_id=_make_track(session, b).id,
        status=status,
    )
    session.add(item)
    session.flush()
    return item


def repoint(item: DiagnosisItem, a: str = "A", b: str = "B") -> PlannedAction:
    return PlannedAction(item.id, "repoint", a_video_id=a, b_video_id=b)


def unlike_shadow(item: DiagnosisItem, a: str = "A", b: str = "B") -> PlannedAction:
    return PlannedAction(item.id, "unlike_shadow", a_video_id=a, b_video_id=b)
