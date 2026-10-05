"""Diagnosis persistence: turn CompareResult into Diagnosis + DiagnosisItem rows."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .align import AlignmentResult, LMBacking, pair_passes_sanity
from .compare import CanonicalMetadata, CompareResult, Match, MatchKind, UnmatchedItem
from .drift import DriftFinding
from .models import Diagnosis, DiagnosisItem, SnapshotItem
from .normalize import normalize_for_match

ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC = "possibly_missing_from_ytmusic"
ISSUE_POINTER_DRIFT = "possible_pointer_drift"
ISSUE_YTMUSIC_ONLY = "ytmusic_only"
ISSUE_UNAVAILABLE_VIDEO = "unavailable_video"
ISSUE_METADATA_DRIFT = "metadata_drift"
ISSUE_DUPLICATE_IN_SOURCE = "duplicate_in_source"
ISSUE_TYPES = (
    ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
    ISSUE_POINTER_DRIFT,
    ISSUE_YTMUSIC_ONLY,
    ISSUE_UNAVAILABLE_VIDEO,
    ISSUE_METADATA_DRIFT,
    ISSUE_DUPLICATE_IN_SOURCE,
)
ISSUE_RELINKED = "relinked"
ISSUE_RENDERED_AS_OTHER = "rendered_as_other"
ISSUE_SHADOW_DUPLICATE = "shadow_duplicate"
ISSUE_DEAD_UNRENDERED = "dead_unrendered"
ISSUE_UNRENDERED_MUSIC = "unrendered_music"
ISSUE_UNBACKED_LM_ENTRY = "unbacked_lm_entry"
ALIGNMENT_ISSUE_TYPES = (
    ISSUE_RELINKED,
    ISSUE_RENDERED_AS_OTHER,
    ISSUE_SHADOW_DUPLICATE,
    ISSUE_DEAD_UNRENDERED,
    ISSUE_UNRENDERED_MUSIC,
    ISSUE_UNBACKED_LM_ENTRY,
    ISSUE_METADATA_DRIFT,
)

# Every Stage-4 pointer-drift reason starts with this. ``sync.plan`` keys
# auto-apply eligibility on it: Stage-4 pairs (channel + duration + title all
# agree) are auto-applied by default, while Stage-3 fuzzy pairs are title-only
# — ``token_set_ratio`` scores a version variant ("Song" vs "Song (Remix)")
# 100 — so they need an explicit opt-in.
STAGE4_REASON_PREFIX = "enriched"


def is_stage4_drift_reason(reason: str) -> bool:
    return reason.startswith(STAGE4_REASON_PREFIX)


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


def _match_reason(match: Match) -> str:
    if match.kind is MatchKind.STAGE4_ENRICHMENT:
        ev = match.evidence
        if ev is None:
            return f"{STAGE4_REASON_PREFIX} drift match"
        via = "" if ev.fuzzy_score is None else f" (verified fuzzy {ev.fuzzy_score:.0f}/100)"
        return (
            f"{STAGE4_REASON_PREFIX}{via}: channel={ev.channel_id[:8]}… "
            f"+ duration={ev.duration_seconds}s + normalized title match"
        )
    return (
        f"fuzzy match (score {match.confidence * 100:.0f}/100): "
        f"YT '{match.youtube_title}' ↔ YT Music '{match.ytmusic_title}'"
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
        reason=_match_reason(match),
        source_track_id=match.youtube_track_id,
        related_track_id=match.ytmusic_track_id,
        status="open",
    )


def _unmatched_reason(item: UnmatchedItem) -> str:
    artists = ", ".join(item.artists) if item.artists else "(no artists)"
    return f"'{item.title}' by {artists}"


def build_unavailable_video_items(
    diagnosis_id: int,
    snapshot_items: list[SnapshotItem],
    *,
    exclude_yt_indices: frozenset[int] | None = None,
) -> list[DiagnosisItem]:
    """Build DiagnosisItem rows for SnapshotItems with is_available=False.

    ``confidence=1.0`` because availability is a fact, not a probability —
    using a lower confidence would let ``--min-confidence`` filters hide
    real ghosts.

    ``exclude_yt_indices`` skips the items at the given positions in
    ``snapshot_items`` (by enumerate index). Stage 4 passes its
    ``consumed_original_yt_indices`` here so promoted drift rows don't
    also surface as ghost findings.

    ``region_blocked`` videos are skipped — the video still exists and may
    become available again when the region restriction lifts (or the user
    travels / VPNs). Unliking these would permanently lose the like if the
    restriction is later removed. Only genuinely-unavailable reasons
    (deleted, private, rejected, unavailable, missing_from_videos_list)
    produce ghost findings.
    """
    out: list[DiagnosisItem] = []
    for i, item in enumerate(snapshot_items):
        if exclude_yt_indices is not None and i in exclude_yt_indices:
            continue
        if item.is_available is not False:
            continue
        if item.unavailable_reason == "region_blocked":
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


def _availability_text(item: SnapshotItem) -> str:
    if item.is_available is None:
        return "availability unknown"
    if item.is_available:
        return "available"
    return f"unavailable: {item.unavailable_reason}" if item.unavailable_reason else "unavailable"


def _same_recording_text(
    sanity: bool, a_meta: CanonicalMetadata | None, b_meta: CanonicalMetadata | None
) -> str:
    if a_meta is None or b_meta is None:
        return "no videos.list metadata"
    parts = [f"\u0394{abs(a_meta.duration_seconds - b_meta.duration_seconds)}s"]
    if a_meta.channel_id == b_meta.channel_id:
        parts.append("same channel")
    else:
        parts.append("different channel")
        a_title, b_title = normalize_for_match(a_meta.title), normalize_for_match(b_meta.title)
        overlap = bool(a_title and b_title) and (a_title in b_title or b_title in a_title)
        parts.append("titles overlap" if overlap else "titles differ")
    return f"{'passed' if sanity else 'failed'}, {', '.join(parts)}"


def _alignment_pair_reason(
    b: LMBacking,
    sanity: bool,
    a_meta: CanonicalMetadata | None,
    b_meta: CanonicalMetadata | None,
) -> str:
    a, lm = b.ll_item, b.lm_item
    check = _same_recording_text(sanity, a_meta, b_meta)
    if a_meta is None or b_meta is None:
        verdict = f"report-only: {check}"
        check = "not run"
    elif not sanity:
        verdict = "report-only: same-recording check failed"
    elif a.is_available is None:
        verdict = "report-only: availability unknown"
    else:
        verdict = None
    reason = (
        f"YouTube video {a.video_id} '{a.title}' (LL #{b.ll_index}, {_availability_text(a)}) "
        f"shows in YT Music as {lm.video_id} '{lm.title}' (LM #{b.lm_index}); "
        f"same-recording check: {check}"
    )
    return f"{reason}; {verdict}" if verdict else reason


def build_alignment_items(
    diagnosis_id: int,
    result: AlignmentResult,
    *,
    metadata: dict[str, CanonicalMetadata],
) -> list[DiagnosisItem]:
    """Build DiagnosisItem rows from an LL→LM ``AlignmentResult`` (spec §4).

    ``rendered`` backings (A = LL video, B = the LM entry's video) classify as
    ``shadow_duplicate`` (B is itself liked on YouTube), ``relinked`` (A is
    unavailable) or ``rendered_as_other``.

    A finding is **write-eligible** iff its ``issue_type`` is one of those
    three AND ``confidence == 1.0``: the pair passed ``pair_passes_sanity`` on
    the ``videos.list`` metadata and A's availability is known; otherwise 0.5
    (report-only). The sync planner keys on this. ``unbacked_lm_entry``,
    ``dead_unrendered`` and ``unrendered_music`` are facts, so 1.0, but they
    are report-only types and never actionable regardless of confidence.
    """
    ll_ids = {it.video_id for it in result.ll if it.video_id}
    out: list[DiagnosisItem] = []

    def add(
        issue: str,
        confidence: float,
        reason: str,
        source_track_id: int,
        related_track_id: int | None = None,
    ) -> None:
        out.append(
            DiagnosisItem(
                diagnosis_id=diagnosis_id,
                issue_type=issue,
                confidence=confidence,
                reason=reason,
                source_track_id=source_track_id,
                related_track_id=related_track_id,
                status="open",
            )
        )

    for b in result.backings:
        if b.kind == "unbacked":
            add(
                ISSUE_UNBACKED_LM_ENTRY,
                1.0,
                f"YT Music entry {b.lm_item.video_id} '{b.lm_item.title}' (LM #{b.lm_index}) "
                "has no determinable YouTube like behind it",
                b.lm_item.track_id,
            )
        elif b.kind == "rendered":
            a, lm = b.ll_item, b.lm_item
            a_meta = metadata.get(a.video_id)
            b_meta = metadata.get(lm.video_id)
            sanity = pair_passes_sanity(a_meta, b_meta)
            eligible = sanity and a.is_available is not None
            # B already liked on YouTube wins over relinked: the fix is then
            # removing A, not re-pointing it.
            if lm.video_id in ll_ids:
                issue = ISSUE_SHADOW_DUPLICATE
            elif a.is_available is False:
                issue = ISSUE_RELINKED
            else:
                issue = ISSUE_RENDERED_AS_OTHER
            add(
                issue,
                1.0 if eligible else 0.5,
                _alignment_pair_reason(b, sanity, a_meta, b_meta),
                a.track_id,
                lm.track_id,
            )

    for i, it in enumerate(result.ll):
        if i in result.rendered_ll or i in result.ambiguous_ll:
            continue
        where = f"YouTube video {it.video_id} '{it.title}' (LL #{i}"
        if it.is_available is False:
            add(
                ISSUE_DEAD_UNRENDERED,
                1.0,
                f"{where}, {_availability_text(it)}) is not shown in YT Music",
                it.track_id,
            )
        elif it.is_available is True and it.is_music_candidate:
            add(
                ISSUE_UNRENDERED_MUSIC,
                1.0,
                f"{where}) looks like music but is not shown in YT Music",
                it.track_id,
            )
    return out


def create_alignment_diagnosis(
    session: Session,
    *,
    ytmusic_snapshot_id: int | None,
    youtube_snapshot_id: int | None,
    result: AlignmentResult,
    metadata: dict[str, CanonicalMetadata],
) -> Diagnosis:
    """Persist a Diagnosis row plus the items from ``build_alignment_items``."""
    diagnosis = Diagnosis(
        ytmusic_snapshot_id=ytmusic_snapshot_id, youtube_snapshot_id=youtube_snapshot_id
    )
    session.add(diagnosis)
    session.flush()
    session.add_all(build_alignment_items(diagnosis.id, result, metadata=metadata))
    session.flush()
    return diagnosis


def latest_diagnosis(session: Session) -> Diagnosis | None:
    stmt = select(Diagnosis).order_by(Diagnosis.created_at.desc(), Diagnosis.id.desc()).limit(1)
    return session.scalar(stmt)


def carry_over_skipped(session: Session, diagnosis: Diagnosis) -> int:
    """Re-apply earlier diagnoses' terminal ``'skipped'`` statuses to the
    matching findings of ``diagnosis``. Returns how many were carried.

    Every ``compare-likes`` run writes fresh ``'open'`` items and ``sync``
    only reads the latest diagnosis, so without this a ``'skipped'`` — set
    by ``sync`` on a cross-prop verify-miss, or by ``likesurgeon skip`` —
    would silently re-open on the next run and re-fire the same writes.
    A finding matches on ``(issue_type, source_track_id, related_track_id)``,
    and its status is taken from the most recent earlier diagnosis that
    contains it — so a gap (e.g. a ``scan --limit`` run that missed it)
    doesn't drop the skip, while flipping the item to ``'open'`` on the
    latest diagnosis still un-skips it for good.
    """
    rows = session.execute(
        select(
            DiagnosisItem.issue_type,
            DiagnosisItem.source_track_id,
            DiagnosisItem.related_track_id,
            DiagnosisItem.status,
        )
        .join(Diagnosis, DiagnosisItem.diagnosis_id == Diagnosis.id)
        .where(Diagnosis.id != diagnosis.id)
        .order_by(Diagnosis.created_at.desc(), Diagnosis.id.desc())
    )
    last_status: dict[tuple[str, int | None, int | None], str] = {}
    for issue_type, source_id, related_id, status in rows:
        last_status.setdefault((issue_type, source_id, related_id), status)
    carried = 0
    for it in session.scalars(
        select(DiagnosisItem).where(
            DiagnosisItem.diagnosis_id == diagnosis.id, DiagnosisItem.status == "open"
        )
    ):
        if last_status.get((it.issue_type, it.source_track_id, it.related_track_id)) == "skipped":
            it.status = "skipped"
            carried += 1
    return carried


def diagnosis_items(session: Session, diagnosis_id: int) -> list[DiagnosisItem]:
    stmt = (
        select(DiagnosisItem)
        .where(DiagnosisItem.diagnosis_id == diagnosis_id)
        .order_by(DiagnosisItem.confidence.desc(), DiagnosisItem.id)
    )
    return list(session.scalars(stmt).all())
