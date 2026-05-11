"""Diagnosis persistence: turn CompareResult into Diagnosis + DiagnosisItem rows."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .compare import CompareResult, Match, UnmatchedItem
from .drift import DriftFinding
from .models import Diagnosis, DiagnosisItem, SnapshotItem

ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC = "possibly_missing_from_ytmusic"
ISSUE_POINTER_DRIFT = "possible_pointer_drift"
ISSUE_YTMUSIC_ONLY = "ytmusic_only"
ISSUE_UNAVAILABLE_VIDEO = "unavailable_video"
ISSUE_METADATA_DRIFT = "metadata_drift"
ISSUE_DUPLICATE_IN_SOURCE = "duplicate_in_source"


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


def build_unavailable_video_items(
    diagnosis_id: int, snapshot_items: list[SnapshotItem]
) -> list[DiagnosisItem]:
    """Build DiagnosisItem rows for SnapshotItems with is_available=False.

    ``confidence=1.0`` because availability is a fact, not a probability —
    using a lower confidence would let ``--min-confidence`` filters hide
    real ghosts.
    """
    out: list[DiagnosisItem] = []
    for item in snapshot_items:
        if item.is_available is not False:
            continue
        out.append(
            DiagnosisItem(
                diagnosis_id=diagnosis_id,
                issue_type=ISSUE_UNAVAILABLE_VIDEO,
                confidence=1.0,
                reason=f"video unavailable: {item.unavailable_reason}",
                source_track_id=item.track_id,
                related_track_id=None,
                status="open",
            )
        )
    return out


def build_metadata_drift_items(
    diagnosis_id: int,
    findings: list[DriftFinding],
    curr_items: list[SnapshotItem],
) -> list[DiagnosisItem]:
    """Build DiagnosisItem rows from DriftFinding objects.

    ``confidence=1.0`` because each finding is a deterministic post-filter
    output (the threshold check happened in ``detect_drift``). Severity goes
    in ``reason`` text, NOT in ``confidence`` — using ``title_similarity``
    as confidence would invert ``--min-confidence`` semantics (lower sim =
    bigger drift = would get filtered out).

    ``related_track_id`` is left ``None`` because the prev/curr ``Track``
    rows are usually the same row (Tracks get upserted with latest metadata
    on every scan). The durable prev/curr identifiers are the
    ``SnapshotItem`` ids embedded in ``reason``.
    """
    curr_by_item_id = {it.id: it for it in curr_items}
    out: list[DiagnosisItem] = []
    for f in findings:
        curr = curr_by_item_id.get(f.curr_snapshot_item_id)
        track_id = curr.track_id if curr is not None else None
        reason = (
            f"source={f.source}; "
            f"prev_item={f.prev_snapshot_item_id}, curr_item={f.curr_snapshot_item_id}; "
            f"title: {f.prev_title!r} → {f.curr_title!r} (sim {f.title_similarity:.2f}); "
            f"artists: {list(f.prev_artists)} → {list(f.curr_artists)}"
        )
        out.append(
            DiagnosisItem(
                diagnosis_id=diagnosis_id,
                issue_type=ISSUE_METADATA_DRIFT,
                confidence=1.0,
                reason=reason,
                source_track_id=track_id,
                related_track_id=None,
                status="open",
            )
        )
    return out


def build_duplicate_in_source_items(
    diagnosis_id: int,
    snapshot_items: list[SnapshotItem],
    source: str,
) -> list[DiagnosisItem]:
    """Build DiagnosisItem rows for within-source duplicate ``video_id`` groups.

    ``confidence=1.0`` because a duplicate row is a deterministic fact about
    the snapshot — there's no probability to surface. Filtering with
    ``--min-confidence`` should never hide a real duplicate.

    Rows with falsy ``video_id`` are skipped (no identity to dedupe). For
    each duplicated ``video_id`` group, the group is sorted by ``position``
    ascending and ``source_track_id`` resolves to the position-1 row's
    ``track_id`` so output is deterministic regardless of caller input order.
    """
    groups: dict[str, list[SnapshotItem]] = {}
    for item in snapshot_items:
        if not item.video_id:
            continue
        groups.setdefault(item.video_id, []).append(item)

    out: list[DiagnosisItem] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        sorted_group = sorted(group, key=lambda it: it.position)
        positions = ", ".join(str(it.position) for it in sorted_group)
        reason = f"appears {len(sorted_group)} times in {source} snapshot (positions: {positions})"
        out.append(
            DiagnosisItem(
                diagnosis_id=diagnosis_id,
                issue_type=ISSUE_DUPLICATE_IN_SOURCE,
                confidence=1.0,
                reason=reason,
                source_track_id=sorted_group[0].track_id,
                related_track_id=None,
                status="open",
            )
        )
    return out


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
