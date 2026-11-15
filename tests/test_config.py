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


def test_config_load_fuzzy_threshold_is_none_when_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is deprecated; None tells compare-likes there's nothing to warn about."""
    from likesurgeon.config import Config

    monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
    _write_config(tmp_path, {"region": "KR"})
    assert Config.load().fuzzy_threshold is None


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
# ytmusic_browser / ytmusic_account — recorded by `auth ytmusic --from-browser`
# ---------------------------------------------------------------------------


def test_config_load_reads_ytmusic_refresh_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from likesurgeon.config import Config

    monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
    _write_config(tmp_path, {"ytmusic_browser": "chrome", "ytmusic_account": "@me"})
    cfg = Config.load()
    assert (cfg.ytmusic_browser, cfg.ytmusic_account) == ("chrome", "@me")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"ytmusic_browser": 3, "ytmusic_account": 3},
        {"ytmusic_browser": "netscape", "ytmusic_account": ""},
        "{bad",
    ],
)
def test_config_load_ytmusic_refresh_keys_none_when_absent_or_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    from likesurgeon.config import Config

    monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
    if payload is not None:
        _write_config(tmp_path, payload)
    cfg = Config.load()
    assert (cfg.ytmusic_browser, cfg.ytmusic_account) == (None, None)


def test_update_config_keeps_other_keys(tmp_path: Path) -> None:
    from likesurgeon.config import update_config

    _write_config(tmp_path, {"region": "KR", "fuzzy_threshold": 80})
    update_config(tmp_path, {"ytmusic_browser": "firefox", "ytmusic_account": "@설"})
    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert json.loads(raw) == {
        "region": "KR",
        "fuzzy_threshold": 80,
        "ytmusic_browser": "firefox",
        "ytmusic_account": "@설",
    }
    assert "@설" in raw  # written as text, not \u escapes


def test_update_config_creates_config_when_missing(tmp_path: Path) -> None:
    from likesurgeon.config import update_config

    update_config(tmp_path, {"ytmusic_browser": "chrome"})
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data == {"ytmusic_browser": "chrome"}


def test_update_config_none_removes_only_those_keys(tmp_path: Path) -> None:
    from likesurgeon.config import update_config

    _write_config(tmp_path, {"region": "KR", "ytmusic_browser": "chrome", "ytmusic_account": "@me"})
    update_config(tmp_path, {"ytmusic_browser": None, "ytmusic_account": None})
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data == {"region": "KR"}


def test_update_config_without_change_writes_nothing(tmp_path: Path) -> None:
    from likesurgeon.config import update_config

    update_config(tmp_path, {"ytmusic_browser": None})
    assert not (tmp_path / "config.json").exists()


@pytest.mark.parametrize("raw", ["{bad", "[1, 2]"])
def test_update_config_refuses_to_overwrite_unreadable_config(tmp_path: Path, raw: str) -> None:
    """A config.json that isn't a JSON object is left for the user to fix,
    never replaced (that would drop their region)."""
    from likesurgeon.config import update_config

    _write_config(tmp_path, raw)
    with pytest.raises(ValueError):
        update_config(tmp_path, {"ytmusic_browser": "chrome"})
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == raw
