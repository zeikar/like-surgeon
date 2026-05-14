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


# ---------------------------------------------------------------------------
# fuzzy_threshold — validator tests
# ---------------------------------------------------------------------------


def test_validate_fuzzy_threshold_accepts_in_range() -> None:
    from likesurgeon.config import _validate_fuzzy_threshold

    for v in (0, 50, 85, 100):
        assert _validate_fuzzy_threshold(v) == v


def test_validate_fuzzy_threshold_rejects_negative() -> None:
    from likesurgeon.config import InvalidFuzzyThresholdError, _validate_fuzzy_threshold

    for v in (-1, -100):
        with pytest.raises(InvalidFuzzyThresholdError):
            _validate_fuzzy_threshold(v)


def test_validate_fuzzy_threshold_rejects_over_100() -> None:
    from likesurgeon.config import InvalidFuzzyThresholdError, _validate_fuzzy_threshold

    for v in (101, 150):
        with pytest.raises(InvalidFuzzyThresholdError):
            _validate_fuzzy_threshold(v)


def test_validate_fuzzy_threshold_rejects_non_integer() -> None:
    from likesurgeon.config import InvalidFuzzyThresholdError, _validate_fuzzy_threshold

    for v in ("85", 85.0, None):
        with pytest.raises(InvalidFuzzyThresholdError) as exc_info:
            _validate_fuzzy_threshold(v)
        assert repr(v) in str(exc_info.value)


def test_validate_fuzzy_threshold_rejects_bool() -> None:
    from likesurgeon.config import InvalidFuzzyThresholdError, _validate_fuzzy_threshold

    for v in (True, False):
        with pytest.raises(InvalidFuzzyThresholdError):
            _validate_fuzzy_threshold(v)


# ---------------------------------------------------------------------------
# fuzzy_threshold — loader tests
# ---------------------------------------------------------------------------


def test_load_fuzzy_threshold_returns_none_when_config_missing(tmp_path: Path) -> None:
    from likesurgeon.config import _load_fuzzy_threshold

    assert _load_fuzzy_threshold(tmp_path) is None


def test_load_fuzzy_threshold_reads_value_from_config_json(tmp_path: Path) -> None:
    from likesurgeon.config import _load_fuzzy_threshold

    _write_config(tmp_path, {"fuzzy_threshold": 80})
    assert _load_fuzzy_threshold(tmp_path) == 80


def test_load_fuzzy_threshold_returns_none_for_missing_or_null_key(tmp_path: Path) -> None:
    from likesurgeon.config import _load_fuzzy_threshold

    for payload in ({}, {"fuzzy_threshold": None}):
        _write_config(tmp_path, payload)
        assert _load_fuzzy_threshold(tmp_path) is None


def test_load_fuzzy_threshold_returns_none_for_malformed_json(tmp_path: Path) -> None:
    from likesurgeon.config import _load_fuzzy_threshold

    _write_config(tmp_path, "{")
    assert _load_fuzzy_threshold(tmp_path) is None


def test_load_fuzzy_threshold_returns_none_for_non_dict_payload(tmp_path: Path) -> None:
    from likesurgeon.config import _load_fuzzy_threshold

    for payload in ([1, 2, 3], 42):
        _write_config(tmp_path, payload)
        assert _load_fuzzy_threshold(tmp_path) is None


def test_load_fuzzy_threshold_raises_for_out_of_range(tmp_path: Path) -> None:
    from likesurgeon.config import InvalidFuzzyThresholdError, _load_fuzzy_threshold

    _write_config(tmp_path, {"fuzzy_threshold": 150})
    with pytest.raises(InvalidFuzzyThresholdError):
        _load_fuzzy_threshold(tmp_path)


def test_load_fuzzy_threshold_raises_for_non_integer_string(tmp_path: Path) -> None:
    from likesurgeon.config import InvalidFuzzyThresholdError, _load_fuzzy_threshold

    _write_config(tmp_path, {"fuzzy_threshold": "85"})
    with pytest.raises(InvalidFuzzyThresholdError):
        _load_fuzzy_threshold(tmp_path)


def test_load_fuzzy_threshold_raises_for_json_bool(tmp_path: Path) -> None:
    from likesurgeon.config import InvalidFuzzyThresholdError, _load_fuzzy_threshold

    _write_config(tmp_path, {"fuzzy_threshold": True})
    with pytest.raises(InvalidFuzzyThresholdError):
        _load_fuzzy_threshold(tmp_path)


# ---------------------------------------------------------------------------
# Config.load — fuzzy_threshold integration tests
# ---------------------------------------------------------------------------


def test_config_load_uses_default_when_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from likesurgeon.config import Config

    monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
    _write_config(tmp_path, {"region": "KR"})
    assert Config.load().fuzzy_threshold == 85


def test_config_load_uses_config_value_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from likesurgeon.config import Config

    monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
    _write_config(tmp_path, {"fuzzy_threshold": 80})
    assert Config.load().fuzzy_threshold == 80


def test_config_load_raises_invalid_fuzzy_threshold_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from likesurgeon.config import Config, InvalidFuzzyThresholdError

    monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
    _write_config(tmp_path, {"fuzzy_threshold": 150})
    with pytest.raises(InvalidFuzzyThresholdError) as exc_info:
        Config.load()
    assert "150" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Drift-invariant: CompareInput and Config share the same default constant
# ---------------------------------------------------------------------------


def test_compare_and_config_share_default_constant() -> None:
    from likesurgeon.compare import DEFAULT_FUZZY_THRESHOLD, CompareInput

    assert CompareInput(ytmusic=[], youtube=[]).fuzzy_threshold == DEFAULT_FUZZY_THRESHOLD == 85
