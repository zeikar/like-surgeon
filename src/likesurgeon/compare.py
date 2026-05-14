"""Cross-source comparison: ytmusic_liked_songs vs. youtube_liked_videos.

Pure-functional. Takes two lists of "items" (anything with the duck-typed
attributes ``track_id``, ``video_id``, ``title``, ``artists``,
``canonical_key``, and ``is_music_candidate``) and returns a structured
result. The CLI passes ``SnapshotItem`` rows directly (whose ``artists``
is a JSON-encoded string); tests pass a lightweight stand-in with
list/tuple ``artists``. ``_normalize_artists`` accepts either shape.

Three-stage matching, in order of confidence:
    1. ``video_id`` exact (highest confidence) — two-pass: music candidates
       first, then all YouTube rows as a fallback. Lets classifier mis-fires
       (e.g. artist names with hyphens) still match via exact video_id, while
       preserving music-candidate priority on collisions.
    2. ``canonical_key`` exact (decoration-robust) — same two-pass shape.
    3. RapidFuzz fuzzy on ``"<title> | <artists>"`` (last resort) —
       classifier-filtered (``is_music_candidate=True`` only).

**Multiset semantics.** A snapshot may contain duplicate rows for the same
``track_id`` (Task 6 policy in 0.1: "scans are recorded faithfully"). The
matcher pairs *rows* — not *track ids* — so if YT Music has two rows for
identity X and YouTube has one, exactly one match is reported and the
surplus YT Music row falls into ``ytmusic_only_likes``. Identity tracking
uses each input list's enumerate index internally, so callers don't need
to expose row-level ids on their items.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from rapidfuzz import fuzz

from .normalize import normalize_for_match


class _Itemish(Protocol):
    track_id: int
    video_id: str | None
    title: str
    # ``artists`` is JSON-encoded ``str`` when this is a ``SnapshotItem`` and
    # a plain ``list``/``tuple`` of strings in the test stand-in.
    # ``_normalize_artists`` decodes both.
    artists: Any
    canonical_key: str
    is_music_candidate: bool | None
    # ``is_available`` is only populated for ``youtube_liked_videos`` items
    # (via stage-1 ``videos.list`` ghost detection). ``True`` = playable,
    # ``False`` = ghost (deleted/private/region-blocked), ``None`` = unknown
    # (e.g. unchecked, or ytmusic items where availability isn't tracked).
    is_available: bool | None


class _VideoIdPositioned(Protocol):
    """Minimal duck-typed contract for ``dedupe_by_video_id``.

    Keeps ``compare.py`` decoupled from the SQLAlchemy ORM — both real
    ``SnapshotItem`` rows and lightweight test stand-ins satisfy this.
    """

    video_id: str | None
    position: int


_T = TypeVar("_T", bound=_VideoIdPositioned)


def dedupe_by_video_id(items: Sequence[_T]) -> list[_T]:
    """Return one row per ``video_id`` (lowest-position winner), NULL-safe.

    Contract:
      - Rows with falsy ``video_id`` pass through unchanged (no identity).
      - For each non-null ``video_id`` group, keep the row with the
        smallest ``position``; discard the rest.
      - Output is sorted by ``position`` ascending (position-stable).
      - Idempotent: running twice returns an equal list.
    """
    kept: dict[str, _T] = {}
    no_id: list[_T] = []
    for it in items:
        if not it.video_id:
            no_id.append(it)
            continue
        existing = kept.get(it.video_id)
        if existing is None or it.position < existing.position:
            kept[it.video_id] = it
    combined: list[_T] = list(kept.values()) + no_id
    combined.sort(key=lambda x: x.position)
    return combined


def _normalize_artists(value: Any) -> list[str]:
    """Return a flat ``list[str]`` regardless of source-shape.

    Accepts:
      - JSON-encoded strings (e.g. ``'["X", "Y"]'`` from ``SnapshotItem.artists``)
      - already-decoded ``list``/``tuple`` of strings (test stand-in)
      - ``None`` / falsy → ``[]``
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(v) for v in decoded] if isinstance(decoded, list) else []
    return [str(v) for v in value]


