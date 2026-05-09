"""Tests for config.py — region validation and config.json loader."""

from __future__ import annotations

import json
from pathlib import Path

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


def _write_config(app_dir: Path, payload: object) -> None:
    """Helper: write `payload` to <app_dir>/config.json, creating dir."""
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "config.json").write_text(
        json.dumps(payload) if not isinstance(payload, str) else payload,
        encoding="utf-8",
    )


def test_load_region_returns_none_when_config_missing(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    assert _load_region(tmp_path) is None


def test_load_region_reads_region_from_config_json(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    _write_config(tmp_path, {"region": "KR"})
    assert _load_region(tmp_path) == "KR"


def test_load_region_normalizes_case(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    _write_config(tmp_path, {"region": "kr"})
    assert _load_region(tmp_path) == "KR"


def test_load_region_returns_none_for_malformed_json(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    _write_config(tmp_path, "{not valid json")
    assert _load_region(tmp_path) is None


def test_load_region_returns_none_for_non_dict_payload(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    _write_config(tmp_path, ["KR"])
    assert _load_region(tmp_path) is None


def test_load_region_returns_none_for_missing_or_null_or_non_string_key(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    for payload in ({}, {"region": None}, {"region": 42}):
        _write_config(tmp_path, payload)
        assert _load_region(tmp_path) is None


def test_load_region_returns_none_for_empty_string_key(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    for payload in ({"region": ""}, {"region": "   "}):
        _write_config(tmp_path, payload)
        assert _load_region(tmp_path) is None


def test_load_region_raises_for_invalid_alpha_2(tmp_path: Path) -> None:
    from likesurgeon.config import InvalidRegionError, _load_region

    _write_config(tmp_path, {"region": "KOREA"})
    with pytest.raises(InvalidRegionError):
        _load_region(tmp_path)
