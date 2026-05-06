"""Diagnosis persistence: turn CompareResult into Diagnosis + DiagnosisItem rows."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .compare import CompareResult, Match, UnmatchedItem
from .models import Diagnosis, DiagnosisItem

ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC = "possibly_missing_from_ytmusic"
ISSUE_POINTER_DRIFT = "possible_pointer_drift"
ISSUE_YTMUSIC_ONLY = "ytmusic_only"


@dataclass(frozen=True)
class DiagnosisInput:
    ytmusic_snapshot_id: int | None
    youtube_snapshot_id: int | None
    result: CompareResult


def create_diagnosis(session: Session, inp: DiagnosisInput) -> Diagnosis:
    """Persist a Diagnosis row plus one DiagnosisItem per finding."""
    diagnosis = Diagnosis(
        ytmusic_snapshot_id=inp.ytmusic_snapshot_id,
        youtube_snapshot_id=inp.youtube_snapshot_id,
    )
    session.add(diagnosis)
    session.flush()

    # High-priority bucket: the user liked it on YouTube but YT Music doesn't
    # have it — these are the candidates the "backup" half of the project
    # should surface most loudly.
    for unmatched in inp.result.possibly_missing_from_ytmusic:
        session.add(
            _unmatched_item(
                diagnosis.id,
                unmatched,
                ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
                0.9,
            )
        )

    for match in inp.result.pointer_drift_candidates:
        session.add(_match_item(diagnosis.id, match, ISSUE_POINTER_DRIFT))

    # Low-priority informational bucket: YT Music has it, YouTube doesn't.
    # Often this is just "user never liked it on YouTube" rather than a
    # problem, so the confidence is intentionally low.
    for unmatched in inp.result.ytmusic_only_likes:
        session.add(
            _unmatched_item(
                diagnosis.id,
                unmatched,
                ISSUE_YTMUSIC_ONLY,
                0.5,
            )
        )

    session.flush()
    return diagnosis


def _unmatched_item(
    diagnosis_id: int,
    item: UnmatchedItem,
    issue_type: str,
    confidence: float,
) -> DiagnosisItem:
    return DiagnosisItem(
        diagnosis_id=diagnosis_id,
        issue_type=issue_type,
        confidence=confidence,
        reason=_unmatched_reason(item),
        source_track_id=item.track_id,
        related_track_id=None,
        status="open",
    )


def _match_item(
    diagnosis_id: int,
    match: Match,
    issue_type: str,
) -> DiagnosisItem:
    return DiagnosisItem(
        diagnosis_id=diagnosis_id,
        issue_type=issue_type,
        confidence=match.confidence,
        reason=(
            f"fuzzy match (score {match.confidence * 100:.0f}/100): "
            f"YT '{match.youtube_title}' ↔ YT Music '{match.ytmusic_title}'"
        ),
        source_track_id=match.youtube_track_id,
        related_track_id=match.ytmusic_track_id,
        status="open",
    )


def _unmatched_reason(item: UnmatchedItem) -> str:
    artists = ", ".join(item.artists) if item.artists else "(no artists)"
    return f"'{item.title}' by {artists}"


def latest_diagnosis(session: Session) -> Diagnosis | None:
    stmt = select(Diagnosis).order_by(Diagnosis.created_at.desc(), Diagnosis.id.desc()).limit(1)
    return session.scalar(stmt)


def diagnosis_items(session: Session, diagnosis_id: int) -> list[DiagnosisItem]:
    stmt = (
        select(DiagnosisItem)
        .where(DiagnosisItem.diagnosis_id == diagnosis_id)
        .order_by(DiagnosisItem.confidence.desc(), DiagnosisItem.id)
    )
    return list(session.scalars(stmt).all())
