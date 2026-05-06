"""Title/artist normalization to compute canonical match keys.

Pure-functional. No I/O. Used by snapshot ingestion and (later) the matching
engine in MVP 0.3.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Noise patterns that decorate titles but don't change song identity.
# Match as whole words; case folding happens before the regex runs.
_NOISE_PATTERNS: tuple[str, ...] = (
    r"\bofficial\s+music\s+video\b",
    r"\bofficial\s+mv\b",
    r"\bofficial\s+video\b",
    r"\bofficial\s+audio\b",
    r"\blyric\s+video\b",
    r"\blyrics\b",
    r"\bvisualizer\b",
    r"\baudio\b",
    r"\bm/v\b",
    r"\bmv\b",
)
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS))
_BRACKET_RE = re.compile(r"[\(\[\{][^()\[\]\{\}]*[\)\]\}]")
_WS_RE = re.compile(r"\s+")
_TRIM_CHARS = " -–—\t"


def _strip_noise_brackets(text: str) -> str:
    """Remove bracketed segments that contain a noise word; keep the rest."""

    def _replace(match: re.Match[str]) -> str:
        return " " if _NOISE_RE.search(match.group(0)) else match.group(0)

    return _BRACKET_RE.sub(_replace, text)


def normalize_title(title: str) -> str:
    """Lowercase, strip noise decorations, collapse whitespace."""
    s = title.lower()
    s = _strip_noise_brackets(s)
    s = _NOISE_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip(_TRIM_CHARS)
    return s


def normalize_artists(artists: Iterable[str]) -> str:
    """Lowercase, trim, drop empties, sort, comma-join.

    Sorting makes the key order-independent so collaborations match
    regardless of which artist YouTube lists first.
    """
    parts = [_WS_RE.sub(" ", a.lower().strip()) for a in artists if a is not None]
    parts = [p for p in parts if p]
    parts.sort()
    return ", ".join(parts)


def canonical_key(title: str, artists: Iterable[str]) -> str:
    """Deterministic identity key: ``"<artists>|<title>"``."""
    return f"{normalize_artists(artists)}|{normalize_title(title)}"
