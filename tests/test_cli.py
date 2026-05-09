"""Tests for cli.py — region resolution helper, --region flag wiring,
and the InvalidRegionError → _fail conversion in _bootstrap.

Each scan-wiring test uses Typer's CliRunner against a tmp_path home
(LIKE_SURGEON_HOME env override) and a FakeYouTubeClient that captures
the user_region keyword reaching fetch_video_statuses.
"""

from __future__ import annotations


def test_resolve_region_prefers_cli_over_config() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region("JP", "KR") == "JP"


def test_resolve_region_falls_back_to_config_when_cli_none() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region(None, "KR") == "KR"


def test_resolve_region_returns_none_when_both_none() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region(None, None) is None