class MatchKind(StrEnum):
    VIDEO_ID = "video_id"
    CANONICAL_KEY = "canonical_key"
    FUZZY = "fuzzy"
    STAGE4_ENRICHMENT = "stage4_enrichment"


@dataclass(frozen=True)
class Stage4Evidence:
    """Evidence captured by Stage 4 drift detection — used for diagnosis reason strings."""

    channel_id: str  # videos.list snippet.channelId of the matched pair
    duration_seconds: int  # ytmusic-side duration; yt-side is within ±2s of this
    normalized_title: str  # normalize_for_match(canonical_title)


@dataclass(frozen=True)
class Stage4Candidate:
    """Stage 4 input — primitive data carrier (no SnapshotItem dependency)."""

    original_index: int  # index into the FULL ytm_items / yt_items list
    track_id: int  # used to construct Match downstream
    video_id: str  # used for metadata lookup + UnmatchedItem bucket filter
    title: str  # used for Match construction (raw snapshot title)


@dataclass(frozen=True)
class Stage4Match:
    """One promoted drift pair from Stage 4. Carries all fields needed to:
    (a) construct a Match downstream, and
    (b) filter UnmatchedItem buckets + ghost findings by video_id / original_index.
    """

    ytmusic_track_id: int
    youtube_track_id: int
    ytmusic_video_id: str
    youtube_video_id: str
    ytmusic_title: str
    youtube_title: str
    evidence: Stage4Evidence
    original_ytm_index: int
    original_yt_index: int


@dataclass(frozen=True)
class Stage4Result:
    """Output of stage4_enrich_drift."""

    new_pairs: list[Stage4Match]
    consumed_original_ytm_indices: frozenset[int]
    consumed_original_yt_indices: frozenset[int]


@dataclass(frozen=True)
class Match:
    ytmusic_track_id: int
    youtube_track_id: int
    kind: MatchKind
    confidence: float  # 1.0 for exact stages; 0.0–1.0 for fuzzy
    ytmusic_title: str
    youtube_title: str
    evidence: Stage4Evidence | None = None


@dataclass(frozen=True)
class UnmatchedItem:
    track_id: int
    video_id: str | None
    title: str
    artists: list[str]
    canonical_key: str


DEFAULT_FUZZY_THRESHOLD: int = 85


@dataclass(frozen=True)
class CompareInput:
    ytmusic: list[_Itemish]
    youtube: list[_Itemish]
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD  # RapidFuzz score 0–100; >= threshold counts


@dataclass(frozen=True)
class CompareResult:
    ytmusic_count: int
    youtube_total_count: int
    youtube_music_count: int

    matched: list[Match] = field(default_factory=list)
    # Music-candidate YT videos with no match in YT Music — i.e. likes the
    # user expressed on YouTube that aren't represented on YouTube Music.
    # This is the high-priority bucket: the "backup half" of like-surgeon.
    possibly_missing_from_ytmusic: list[UnmatchedItem] = field(default_factory=list)
    # YT Music tracks with no corresponding YT video. Lower priority — most
    # users don't mirror every YT Music like to YouTube. Surfaced for
    # completeness; ``issues --type ytmusic_only`` filters these out (the
    # persisted ``DiagnosisItem.issue_type`` value is ``ytmusic_only``).
    ytmusic_only_likes: list[UnmatchedItem] = field(default_factory=list)
    # Subset of ``matched``: rows that matched only via fuzzy stage.
    pointer_drift_candidates: list[Match] = field(default_factory=list)


@dataclass(frozen=True)
class CanonicalMetadata:
    """Authoritative metadata for a YouTube video, fetched via videos.list.

    Distinct from playlistItems.list data — at the videos.list endpoint,
    snippet.channelId IS the video uploader (there's no videoOwnerChannelId
    field). The playlistItems-side equivalent is snippet.videoOwnerChannelId
    (snippet.channelId there is the playlist owner, NOT the uploader).
    """

    video_id: str
    title: str
    channel_id: str
    duration_seconds: int


