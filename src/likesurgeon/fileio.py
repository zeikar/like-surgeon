"""Credential-file writes shared by the provider clients."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path


def write_private_text(target: Path, text: str) -> None:
    """Write ``text`` to ``target`` atomically, mode ``0o600`` on POSIX from
    creation (falls back to ``Path.write_text`` on Windows where POSIX mode
    bits don't apply).

    On POSIX we write to a sibling temp file in ``target.parent`` and then
    ``os.replace`` it into place. ``tempfile.mkstemp`` creates the temp file
    with mode ``0o600`` from inception, and rename(2) preserves that mode
    onto ``target`` — so the fresh secret never lives on disk at a more
    permissive mode, even when ``target`` already existed at e.g. ``0o644``
    from an older version of this code. Same-directory rename is required
    for atomicity (cross-filesystem rename isn't atomic).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        target.write_text(text, encoding="utf-8")
        return
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
