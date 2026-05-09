"""Tests for config.py — region validation and config.json loader."""

from __future__ import annotations

import pytest


def test_validate_region_accepts_uppercase_alpha_2() -> None:
    from likesurgeon.config import _validate_region

    assert _validate_region("KR") == "KR"


def test_validate_region_normalizes_case_and_whitespace() -> None:
    from likesurgeon.config import _validate_region

    assert _validate_region("  kr  ") == "KR"


def test_validate_region_rejects_three_letter_code() -> None:
    from likesurgeon.config import InvalidRegionError, _validate_region

    with pytest.raises(InvalidRegionError):
        _validate_region("KOR")


def test_validate_region_rejects_full_country_name() -> None:
    from likesurgeon.config import InvalidRegionError, _validate_region

    with pytest.raises(InvalidRegionError):
        _validate_region("Korea")


def test_validate_region_rejects_empty_after_strip() -> None:
    from likesurgeon.config import InvalidRegionError, _validate_region

    with pytest.raises(InvalidRegionError):
        _validate_region("   ")


def test_validate_region_rejects_embedded_whitespace_or_punct() -> None:
    from likesurgeon.config import InvalidRegionError, _validate_region

    for bogus in ("K R", "K\nR", "K1"):
        with pytest.raises(InvalidRegionError):
            _validate_region(bogus)