class MetadataLookup(Protocol):
    def fetch_canonical_metadata(self, video_ids: list[str]) -> dict[str, CanonicalMetadata]: ...


def _to_unmatched(item: _Itemish) -> UnmatchedItem:
    return UnmatchedItem(
        track_id=item.track_id,
        video_id=item.video_id,
        title=item.title,
        artists=_normalize_artists(item.artists),
        canonical_key=item.canonical_key,
    )


def _fuzz_target(item: _Itemish) -> str:
    artists = _normalize_artists(item.artists)
    return f"{item.title} | {', '.join(artists)}"


def compare_likes(inp: CompareInput) -> CompareResult:
    """Three-stage matcher with row-level identity.

    Each input *row* (tracked via the input list's enumerate index) is
    matched at most once, so duplicate rows in either source — same
    ``track_id``, same ``video_id``, same ``canonical_key`` — produce
    surplus that lands in the unmatched buckets rather than getting
    silently collapsed.
    """
    # Two iteration lists over inp.youtube:
    #   youtube_music — classifier-filtered (is_music_candidate=True). Used by
    #                   Stage 3 (fuzzy), possibly_missing, youtube_music_count,
    #                   AND as the *first pass* of Stages 1 & 2.
    #   youtube_all   — every row. Used as the *fallback pass* of Stages 1 & 2
    #                   so videos misclassified by the music-candidate filter
    #                   (e.g. artist names containing hyphens) are still
    #                   matched when an exact video_id or canonical_key exists
    #                   in YT Music. Running youtube_music first ensures a
    #                   classifier-true row wins a same-key collision against
    #                   a classifier-false row, preventing a real music
    #                   candidate from leaking into possibly_missing — which
    #                   is sync-actionable and would trigger a spurious
    #                   ytm_like on the unrelated video_id.
    # Original enumerate indices are preserved so both lists share the same
    # index space and non-music rows still count toward youtube_total_count.
    youtube_all: list[tuple[int, _Itemish]] = list(enumerate(inp.youtube))
    youtube_music: list[tuple[int, _Itemish]] = [
        (idx, it) for idx, it in enumerate(inp.youtube) if it.is_music_candidate
    ]

    matched: list[Match] = []
    used_ytm_idx: set[int] = set()
    used_yt_idx: set[int] = set()

    def _take_first_unused(candidates: list[int]) -> int | None:
        return next((i for i in candidates if i not in used_ytm_idx), None)

    def _record_match(yt_idx: int, ytm_idx: int, kind: MatchKind, confidence: float) -> None:
        ytm = inp.ytmusic[ytm_idx]
        yt = inp.youtube[yt_idx]
        matched.append(
            Match(
                ytmusic_track_id=ytm.track_id,
                youtube_track_id=yt.track_id,
                kind=kind,
                confidence=confidence,
                ytmusic_title=ytm.title,
                youtube_title=yt.title,
            )
        )
        used_ytm_idx.add(ytm_idx)
        used_yt_idx.add(yt_idx)

    # Stage 1: video_id exact match. Index is a queue per video_id so two
    # ytm rows with the same video_id can pair against two distinct yt rows.
    by_video_id_ytm: dict[str, list[int]] = {}
    for idx, it in enumerate(inp.ytmusic):
        if it.video_id:
            by_video_id_ytm.setdefault(it.video_id, []).append(idx)

    for source in (youtube_music, youtube_all):
        for yt_idx, yt in source:
            if yt_idx in used_yt_idx or not yt.video_id:
                continue
            candidates = by_video_id_ytm.get(yt.video_id)
            if not candidates:
                continue
            chosen = _take_first_unused(candidates)
            if chosen is None:
                continue
            _record_match(yt_idx, chosen, MatchKind.VIDEO_ID, 1.0)

    # Stage 2: canonical_key exact match — same queue-per-key shape, same
    # music-first / all-fallback ordering as Stage 1.
    by_canon_ytm: dict[str, list[int]] = {}
    for idx, it in enumerate(inp.ytmusic):
        by_canon_ytm.setdefault(it.canonical_key, []).append(idx)

    for source in (youtube_music, youtube_all):
        for yt_idx, yt in source:
            if yt_idx in used_yt_idx:
                continue
            candidates = by_canon_ytm.get(yt.canonical_key)
            if not candidates:
                continue
            chosen = _take_first_unused(candidates)
            if chosen is None:
                continue
            _record_match(yt_idx, chosen, MatchKind.CANONICAL_KEY, 1.0)

    # Stage 3: RapidFuzz fuzzy on "title | artists". Best-of-remaining per
    # yt row, ties go to the highest score. Each ytm row reserved on use.
    threshold = inp.fuzzy_threshold
    for yt_idx, yt in youtube_music:
        if yt_idx in used_yt_idx:
            continue
        best: tuple[float, int] | None = None
        for ytm_idx, ytm in enumerate(inp.ytmusic):
            if ytm_idx in used_ytm_idx:
                continue
            score = fuzz.token_set_ratio(_fuzz_target(yt), _fuzz_target(ytm))
            if score >= threshold and (best is None or score > best[0]):
                best = (float(score), ytm_idx)
        if best is not None:
            score, ytm_idx = best
            _record_match(yt_idx, ytm_idx, MatchKind.FUZZY, score / 100.0)

    # Build unmatched lists at row level — surplus rows survive intact.
    # Exclude YouTube ghosts (``is_available is False``) from possibly_missing:
    # those are dead videos the user wants gone (unavailable_video finding),
    # not "missing from ytmusic" candidates. Surfacing them here triggered a
    # destructive conflict in 0.4-0.6 sync — ``yt_unlike`` would unlike the
    # ghost, then ``ytm_like`` on the same vid would cross-propagate the
    # like back to YouTube via ytmusic, reverting the unlike. See
    # ``docs/ARCHITECTURE.md`` (0.6.1 fix).
    possibly_missing = [
        _to_unmatched(yt)
        for yt_idx, yt in youtube_music
        if yt_idx not in used_yt_idx and getattr(yt, "is_available", None) is not False
    ]
    ytmusic_only = [
        _to_unmatched(ytm) for ytm_idx, ytm in enumerate(inp.ytmusic) if ytm_idx not in used_ytm_idx
    ]
    pointer_drift = [m for m in matched if m.kind is MatchKind.FUZZY]

    return CompareResult(
        ytmusic_count=len(inp.ytmusic),
        youtube_total_count=len(inp.youtube),
        youtube_music_count=len(youtube_music),
        matched=matched,
        possibly_missing_from_ytmusic=possibly_missing,
        ytmusic_only_likes=ytmusic_only,
        pointer_drift_candidates=pointer_drift,
    )


