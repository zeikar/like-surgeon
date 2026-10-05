"""LL → LM order alignment: which YouTube like backs each YT Music Liked songs entry.

Pure-functional. See ``docs/design/ll-lm-alignment.md`` §2–§3.

YT Music Liked songs (LM) is a rendering of YouTube Liked videos (LL): same
order, filtered to what YT Music shows, each entry displayed as its playable
track — whose ``videoId`` (B) may differ from the LL video (A) that backs it.
LM exposes no backing id, so order is the only signal:

1. **Anchors** — LM entries whose ``videoId`` is itself in LL, restricted to a
   maximum-length subsequence strictly increasing in LL index (LIS; a greedy
   pass collapses on real data). When an anchored video has several LM copies
   in its anchor window, the copy that leaves the fewest unbacked entries wins.
2. **Gaps** — between consecutive anchors, the *k* non-anchor LM entries are
   renderings of the *m* LL videos in the same stretch, in order. Only *k* = *m*
   pins down which is which; any other gap leaves its LM entries unbacked.

Inputs are the raw snapshot rows (``SnapshotItem`` or a test stand-in), each
list ordered by ``position``, newest first. Do not dedupe LM first: a video
shown twice is how a shadow like behind B shows up.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal, Protocol

from .compare import CanonicalMetadata
from .normalize import normalize_for_match


class _LMItem(Protocol):
    video_id: str | None


class _LLItem(_LMItem, Protocol):
    # Not read by ``align``; declared so callers reading LL items back off the
    # result (``AlignmentResult.ll``, ``LMBacking.ll_item``) see the LL-only fields.
    is_available: bool | None
    unavailable_reason: str | None
    is_music_candidate: bool | None


BackingKind = Literal["self", "rendered", "unbacked"]


@dataclass(frozen=True)
class LMBacking:
    """The LL video backing one LM entry.

    ``self``: the entry shows its own LL video (an anchor). ``rendered``: the
    entry shows ``lm_item`` (B) on behalf of ``ll_item`` (A). ``unbacked``: its
    gap couldn't be pinned down (``ll_index`` / ``ll_item`` are None).
    """

    lm_index: int
    lm_item: _LMItem
    ll_index: int | None
    ll_item: _LLItem | None
    kind: BackingKind


@dataclass(frozen=True)
class AlignmentResult:
    backings: list[LMBacking]  # one per LM entry, in LM order
    rendered_ll: frozenset[int]  # LL indices backing at least one LM entry
    # LL indices that may be rendered but can't be placed — in a gap whose LM
    # entries are unbacked, or shown by an LM copy that didn't anchor — so
    # "not rendered" can't be claimed either. Disjoint from ``rendered_ll``.
    ambiguous_ll: frozenset[int]
    anchor_count: int
    # The LL input, so callers can resolve the index sets above without
    # threading the list alongside the result.
    ll: tuple[_LLItem, ...] = field(repr=False, compare=False)

    def unrendered_ll(self) -> list[_LLItem]:
        """LL videos that no LM entry renders, in LL order."""
        return [
            it
            for i, it in enumerate(self.ll)
            if i not in self.rendered_ll and i not in self.ambiguous_ll
        ]


def _anchors(candidates: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest subsequence of ``(lm_index, ll_index)`` strictly increasing in ll_index.

    Patience LIS, O(n log n). On an equal tail the earlier candidate is kept;
    which LM copy of a duplicated video anchors is settled by ``_settle_copies``.
    """
    tails: list[int] = []  # tails[p]: smallest ll_index ending a run of length p + 1
    tail_at: list[int] = []  # candidate index of that run's last element
    prev: list[int | None] = [None] * len(candidates)
    for ci, (_, i) in enumerate(candidates):
        p = bisect_left(tails, i)
        if p < len(tails) and tails[p] == i:
            continue
        prev[ci] = tail_at[p - 1] if p else None
        if p == len(tails):
            tails.append(i)
            tail_at.append(ci)
        else:
            tails[p] = i
            tail_at[p] = ci

    out: list[tuple[int, int]] = []
    ci_next = tail_at[-1] if tail_at else None
    while ci_next is not None:
        out.append(candidates[ci_next])
        ci_next = prev[ci_next]
    out.reverse()
    return out


def _pairable(lm: Sequence[_LMItem], gap_lm: range, gap_ll: range) -> bool:
    """Whether a gap's LM entries pair one-to-one, in order, with its LL videos."""
    # An LM row without a video_id has no B to show, so its gap can't be pinned down.
    return len(gap_lm) == len(gap_ll) and all(lm[j].video_id for j in gap_lm)


