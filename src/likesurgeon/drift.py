"""Snapshot-pair drift detection.

Pure module: takes two `SnapshotItem` lists (prev and curr from the same
source) and returns `DriftFinding` rows where same-`video_id` items have
meaningfully different metadata. Used by `compare-likes` to surface
"the underlying video was retitled / channel renamed / `-Topic`
migrated" between scans.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from rapidfuzz.fuzz import token_sort_ratio

_TITLE_SIMILARITY_THRESHOLD = 0.90


@dataclass(frozen=True)
class DriftFinding:
    """One same-video_id pair where metadata moved between snapshots."""

    source: str
    video_id: str
    prev_snapshot_item_id: int
    curr_snapshot_item_id: int
    prev_title: str
    curr_title: str
    prev_artists: tuple[str, ...]
    curr_artists: tuple[str, ...]
    title_similarity: float
    artists_changed: bool


def _decode_artists(value: Any) -> tuple[str, ...]:
    """SnapshotItem.artists is a JSON-encoded list[str]; decode it
    defensively (NULL, empty string, malformed → empty tuple)."""
    if not value:
        return ()
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(x) for x in decoded)


def _normalize_artists_set(artists: tuple[str, ...]) -> frozenset[str]:
    """Order-independent normalised artist comparison: lowercase + strip,
    drop empties, return as a frozenset so swap-only diffs (`['A','B']`
    vs `['B','A']`) are not flagged as drift."""
    return frozenset(a.strip().lower() for a in artists if a.strip())


def detect_drift(prev: list[Any], curr: list[Any], *, source: str) -> list[DriftFinding]:
    """Return drift findings for items present in both snapshots by video_id.

    A finding is emitted when EITHER:
      * `token_sort_ratio(prev.title, curr.title) / 100 < 0.90` — the title
        moved beyond cosmetic punctuation/whitespace tolerance, OR
      * the (set-normalised) artists list changed at all — channel
        renames, `- Topic` migrations, attribution edits.

    Items present in only one of `prev` / `curr` are not drift (those are
    new likes / unlikes, surfaced elsewhere). Items lacking `video_id`
    (rare YT Music edge case where the only handle is `canonical_key`)
    can't be drift-keyed and are skipped.
    """
    prev_by_id = {p.video_id: p for p in prev if p.video_id}
    findings: list[DriftFinding] = []
    for c in curr:
        if not c.video_id:
            continue
        p = prev_by_id.get(c.video_id)
        if p is None:
            continue
        title_sim = token_sort_ratio(p.title, c.title) / 100.0
        prev_artists = _decode_artists(p.artists)
        curr_artists = _decode_artists(c.artists)
        artists_changed = _normalize_artists_set(prev_artists) != _normalize_artists_set(
            curr_artists
        )
        if title_sim < _TITLE_SIMILARITY_THRESHOLD or artists_changed:
            findings.append(
                DriftFinding(
                    source=source,
                    video_id=c.video_id,
                    prev_snapshot_item_id=p.id,
                    curr_snapshot_item_id=c.id,
                    prev_title=p.title,
                    curr_title=c.title,
                    prev_artists=prev_artists,
                    curr_artists=curr_artists,
                    title_similarity=title_sim,
                    artists_changed=artists_changed,
                )
            )
    return findings