def stage4_enrich_drift(
    *,
    ytm_candidates: list[Stage4Candidate],
    yt_candidates: list[Stage4Candidate],
    metadata: dict[str, CanonicalMetadata],
) -> Stage4Result:
    """Pure post-process drift detection — see plan v2-r3 Locked decision 6."""
    if not metadata or not ytm_candidates:
        return Stage4Result(
            new_pairs=[],
            consumed_original_ytm_indices=frozenset(),
            consumed_original_yt_indices=frozenset(),
        )

    # Build yt-side index: (channel_id, normalized_title) → list[(yt_candidate, duration_seconds)]
    yt_index: dict[tuple[str, str], list[tuple[Stage4Candidate, int]]] = {}
    for c in yt_candidates:
        meta = metadata.get(c.video_id)
        if meta is None or not meta.channel_id:
            continue
        norm_title = normalize_for_match(meta.title)
        if not norm_title:  # safety: skip if normalization produces empty
            continue
        key = (meta.channel_id, norm_title)
        yt_index.setdefault(key, []).append((c, meta.duration_seconds))

    new_pairs: list[Stage4Match] = []
    consumed_ytm: set[int] = set()
    consumed_yt: set[int] = set()

    for ytm_cand in ytm_candidates:
        meta = metadata.get(ytm_cand.video_id)
        if meta is None or not meta.channel_id:
            continue
        norm_title = normalize_for_match(meta.title)
        if not norm_title:
            continue
        key = (meta.channel_id, norm_title)
        yt_entries = yt_index.get(key, [])

        # Filter to ±2s tolerance, dedupe by original_index, exclude already-consumed.
        in_tolerance: dict[int, tuple[Stage4Candidate, int]] = {}
        for yt_cand, yt_dur in yt_entries:
            if yt_cand.original_index in consumed_yt:
                continue
            if abs(yt_dur - meta.duration_seconds) > 2:
                continue
            in_tolerance[yt_cand.original_index] = (yt_cand, yt_dur)

        if len(in_tolerance) != 1:
            continue  # 0 candidates or ambiguous (2+)

        yt_cand, yt_dur = next(iter(in_tolerance.values()))
        evidence = Stage4Evidence(
            channel_id=meta.channel_id,
            duration_seconds=meta.duration_seconds,
            normalized_title=norm_title,
        )
        new_pairs.append(
            Stage4Match(
                ytmusic_track_id=ytm_cand.track_id,
                youtube_track_id=yt_cand.track_id,
                ytmusic_video_id=ytm_cand.video_id,
                youtube_video_id=yt_cand.video_id,
                ytmusic_title=ytm_cand.title,
                youtube_title=yt_cand.title,
                evidence=evidence,
                original_ytm_index=ytm_cand.original_index,
                original_yt_index=yt_cand.original_index,
            )
        )
        consumed_ytm.add(ytm_cand.original_index)
        consumed_yt.add(yt_cand.original_index)

    return Stage4Result(
        new_pairs=new_pairs,
        consumed_original_ytm_indices=frozenset(consumed_ytm),
        consumed_original_yt_indices=frozenset(consumed_yt),
    )


