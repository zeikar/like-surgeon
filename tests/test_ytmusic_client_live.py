"""Live e2e for the cookie-import auth flow.

Marked ``@pytest.mark.live`` and deselected by default — they need:
  * a real browser DB readable by browser-cookie3 (locked profiles or
    sandboxed installs surface as ``CookieExtractionError`` here, not a
    test bug);
  * a logged-in music.youtube.com session in the named browser;
  * outbound HTTPS to music.youtube.com.

Run explicitly: ``uv run pytest -m live``. Override the browser via
``LIKESURGEON_LIVE_BROWSER=firefox uv run pytest -m live``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from likesurgeon.ytmusic_client import (
    YTMusicClient,
    write_browser_json_from_browser,
)


@pytest.mark.live
def test_from_browser_writes_browser_json_and_lists_liked_songs(
    tmp_path: Path,
) -> None:
    """End-to-end smoke: extract → persist → load → fetch (twice).

    Pulls cookies from the live browser, writes browser.json into
    ``tmp_path`` (POSIX mode 0o600 verified), then constructs a
    YTMusicClient pointed at it and fetches one page of liked songs.
    Runs the fetch twice back-to-back to catch the cookie-staleness
    regression that motivated the 0.2.1 parse-error boundary — both
    calls must succeed.

    No assertion on the *count* of liked songs: a brand-new test
    account legitimately has zero, and we only care that the auth
    file works and the response shape parses.
    """
    browser = os.environ.get("LIKESURGEON_LIVE_BROWSER", "chrome")
    target = tmp_path / "browser.json"

    write_browser_json_from_browser(browser, target)
    assert target.exists()
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600

    client = YTMusicClient(target)
    first = client.fetch_liked_songs(limit=5)
    assert isinstance(first, list)

    second = client.fetch_liked_songs(limit=5)
    assert isinstance(second, list)
