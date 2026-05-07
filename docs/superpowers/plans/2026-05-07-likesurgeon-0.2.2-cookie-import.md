# likesurgeon 0.2.2 Implementation Plan — cookie-import auth

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual `ytmusicapi browser` header-paste step with a one-flag automatic flow: `likesurgeon auth ytmusic --from-browser chrome` reads YT Music cookies straight from the user's logged-in browser and writes a ytmusicapi-compatible `browser.json`. The manual flow stays as a fallback for environments where browser-cookie3 can't read the cookie store (headless servers, sandboxed browsers).

**Architecture:** One feature branch (`feat/0.2.2-cookie-import`), one task, one commit. All changes land in `ytmusic_client.py` (new helpers + new `CookieExtractionError`) and `cli.py` (new `--from-browser` option). The pure pieces — header dict construction, cookie validation — are extracted as a module-level function so tests can synthesize cookie objects without touching `browser_cookie3`. ytmusicapi itself is untouched; we just generate a file in the format it already accepts.

**Tech Stack:** [browser-cookie3](https://pypi.org/project/browser-cookie3/) (new dep, ~1 MB pure-Python with `lz4` + `pycryptodomex` for Chrome decrypt), Typer/Rich (CLI), pytest, ruff. ytmusicapi continues to handle the data path (`get_liked_songs`).

**Out of scope:**
- OAuth (the 0.2.1 path) — see `docs/notes/ytmusic-oauth-tvhtml5-fallback.md` for the abandoned attempt and what to revisit if cookie-import doesn't work somewhere.
- Auto-detection of "which browser is the user using" — they name it explicitly. We can revisit if the explicit-name UX bites.
- Auto-refresh of `browser.json` on staleness — out of band; if cookies expire, user re-runs `auth ytmusic --from-browser …`.

**Commit cadence.** Per `~/.claude/CLAUDE.md` section 5, when a stop-time review hook is active, commits should happen on the *next* turn after work + verification so the review can examine the diff. The "Stage for commit" step below shows the staged content + suggested message; defer or execute per the active workflow (subagent-driven-development's per-task implementer commits as part of its own cadence).

---

## File Structure

| File | Responsibility |
|------|---------------|
| `pyproject.toml` | Add `browser-cookie3` to dependencies |
| `src/likesurgeon/ytmusic_client.py` | New `CookieExtractionError`; new `extract_youtube_cookies()` (browser_cookie3 wrapper); new `_cookies_to_browser_json()` (pure header-dict builder); new `write_browser_json_from_browser()` (orchestrator with chmod 0o600) |
| `src/likesurgeon/cli.py` | Add `--from-browser` flag to `auth_ytmusic`; route through the new helper, catch `CookieExtractionError` via `_fail` |
| `tests/test_ytmusic_client.py` | Tests for the pure builder + the browser_cookie3 wrapper (monkeypatched) + `write_browser_json_from_browser` end-to-end on tmp_path |
| `README.md` | Lead the YT Music auth section with `--from-browser`, keep manual-paste as fallback; document OS caveats (macOS keychain prompt for Chrome) |

CLI tests are skipped — Typer flag wiring is glue, exercised by manual e2e at the end.

---

## Task 1: Cookie-import auth via browser-cookie3

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Modify: `src/likesurgeon/ytmusic_client.py` (whole file gains ~70 lines)
- Modify: `src/likesurgeon/cli.py:87-104` (`auth_ytmusic` body) and `:20` (import line)
- Modify: `tests/test_ytmusic_client.py` (append helpers + ~5 tests)
- Modify: `README.md` (YT Music auth section)

**Branch:** create from `main` as `feat/0.2.2-cookie-import` before step 1.

- [ ] **Step 1: Add the `browser-cookie3` dep**

Run:

```bash
uv add browser-cookie3
```

This updates `pyproject.toml` and `uv.lock`. Expected: ~3 packages installed (`browser-cookie3`, `lz4`, `pycryptodomex`).

Sanity-check the import *and* probe every name we'll list in `SUPPORTED_BROWSERS` (Step 3) so drift in browser-cookie3 is caught at plan-execution time rather than runtime — `arc` in particular is exposed by 0.20.1 but isn't on third-party indices like libraries.io, so the probe doubles as a record that we actually verified it:

```bash
uv run python - <<'PY'
import browser_cookie3
expected = [
    "chrome", "chromium", "firefox", "edge", "brave", "safari",
    "opera", "opera_gx", "librewolf", "vivaldi", "arc", "w3m", "lynx",
]
missing = [n for n in expected if not callable(getattr(browser_cookie3, n, None))]
if missing:
    raise SystemExit(f"missing from browser_cookie3: {missing}")
print(f"OK ({len(expected)} browsers callable)")
PY
```

Expected output: `OK (13 browsers callable)`. If any are missing, drop them from both `SUPPORTED_BROWSERS` (Step 3) and the help/README copy (Steps 8–9) before continuing.

- [ ] **Step 2: Failing tests for the pure builder**

Append to `tests/test_ytmusic_client.py`:

```python
class _StubCookie:
    """Minimal duck-typed stand-in for browser_cookie3's Cookie objects.

    Only ``.name`` and ``.value`` are read by ``_cookies_to_browser_json``.
    Keeping the stub here means tests don't depend on importing
    ``browser_cookie3`` (which would prompt for keychain access on macOS
    when CI runs against a real Chrome profile).
    """

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


def test_cookies_to_browser_json_builds_expected_headers() -> None:
    from likesurgeon.ytmusic_client import _cookies_to_browser_json

    cookies = [
        _StubCookie("__Secure-3PAPISID", "abc"),
        _StubCookie("SAPISID", "abc"),
        _StubCookie("__Secure-3PSID", "xyz"),
    ]
    headers = _cookies_to_browser_json(cookies)
    # Cookie header has all three names.
    cookie = headers["Cookie"]
    assert "__Secure-3PAPISID=abc" in cookie
    assert "SAPISID=abc" in cookie
    assert "__Secure-3PSID=xyz" in cookie
    # Required ytmusicapi headers present.
    assert headers["x-origin"] == "https://music.youtube.com"
    assert "User-Agent" in headers
    assert headers["Content-Type"] == "application/json"
    # ytmusicapi's `determine_auth_type` reads the Authorization header at
    # YTMusic init time — without "SAPISIDHASH" in the value it falls through
    # to OAUTH_CUSTOM_CLIENT and demands `oauth_credentials`. The hash is
    # regenerated on every actual request from the cookie, so the persisted
    # value just needs the right *prefix* for type detection.
    assert headers["Authorization"].startswith("SAPISIDHASH ")


def test_cookies_to_browser_json_rejects_missing_3papisid() -> None:
    """ytmusicapi's `sapisid_from_cookie` reads `__Secure-3PAPISID` to compute
    the SAPISIDHASH on every request. Without it the auth file is unusable —
    surface that early with a clear message."""
    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        _cookies_to_browser_json,
    )

    cookies = [_StubCookie("SAPISID", "abc"), _StubCookie("__Secure-3PSID", "xyz")]
    with pytest.raises(CookieExtractionError, match="__Secure-3PAPISID"):
        _cookies_to_browser_json(cookies)


def test_cookies_to_browser_json_rejects_empty() -> None:
    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        _cookies_to_browser_json,
    )

    with pytest.raises(CookieExtractionError, match="No youtube"):
        _cookies_to_browser_json([])
```

Run: `uv run pytest tests/test_ytmusic_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'CookieExtractionError'` (or `_cookies_to_browser_json`).

- [ ] **Step 3: Implement `CookieExtractionError` and the pure builder**

In `src/likesurgeon/ytmusic_client.py`, after the existing imports add `import json`, `import os`, `import sys`, `from collections.abc import Iterable`. After the existing exception classes append:

```python
class CookieExtractionError(RuntimeError):
    """Raised when we can't pull usable YouTube cookies from a browser.

    Covers both "browser DB unreadable" (sandboxed, locked, decrypt failure)
    and "DB was readable but the user wasn't actually logged in" (no
    ``__Secure-3PAPISID`` cookie). Recovery is identical: log into
    music.youtube.com in the named browser and retry.
    """


# Browsers `browser_cookie3` 0.20.1 exposes as lowercase top-level functions.
# Listed explicitly so an unsupported value gives a friendly error instead
# of a generic AttributeError on the underlying lookup. Verified via probe;
# includes text-mode browsers (`w3m`, `lynx`) for completeness even though
# they're unlikely to have a usable music.youtube.com session.
SUPPORTED_BROWSERS = (
    "chrome",
    "chromium",
    "firefox",
    "edge",
    "brave",
    "safari",
    "opera",
    "opera_gx",
    "librewolf",
    "vivaldi",
    "arc",
    "w3m",
    "lynx",
)


_YTM_ORIGIN = "https://music.youtube.com"


def _cookies_to_browser_json(cookies: Iterable[Any]) -> dict[str, str]:
    """Build a ytmusicapi-compatible browser.json header dict from cookies.

    Pure function — no I/O. Each cookie object must expose ``.name`` and
    ``.value`` (the public surface of both ``browser_cookie3``'s Cookie and
    the test stub). Validates that ``__Secure-3PAPISID`` is present, since
    ytmusicapi's ``sapisid_from_cookie`` reads exactly that name to build
    the per-request SAPISIDHASH.

    Also embeds an ``Authorization: SAPISIDHASH …`` header. ytmusicapi's
    ``determine_auth_type`` reads this *at YTMusic init time* to classify
    the file as ``AuthType.BROWSER`` — without it the file is treated as
    OAuth and the constructor errors out for missing ``oauth_credentials``.
    The hash is regenerated dynamically on every real request, so the
    value persisted to disk is only used for type detection.
    """
    from ytmusicapi.helpers import get_authorization

    cookie_list = list(cookies)
    if not cookie_list:
        raise CookieExtractionError(
            "No youtube.com cookies found in the browser. "
            "Make sure you're logged into music.youtube.com in that browser, "
            "then retry."
        )
    by_name = {c.name: c.value for c in cookie_list}
    if "__Secure-3PAPISID" not in by_name:
        raise CookieExtractionError(
            "Missing `__Secure-3PAPISID` cookie — that's what ytmusicapi "
            "hashes for the Authorization header. Are you actually signed in "
            "on that browser? (Sign-out wipes this cookie.)"
        )
    cookie_header = "; ".join(f"{name}={value}" for name, value in by_name.items())
    authorization = get_authorization(f"{by_name['__Secure-3PAPISID']} {_YTM_ORIGIN}")
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:88.0) "
            "Gecko/20100101 Firefox/88.0"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Content-Type": "application/json",
        "X-Goog-AuthUser": "0",
        "x-origin": _YTM_ORIGIN,
        "Authorization": authorization,
        "Cookie": cookie_header,
    }
```

Run: `uv run pytest tests/test_ytmusic_client.py -v`
Expected: PASS — the three new tests + the existing tests.

- [ ] **Step 4: Failing tests for the browser_cookie3 wrapper**

Append to `tests/test_ytmusic_client.py`:

```python
def test_extract_youtube_cookies_rejects_unsupported_browser() -> None:
    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        extract_youtube_cookies,
    )

    with pytest.raises(CookieExtractionError, match="Unsupported browser"):
        extract_youtube_cookies("netscape")


def test_extract_youtube_cookies_dispatches_to_browser_cookie3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper should call ``browser_cookie3.<name>(domain_name="youtube.com")``
    for the named browser and return the resulting jar as a list."""
    import browser_cookie3

    from likesurgeon.ytmusic_client import extract_youtube_cookies

    fake_jar = [_StubCookie("__Secure-3PAPISID", "x")]
    captured: dict[str, Any] = {}

    def fake_chrome(**kwargs: Any) -> list[Any]:
        captured["kwargs"] = kwargs
        return fake_jar

    monkeypatch.setattr(browser_cookie3, "chrome", fake_chrome)
    cookies = extract_youtube_cookies("chrome")
    assert cookies == fake_jar
    assert captured["kwargs"] == {"domain_name": "youtube.com"}


def test_extract_youtube_cookies_wraps_browser_cookie_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decrypt / DB-locked failures from browser_cookie3 should surface as
    our own CookieExtractionError so the CLI's `_fail` can handle them."""
    import browser_cookie3

    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        extract_youtube_cookies,
    )

    def boom(**kwargs: Any) -> list[Any]:
        raise browser_cookie3.BrowserCookieError("locked")

    monkeypatch.setattr(browser_cookie3, "firefox", boom)
    with pytest.raises(CookieExtractionError, match="firefox"):
        extract_youtube_cookies("firefox")


def test_extract_youtube_cookies_wraps_arbitrary_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browser_cookie3 calls into platform-specific decrypt code and not all
    failures consolidate into BrowserCookieError — verify we wrap a generic
    OSError so the user still sees one friendly error class."""
    import browser_cookie3

    from likesurgeon.ytmusic_client import (
        CookieExtractionError,
        extract_youtube_cookies,
    )

    def boom(**kwargs: Any) -> list[Any]:
        raise OSError("Permission denied")

    monkeypatch.setattr(browser_cookie3, "edge", boom)
    with pytest.raises(CookieExtractionError, match="OSError") as exc_info:
        extract_youtube_cookies("edge")
    # Original exception preserved via __cause__ for diagnostics.
    assert isinstance(exc_info.value.__cause__, OSError)
```

Run: `uv run pytest tests/test_ytmusic_client.py::test_extract_youtube_cookies_rejects_unsupported_browser -v`
Expected: FAIL — `ImportError: cannot import name 'extract_youtube_cookies'`.

- [ ] **Step 5: Implement `extract_youtube_cookies`**

In `src/likesurgeon/ytmusic_client.py`, add after `_cookies_to_browser_json`:

```python
def extract_youtube_cookies(browser: str) -> list[Any]:
    """Read youtube.com cookies from the user's named browser DB.

    Wraps ``browser_cookie3.<browser>(domain_name="youtube.com")``. The
    ``domain_name`` filter substring-matches both ``.youtube.com`` and
    ``.music.youtube.com``, which is what ytmusicapi needs.

    Raises ``CookieExtractionError`` for unsupported browser names or any
    failure pulling the cookies. ``browser_cookie3`` calls into
    platform-specific decrypt code (DPAPI on Windows, libsecret/Keychain on
    Linux/macOS) and not all failures consolidate into ``BrowserCookieError``
    — we've seen ``OSError``, ``RuntimeError``, ``sqlite3.OperationalError``
    leak through in the wild. Catch broadly at this system boundary so the
    CLI gets one named exception regardless of which OS path failed.
    """
    if browser not in SUPPORTED_BROWSERS:
        raise CookieExtractionError(
            f"Unsupported browser: {browser!r}. Choose from: "
            + ", ".join(SUPPORTED_BROWSERS)
        )
    import browser_cookie3

    extractor = getattr(browser_cookie3, browser)
    try:
        jar = extractor(domain_name="youtube.com")
    except Exception as exc:  # noqa: BLE001 — system-boundary catch (see docstring)
        raise CookieExtractionError(
            f"Failed to read {browser} cookies: {type(exc).__name__}: {exc}. "
            f"Make sure {browser} is installed, you're logged into "
            "music.youtube.com, and the browser DB isn't locked by a running "
            "instance (especially on Windows)."
        ) from exc
    return list(jar)
```

Run: `uv run pytest tests/test_ytmusic_client.py -v`
Expected: PASS — all extraction-wrapper tests now green.

- [ ] **Step 6: Failing test for the orchestrator + chmod**

Append to `tests/test_ytmusic_client.py`:

```python
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
def test_write_browser_json_from_browser_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end test of the orchestrator: stubbed cookies → file on disk
    with chmod 0o600 → contents parse back to a valid headers dict."""
    import json as json_lib

    import browser_cookie3

    from likesurgeon.ytmusic_client import write_browser_json_from_browser

    monkeypatch.setattr(
        browser_cookie3,
        "chrome",
        lambda **kw: [
            _StubCookie("__Secure-3PAPISID", "abc"),
            _StubCookie("SAPISID", "abc"),
        ],
    )
    target = tmp_path / "browser.json"
    write_browser_json_from_browser("chrome", target)

    assert target.exists()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600
    headers = json_lib.loads(target.read_text(encoding="utf-8"))
    assert "__Secure-3PAPISID=abc" in headers["Cookie"]
    assert headers["x-origin"] == "https://music.youtube.com"
```

Add `import sys` to the test file's top imports if not already present.

Run: `uv run pytest tests/test_ytmusic_client.py::test_write_browser_json_from_browser_e2e -v`
Expected: FAIL — `ImportError: cannot import name 'write_browser_json_from_browser'`.

- [ ] **Step 7: Implement `write_browser_json_from_browser`**

Append to `src/likesurgeon/ytmusic_client.py`:

```python
def write_browser_json_from_browser(browser: str, target: Path) -> None:
    """Read youtube.com cookies from ``browser``, build a ytmusicapi-compatible
    ``browser.json``, and write it to ``target`` (chmod 0o600 on POSIX).

    Replaces the user-facing manual flow ``ytmusicapi browser`` for users who
    are already logged into music.youtube.com in a supported browser.
    """
    cookies = extract_youtube_cookies(browser)
    headers = _cookies_to_browser_json(cookies)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(headers, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(target, 0o600)
```

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

- [ ] **Step 8: Wire the `--from-browser` flag into `auth_ytmusic`**

Edit `src/likesurgeon/cli.py`. Update the import on line 20 to include the new exception and helper:

```python
from .ytmusic_client import (
    AuthFileMissingError,
    CookieExtractionError,
    UnexpectedResponseError,
    YTMusicClient,
    write_browser_json_from_browser,
)
```

Replace the entire `auth_ytmusic` function (currently lines 87-104) with:

```python
@auth_app.command("ytmusic")
def auth_ytmusic(
    from_browser: Annotated[
        str | None,
        typer.Option(
            "--from-browser",
            help=(
                "Auto-extract cookies from this browser instead of running "
                "the manual ytmusicapi paste flow. Supported (lowercase): "
                "chrome, chromium, firefox, edge, brave, safari, opera, "
                "opera_gx, librewolf, vivaldi, arc, w3m, lynx. Requires you "
                "to be logged into music.youtube.com in that browser."
            ),
        ),
    ] = None,
) -> None:
    """Set up ytmusicapi browser-header auth for YouTube Music."""
    cfg = Config.load()
    cfg.ensure_app_dir()
    target = cfg.ytmusic_browser_path

    if from_browser is not None:
        # Normalize so `Chrome`, ` chrome `, and `CHROME` all dispatch the same
        # way — `SUPPORTED_BROWSERS` is lowercase by design.
        normalized = from_browser.strip().lower()
        try:
            write_browser_json_from_browser(normalized, target)
        except CookieExtractionError as e:
            _fail(str(e), code=2)
        console.print(
            f"[green]✓[/green] browser.json written to [cyan]{target}[/cyan].\n"
            "[dim]Verify with: [/dim]"
            "[cyan]uv run likesurgeon scan ytmusic --limit 1[/cyan]"
        )
        return

    console.print("[bold]YouTube Music browser-header setup[/bold]")
    console.print(
        "Tip: skip the manual paste with "
        "[cyan]uv run likesurgeon auth ytmusic --from-browser chrome[/cyan] "
        "(or firefox / edge / etc.) if you're logged into music.youtube.com "
        "in that browser.\n\n"
        "Manual flow:\n"
        "1. Open YouTube Music in your browser and sign in.\n"
        "2. Open DevTools → Network → find an authenticated POST request to "
        "[cyan]/youtubei/v1/browse[/cyan] and copy its raw request headers.\n"
        "   (See [cyan]https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html[/cyan])\n"
        "3. Run [cyan]uv run ytmusicapi browser[/cyan] and paste the headers when prompted.\n"
        f"4. Move the generated [cyan]browser.json[/cyan] to [cyan]{target}[/cyan]."
    )
```

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: all pass.

- [ ] **Step 9: README updates**

In `README.md`, replace the YT Music auth block (around lines 53-60) with:

```markdown
**YouTube Music** (browser-header flow, auto-extracted from your browser):

```bash
uv run likesurgeon auth ytmusic --from-browser chrome
```

Replace `chrome` with whichever browser you're signed into music.youtube.com on
(supported lowercase names: `chromium`, `firefox`, `edge`, `brave`, `safari`,
`opera`, `opera_gx`, `librewolf`, `vivaldi`, `arc`, `w3m`, `lynx`). The command
reads cookies from that browser's local store and writes
`~/.like-surgeon/browser.json` (POSIX mode `0o600`) — no DevTools copy-paste
required.

> **macOS quirk.** Chrome (and Chromium-family browsers) on macOS encrypt
> their cookie store with a Keychain entry; the first run prompts you to
> allow `python` (or `Terminal`) to access it. Firefox usually avoids that
> prompt because it stores cookies in plain SQLite. Safari may still be
> blocked by macOS privacy settings — if extraction fails, give Terminal
> (or your IDE) **Full Disk Access** in System Settings → Privacy &
> Security and retry.

If `--from-browser` doesn't work in your environment (sandboxed browser,
headless server, locked DB), fall back to the manual flow:

```bash
uv run likesurgeon auth ytmusic
```

This prints the four-step `ytmusicapi browser` paste recipe.

> **Treat `~/.like-surgeon/browser.json` like a session token.** It contains
> live YouTube Music cookies — anyone who reads the file can act as you on
> music.youtube.com until those cookies rotate. The tool stores it with
> POSIX mode `0o600` (owner read/write only). Don't commit it, share it,
> or leave it in shared filesystems.
```

(Don't touch the YouTube Data API auth block below it.)

Also fix the roadmap row for 0.2.2 — the previous wording said cookies are
pulled "at scan time," but in this implementation `--from-browser` runs at
auth time and persists `browser.json` for subsequent scans (matching the
existing browser-header flow's lifecycle). Update the row in the Roadmap
table:

```markdown
| **0.2.2**   | Auth/UX polish: `--from-browser` flag on `auth ytmusic` reads YT Music cookies straight from a logged-in browser via [browser-cookie3](https://pypi.org/project/browser-cookie3/) and writes a ytmusicapi-compatible `browser.json` (POSIX mode `0o600`). Manual paste flow stays as a fallback. The TVHTML5 OAuth path documented in [docs/notes/](docs/notes/ytmusic-oauth-tvhtml5-fallback.md) remains shelved unless cookie extraction fails on a target platform. |
```

- [ ] **Step 10: Manual e2e (run on the actual user's machine, not a fake)**

```bash
# Backup any existing browser.json so you can compare:
cp ~/.like-surgeon/browser.json ~/browser.json.bak  2>/dev/null || true

# 1. Make sure you're logged into music.youtube.com in Chrome (or whichever browser).
# 2. Run the new flag:
uv run likesurgeon auth ytmusic --from-browser chrome

# 3. Verify the file:
ls -la ~/.like-surgeon/browser.json   # should be 0o600 (-rw-------)
jq '.["x-origin"], .Cookie | tostring | .[0:60]' ~/.like-surgeon/browser.json  # spot-check shape
# This run's headline regression is the missing-Authorization bug — confirm
# the SAPISIDHASH header made it into the file or ytmusicapi will mis-classify
# the auth type at the next scan and demand `oauth_credentials`:
jq '.Authorization | startswith("SAPISIDHASH ")' ~/.like-surgeon/browser.json
# expected output: true

# 4. End-to-end smoke:
uv run likesurgeon scan ytmusic --limit 5

# 5. Run twice in close succession — make sure the second one doesn't
#    blow up like the cookie-staleness scenario from yesterday.
uv run likesurgeon scan ytmusic --limit 5
```

If both scans complete cleanly, the cookie-import path works end-to-end. If the
second one trips the parse-error boundary we shipped in 0.2.1-salvage, the
cookies were good once but something rotated between calls — note it and we'll
investigate before claiming this fixes the staleness pain.

- [ ] **Step 11: Stage Task 1 for commit**

Confirm working tree is review-ready (lint + tests green, manual e2e passed).
Files to stage when committing:

```
pyproject.toml
uv.lock
src/likesurgeon/ytmusic_client.py
src/likesurgeon/cli.py
tests/test_ytmusic_client.py
README.md
```

Suggested commit message (commit deferred per the "Commit cadence" header note —
let stop-time review run on the diff first):

```
feat(auth): auto-extract YT Music cookies via --from-browser
```

---

## Final verification

- [ ] **Test suite green:** `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
- [ ] **Commit lands** per the "Commit cadence" header policy.
- [ ] **Push branch:** `git push -u origin feat/0.2.2-cookie-import`
- [ ] **Open PR** targeting `main` with the suggested commit message in the title and a body that links to:
  - the 0.2.1 salvage PR for the parse-error boundary context
  - `docs/notes/ytmusic-oauth-tvhtml5-fallback.md` for what we tried and why this path beats it
  - a one-line manual e2e summary ("scanned N tracks twice in a row, no regression")