def apply_stage4_result(
    compare_result: CompareResult,
    stage4: Stage4Result,
) -> CompareResult:
    """Apply Stage 4 promotions to a CompareResult, returning a fresh instance.

    No SnapshotItem dependency — all fields needed come from Stage4Match.
    """
    if not stage4.new_pairs:
        return compare_result

    # Build Match objects from Stage4Match.
    new_matches: list[Match] = []
    for pair in stage4.new_pairs:
        new_matches.append(
            Match(
                ytmusic_track_id=pair.ytmusic_track_id,
                youtube_track_id=pair.youtube_track_id,
                kind=MatchKind.STAGE4_ENRICHMENT,
                confidence=0.95,
                ytmusic_title=pair.ytmusic_title,
                youtube_title=pair.youtube_title,
                evidence=pair.evidence,
            )
        )

    # Pre-compute consumed video_id sets for UnmatchedItem filtering.
    consumed_ytm_vids = {p.ytmusic_video_id for p in stage4.new_pairs}
    consumed_yt_vids = {p.youtube_video_id for p in stage4.new_pairs}

    # Filter UnmatchedItem buckets.
    filtered_possibly_missing = [
        u
        for u in compare_result.possibly_missing_from_ytmusic
        if u.video_id not in consumed_yt_vids
    ]
    filtered_ytmusic_only = [
        u for u in compare_result.ytmusic_only_likes if u.video_id not in consumed_ytm_vids
    ]

    return dataclasses.replace(
        compare_result,
        matched=list(compare_result.matched) + new_matches,
        pointer_drift_candidates=list(compare_result.pointer_drift_candidates) + new_matches,
        possibly_missing_from_ytmusic=filtered_possibly_missing,
        ytmusic_only_likes=filtered_ytmusic_only,
    )