def _unbacked_count(lm: Sequence[_LMItem], lo: tuple[int, int], hi: tuple[int, int]) -> int:
    """LM entries left unbacked by the gap strictly between anchors ``lo`` and ``hi``."""
    gap_lm = range(lo[0] + 1, hi[0])
    return 0 if _pairable(lm, gap_lm, range(lo[1] + 1, hi[1])) else len(gap_lm)


def _settle_copies(
    lm: Sequence[_LMItem], ll_of: dict[int, int], bounds: list[tuple[int, int]]
) -> None:
    """Pick which LM copy of each anchored video anchors, in place.

    Any copy lying strictly between the neighbouring anchors keeps the LIS
    equally long. Which one is the video's own like depends on whether it was
    liked before or after the shadow rendered as it, so neither the earliest
    nor the latest copy is right on its own: take the copy that leaves the
    fewest unbacked LM entries in the two adjacent gaps (ties → earliest).
    ``bounds`` is the anchor list with the list-end sentinels around it.
    """
    for a in range(1, len(bounds) - 1):
        lo, (_, i), hi = bounds[a - 1], bounds[a], bounds[a + 1]
        copies = [j for j in range(lo[0] + 1, hi[0]) if ll_of.get(j) == i]
        if len(copies) > 1:
            _, best = min(
                (_unbacked_count(lm, lo, (j, i)) + _unbacked_count(lm, (j, i), hi), j)
                for j in copies
            )
            bounds[a] = (best, i)


def align(ll: Sequence[_LLItem], lm: Sequence[_LMItem]) -> AlignmentResult:
    """Back every LM entry with an LL video (or none); see the module docstring."""
    first_ll_index: dict[str, int] = {}
    for i, it in enumerate(ll):
        # NULL-safe: rows without a video_id have no identity to anchor on.
        if it.video_id:
            first_ll_index.setdefault(it.video_id, i)

    candidates = [
        (j, first_ll_index[it.video_id]) for j, it in enumerate(lm) if it.video_id in first_ll_index
    ]
    # Sentinels close the leading and trailing gaps against the list ends.
    bounds = [(-1, -1), *_anchors(candidates), (len(lm), len(ll))]
    _settle_copies(lm, dict(candidates), bounds)

    backings: list[LMBacking] = []
    rendered: set[int] = set()
    ambiguous: set[int] = set()
    for (j0, i0), (j1, i1) in pairwise(bounds):
        gap_lm = range(j0 + 1, j1)
        # Anchors strictly increase in LL index, so nothing in here is anchored.
        gap_ll = range(i0 + 1, i1)
        if gap_lm and _pairable(lm, gap_lm, gap_ll):
            for j, i in zip(gap_lm, gap_ll, strict=True):
                backings.append(LMBacking(j, lm[j], i, ll[i], "rendered"))
            rendered.update(gap_ll)
        elif gap_lm:
            backings.extend(LMBacking(j, lm[j], None, None, "unbacked") for j in gap_lm)
            ambiguous.update(gap_ll)
        if j1 < len(lm):
            backings.append(LMBacking(j1, lm[j1], i1, ll[i1], "self"))
            rendered.add(i1)

    # An LM copy that didn't anchor still shows its LL video somewhere; we just
    # can't say where, so that video mustn't be reported as unrendered.
    anchored_lm = {j for j, _ in bounds[1:-1]}
    ambiguous.update(i for j, i in candidates if j not in anchored_lm)

    return AlignmentResult(
        backings=backings,
        rendered_ll=frozenset(rendered),
        ambiguous_ll=frozenset(ambiguous - rendered),
        anchor_count=len(bounds) - 2,
        ll=tuple(ll),
    )


# Duration tolerance between A and B for a write-eligible pair, in seconds.
_DURATION_TOLERANCE = 3


def pair_passes_sanity(a_meta: CanonicalMetadata | None, b_meta: CanonicalMetadata | None) -> bool:
    """Whether an aligned A → B pair looks like the same recording (spec §3.3).

    Order alone decides the pairing; this ``videos.list`` check only guards
    the writes. Missing metadata fails, so the pair stays report-only.
    """
    if a_meta is None or b_meta is None:
        return False
    if abs(a_meta.duration_seconds - b_meta.duration_seconds) > _DURATION_TOLERANCE:
        return False
    if a_meta.channel_id == b_meta.channel_id:
        return True
    a_title = normalize_for_match(a_meta.title)
    b_title = normalize_for_match(b_meta.title)
    return bool(a_title and b_title) and (a_title in b_title or b_title in a_title)
