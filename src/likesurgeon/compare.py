"""Cross-source comparison: ytmusic_liked_songs vs. youtube_liked_videos.

Pure-functional. Takes two lists of "items" (anything with the duck-typed
attributes ``track_id``, ``video_id``, ``title``, ``artists``,
``canonical_key``, and ``is_music_candidate``) and returns a structured
result. The CLI passes ``SnapshotItem`` rows directly (whose ``artists``
is a JSON-encoded string); tests pass a lightweight stand-in with
list/tuple ``artists``. ``_normalize_artists`` accepts either shape.

Three-stage matching, in order of confidence:
    1. ``video_id`` exact (highest confidence)
    2. ``canonical_key`` exact (decoration-robust)
    3. RapidFuzz fuzzy on ``"<title> | <artists>"`` (last resort)

**Multiset semantics.** A snapshot may contain duplicate rows for the same
``track_id`` (Task 6 policy in 0.1: "scans are recorded faithfully"). The
matcher pairs *rows* — not *track ids* — so if YT Music has two rows for
identity X and YouTube has one, exactly one match is reported and the
surplus YT Music row falls into ``ytmusic_only_likes``. Identity tracking
uses each input list's enumerate index internally, so callers don't need
to expose row-level ids on their items.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from rapidfuzz import fuzz


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


@dataclass(frozen=True)
class Match:
    ytmusic_track_id: int
    youtube_track_id: int
    kind: MatchKind
    confidence: float  # 1.0 for exact stages; 0.0–1.0 for fuzzy
    ytmusic_title: str
    youtube_title: str


@dataclass(frozen=True)
class UnmatchedItem:
    track_id: int
    video_id: str | None
    title: str
    artists: list[str]
    canonical_key: str


@dataclass(frozen=True)
class CompareInput:
    ytmusic: list[_Itemish]
    youtube: list[_Itemish]
    fuzzy_threshold: int = 85  # RapidFuzz score 0–100; >= threshold counts


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
    # completeness; ``issues --type ytmusic_only_likes`` filters these out.
    ytmusic_only_likes: list[UnmatchedItem] = field(default_factory=list)
    # Subset of ``matched``: rows that matched only via fuzzy stage.
    pointer_drift_candidates: list[Match] = field(default_factory=list)


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
    # Music-only YT candidates participate in matching, but the original
    # positions are preserved so non-music YT rows still count toward
    # ``youtube_total_count``.
    youtube_music: list[tuple[int, _Itemish]] = [
        (idx, it) for idx, it in enumerate(inp.youtube) if it.is_music_candidate
    ]

    matched: list[Match] = []
    used_ytm_idx: set[int] = set()
    used_yt_idx: set[int] = set()

    def _take_first_unused(candidates: list[int]) -> int | None:
        return next((i for i in candidates if i not in used_ytm_idx), None)

    def _record_match(
        yt_idx: int, ytm_idx: int, kind: MatchKind, confidence: float
    ) -> None:
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

    for yt_idx, yt in youtube_music:
        if yt_idx in used_yt_idx or not yt.video_id:
            continue
        candidates = by_video_id_ytm.get(yt.video_id)
        if not candidates:
            continue
        chosen = _take_first_unused(candidates)
        if chosen is None:
            continue
        _record_match(yt_idx, chosen, MatchKind.VIDEO_ID, 1.0)

    # Stage 2: canonical_key exact match — same queue-per-key shape.
    by_canon_ytm: dict[str, list[int]] = {}
    for idx, it in enumerate(inp.ytmusic):
        by_canon_ytm.setdefault(it.canonical_key, []).append(idx)

    for yt_idx, yt in youtube_music:
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
    possibly_missing = [
        _to_unmatched(yt)
        for yt_idx, yt in youtube_music
        if yt_idx not in used_yt_idx
    ]
    ytmusic_only = [
        _to_unmatched(ytm)
        for ytm_idx, ytm in enumerate(inp.ytmusic)
        if ytm_idx not in used_ytm_idx
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
