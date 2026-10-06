"""Typer CLI for likesurgeon.

Every command only reads the providers and writes the local DB, except
``sync``, which writes likes on YouTube.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy.orm import sessionmaker

from . import __version__
from .config import Config, InvalidFuzzyThresholdError, InvalidRegionError, _validate_region
from .db import init_db, make_engine, make_session_factory, session_scope
from .diagnosis import (
    ALIGNMENT_ISSUE_TYPES,
    diagnosis_items,
    latest_diagnosis,
)
from .diff import diff_snapshots
from .doctor import health_summary
from .export import export_snapshot_json
from .pipeline import MissingSnapshotsError, compare_and_persist
from .snapshot import (
    create_snapshot,
    get_snapshot_items,
    list_snapshots,
)
from .sync import (  # noqa: F401 — _TRACK_LOOKUP_BATCH_SIZE is re-exported
    _TRACK_LOOKUP_BATCH_SIZE,
    _video_ids_for_tracks,
    plan,
    resolve_video_ids,
    summarize,
)
from .sync_dispatch import execute
from .sync_preflight import (
    diagnosis_staleness,
    stranded_unliked_video_ids,
    sync_attempts_since_older_scan,
)
from .ytmusic_client import (
    SUPPORTED_BROWSERS,
    AuthFileMissingError,
    CookieExtractionError,
    UnexpectedResponseError,
    YTMusicClient,
    write_browser_json_from_browser,
)

# All option/argument metadata is attached via ``Annotated[...]`` rather than
# ``typer.Option(...)`` defaults so that ruff's ``B008`` (function call in
# argument default) stays clean. This is also the modern Typer-recommended
# pattern.

app = typer.Typer(
    help=(
        "Back up your YouTube Music liked songs, find relinked, duplicate and dead likes, "
        "and fix the ones it can prove."
    ),
    no_args_is_help=True,
    add_completion=False,
)
auth_app = typer.Typer(help="Set up authentication with music providers.", no_args_is_help=True)
scan_app = typer.Typer(help="Scan music providers for liked songs.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(scan_app, name="scan")

console = Console()
err_console = Console(stderr=True)


def _safe_config_load() -> Config:
    """Wrap ``Config.load`` so a ``config.json`` typo on the ``region`` or
    ``fuzzy_threshold`` key fails fast with a friendly exit-2 instead of a
    traceback.

    Used by every CLI entrypoint that reads config — ``_bootstrap`` for
    DB-backed commands plus the ``auth`` setup commands that don't need
    a session. CLI flag validation lives elsewhere in the Typer callback
    ``_parse_region_flag``.
    """
    try:
        return Config.load()
    except (InvalidRegionError, InvalidFuzzyThresholdError) as e:
        _fail(str(e), code=2)


def _bootstrap() -> tuple[Config, sessionmaker]:
    """Resolve config, ensure app dir + schema, return a session factory."""
    cfg = _safe_config_load()
    cfg.ensure_app_dir()
    engine = make_engine(cfg.db_path)
    init_db(engine)
    return cfg, make_session_factory(engine)


def _fail(msg: str, code: int = 1) -> NoReturn:
    err_console.print(f"[bold red]Error:[/bold red] {msg}")
    raise typer.Exit(code)


def _resolve_region(cli_region: str | None, config_region: str | None) -> str | None:
    """Pick the region to use for this scan.

    Precedence: CLI flag > config.json > None. Both inputs are
    pre-validated upstream (Config.load and the Typer parser callback
    both call ``_validate_region`` and raise ``InvalidRegionError`` on
    bad shape), so this helper sees only ``None`` or a valid alpha-2
    code.
    """
    return cli_region or config_region


def _parse_region_flag(value: str | None) -> str | None:
    """Typer callback for ``--region``: validates the alpha-2 shape and
    raises ``typer.BadParameter`` (exit 2) on bad input so the failure
    surfaces before any API call."""
    if value is None:
        return None
    try:
        return _validate_region(value)
    except InvalidRegionError as e:
        raise typer.BadParameter(str(e)) from e


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"likesurgeon {__version__}")
        raise typer.Exit()


# ``is_eager=True`` is required: the root ``Typer`` has ``no_args_is_help=True``,
# which short-circuits with the "Missing command" help text *before* a non-eager
# callback body runs. An eager option callback fires before that check.
@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    pass


@app.command()
def init() -> None:
    """Initialize the local app directory and SQLite database."""
    cfg, _ = _bootstrap()
    console.print(f"[green]✓[/green] Initialized at [cyan]{cfg.app_dir}[/cyan]")
    console.print(f"  Database: [cyan]{cfg.db_path}[/cyan]")


@auth_app.command("ytmusic")
def auth_ytmusic(
    from_browser: Annotated[
        str | None,
        typer.Option(
            "--from-browser",
            help=(
                "Auto-extract cookies from this browser instead of running "
                "the manual ytmusicapi paste flow. Supported (lowercase): "
                f"{', '.join(SUPPORTED_BROWSERS)}. Requires you to be logged "
                "into music.youtube.com in that browser."
            ),
        ),
    ] = None,
) -> None:
    """Set up ytmusicapi browser-header auth for YouTube Music."""
    cfg = _safe_config_load()
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
            "[cyan]likesurgeon scan ytmusic[/cyan]"
        )
        return

    console.print("[bold]YouTube Music browser-header setup[/bold]")
    console.print(
        "Tip: skip the manual paste with "
        "[cyan]likesurgeon auth ytmusic --from-browser chrome[/cyan] "
        "(or firefox / edge / etc.) if you're logged into music.youtube.com "
        "in that browser.\n\n"
        "Manual flow:\n"
        "1. Open YouTube Music in your browser and sign in.\n"
        "2. Open DevTools → Network → find an authenticated POST request to "
        "[cyan]/youtubei/v1/browse[/cyan] and copy its raw request headers.\n"
        "   (See [cyan]https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html[/cyan])\n"
        # A `uv tool` / pipx install doesn't put ytmusicapi's own CLI on PATH.
        "3. Run [cyan]uvx --from ytmusicapi ytmusicapi browser[/cyan] and paste the "
        "headers when prompted.\n"
        f"4. Move the generated [cyan]browser.json[/cyan] to [cyan]{target}[/cyan]."
    )


@auth_app.command("youtube")
def auth_youtube() -> None:
    """Set up OAuth for YouTube Data API access."""
    cfg = _safe_config_load()
    cfg.ensure_app_dir()
    secrets_path = cfg.youtube_oauth_client_path
    token_path = cfg.youtube_token_path

    console.print("[bold]YouTube OAuth setup[/bold]")
    if not secrets_path.exists():
        console.print(
            "1. Open [cyan]https://console.cloud.google.com/[/cyan] and create "
            "(or pick) a project.\n"
            "2. Enable [bold]YouTube Data API v3[/bold] for the project.\n"
            "3. APIs & Services → OAuth consent screen → External, add yourself "
            "as a test user.\n"
            "4. Credentials → Create credentials → OAuth client ID → "
            "[bold]Desktop app[/bold] → download the JSON.\n"
            f"5. Save it as [cyan]{secrets_path}[/cyan].\n"
            "6. Re-run [cyan]likesurgeon auth youtube[/cyan]."
        )
        return

    from .youtube_client import YouTubeClient

    client = YouTubeClient(
        client_secrets_path=secrets_path,
        token_path=token_path,
    )
    console.print(
        "[dim]Opening browser for Google consent…[/dim] "
        "(this will block until you approve and the local server captures the redirect)."
    )
    client.authorize()
    console.print(f"[green]✓[/green] Authorized. Token saved to [cyan]{token_path}[/cyan].")


# Neither scan takes a --limit: compare-likes aligns the two lists by
# position, which a truncated scan breaks (spec §5.2).
@scan_app.command("ytmusic")
def scan_ytmusic() -> None:
    """Fetch every YouTube Music liked song and store a snapshot."""
    cfg, factory = _bootstrap()
    client = YTMusicClient(browser_path=cfg.ytmusic_browser_path)
    try:
        items = client.fetch_liked_songs()
    except (AuthFileMissingError, UnexpectedResponseError) as e:
        _fail(str(e), code=2)

    with session_scope(factory) as session:
        snap = create_snapshot(session, "ytmusic_liked_songs", items)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored ({len(items)} tracks)."
        )


@scan_app.command("youtube-likes")
def scan_youtube_likes(
    region: Annotated[
        str | None,
        typer.Option(
            "--region",
            callback=_parse_region_flag,
            help=(
                "ISO 3166-1 alpha-2 country code for region-block detection. "
                "Overrides config.json for this scan only; never written to disk."
            ),
        ),
    ] = None,
) -> None:
    """Fetch every YouTube liked video (LL playlist) and store a snapshot.

    Also runs a per-video availability check (`videos.list?part=status,contentDetails`)
    and persists the result as `SnapshotItem.is_available` /
    `unavailable_reason` so `compare-likes` can surface ghost videos.
    Region-aware detection runs when ``region`` is provided either via
    ``--region`` or ``config.json``.
    Afterwards the playlist's ``itemCount`` is compared with the fetched items and a
    shortfall is only warned about: hidden likes can't be fetched, so it can't fail the scan.
    """
    from googleapiclient.errors import HttpError

    from .youtube_client import (
        AuthorizationRequiredError,
        ClientSecretsMissingError,
        YouTubeClient,
        attach_video_statuses,
    )

    cfg, factory = _bootstrap()
    user_region = _resolve_region(region, cfg.region)
    if user_region is None:
        console.print(
            "[yellow]⚠[/yellow] No region configured — region-blocked videos won't "
            "be detected as ghosts. Set [cyan]region[/cyan] in "
            "[cyan]~/.like-surgeon/config.json[/cyan] or pass [cyan]--region <ISO-code>[/cyan]."
        )

    client = YouTubeClient(
        client_secrets_path=cfg.youtube_oauth_client_path,
        token_path=cfg.youtube_token_path,
    )
    try:
        items = client.fetch_liked_videos()
    except (ClientSecretsMissingError, AuthorizationRequiredError) as e:
        _fail(str(e), code=2)
    except HttpError as e:
        _fail(f"YouTube API error while fetching liked videos: {e}", code=1)

    video_ids: list[str] = []
    for it in items:
        snippet = it.get("snippet") or {}
        content = it.get("contentDetails") or {}
        vid = content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        if vid:
            video_ids.append(vid)

    statuses = client.fetch_video_statuses(video_ids, user_region=user_region)
    attach_video_statuses(items, statuses)

    with session_scope(factory) as session:
        snap = create_snapshot(session, "youtube_liked_videos", items)
        scan_items = get_snapshot_items(session, snap.id)
        music_like = sum(1 for it in scan_items if it.is_music_candidate)
        unavailable = sum(1 for it in scan_items if it.is_available is False)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored "
            f"({len(items)} videos, [bold]{music_like}[/bold] music-like, "
            f"[bold]{unavailable}[/bold] unavailable)."
        )

    try:
        playlist_count = client.fetch_likes_item_count()
    except Exception as e:  # noqa: BLE001 — the check is advisory; never fail a stored scan
        console.print(
            "[yellow]⚠[/yellow] could not read the likes playlist count "
            f"({type(e).__name__}: {escape(str(e))}); hidden-likes check skipped"
        )
        return
    if playlist_count > len(items):
        console.print(
            f"[yellow]⚠[/yellow] YouTube counts {playlist_count} liked videos but the API "
            f"returned {len(items)}: {playlist_count - len(items)} like(s) are hidden from "
            "the Data API and from this scan. See "
            "https://github.com/zeikar/like-surgeon/blob/main/docs/notes/hidden-likes.md "
            "to find and re-like them."
        )


@app.command()
def snapshots() -> None:
    """List previous snapshots."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        rows = list_snapshots(session)

    table = Table(title="Snapshots")
    table.add_column("ID", justify="right")
    table.add_column("Source")
    table.add_column("Created (UTC)")
    table.add_column("Items", justify="right")
    for s in rows:
        table.add_row(
            str(s.id),
            s.source,
            s.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            str(s.raw_count),
        )
    if not rows:
        console.print(
            "[yellow]No snapshots yet.[/yellow] Run [cyan]likesurgeon scan ytmusic[/cyan]."
        )
        return
    console.print(table)


@app.command()
def diff(
    old_snapshot_id: Annotated[int, typer.Argument(help="Older snapshot id.")],
    new_snapshot_id: Annotated[int, typer.Argument(help="Newer snapshot id.")],
) -> None:
    """Compare two snapshots and show added/removed tracks."""
    _, factory = _bootstrap()
    try:
        with session_scope(factory) as session:
            result = diff_snapshots(session, old_snapshot_id, new_snapshot_id)
    except ValueError as e:
        _fail(str(e), code=1)

    console.print(
        f"[bold]Common:[/bold] {result.common_count}  "
        f"[bold green]Added:[/bold green] {len(result.added)}  "
        f"[bold red]Removed:[/bold red] {len(result.removed)}"
    )

    if result.added:
        t = Table(title="Added")
        t.add_column("Track ID", justify="right")
        t.add_column("Title")
        t.add_column("Artists")
        t.add_column("Video ID")
        for s in result.added:
            t.add_row(str(s.track_id), s.title, ", ".join(s.artists), s.video_id or "")
        console.print(t)

    if result.removed:
        t = Table(title="Removed")
        t.add_column("Track ID", justify="right")
        t.add_column("Title")
        t.add_column("Artists")
        t.add_column("Video ID")
        for s in result.removed:
            t.add_row(str(s.track_id), s.title, ", ".join(s.artists), s.video_id or "")
        console.print(t)


@app.command()
def export(
    snapshot_id: Annotated[int, typer.Argument(help="Snapshot id to export.")],
    format: Annotated[
        str, typer.Option("--format", "-f", help="Export format. Currently 'json'.")
    ] = "json",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write to file instead of stdout."),
    ] = None,
) -> None:
    """Export a snapshot's tracks."""
    fmt = format.lower()
    if fmt != "json":
        _fail(f"Unsupported format: {format!r}. Currently only 'json' is supported.", code=2)

    _, factory = _bootstrap()
    try:
        with session_scope(factory) as session:
            payload = export_snapshot_json(session, snapshot_id)
    except ValueError as e:
        _fail(str(e), code=1)

    if output is not None:
        output.write_text(payload, encoding="utf-8")
        console.print(f"[green]✓[/green] Wrote [cyan]{output}[/cyan]")
    else:
        # Use the bare print so machine consumers can pipe stdout cleanly.
        print(payload)


@app.command()
def doctor() -> None:
    """Multi-source health summary using stored data."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        report = health_summary(session)

    def _render_source(label: str, sh) -> None:
        if sh.latest_count is None:
            console.print(
                f"[yellow]{label}:[/yellow] no snapshots yet (run the matching scan command first)."
            )
            return
        line = f"[bold]{label}:[/bold] {sh.latest_count} items ({sh.snapshot_count} snapshots)"
        if sh.last_diff is not None:
            line += (
                f"  · vs prev: +{len(sh.last_diff.added)} / "
                f"-{len(sh.last_diff.removed)} / common {sh.last_diff.common_count}"
            )
        console.print(line)

    console.print("[bold]like-surgeon doctor[/bold]")
    _render_source("YouTube Music liked songs", report.ytmusic)
    _render_source("YouTube liked videos", report.youtube)

    if report.latest_diagnosis is None:
        console.print("[dim]No diagnosis yet — run [cyan]likesurgeon compare-likes[/cyan].[/dim]")
    else:
        d = report.latest_diagnosis
        console.print(
            f"[bold]Latest diagnosis #{d.diagnosis_id}:[/bold] "
            + " · ".join(f"{n} {issue_type}" for issue_type, n in d.counts.items())
        )

    if report.lm_backed_percent is None:
        console.print("[dim]LM backed: N/A (no diagnosed YT Music liked songs).[/dim]")
    else:
        score = report.lm_backed_percent
        color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
        console.print(
            f"[bold]LM backed:[/bold] [{color}]{score:.1f}%[/{color}] of YT Music liked songs "
            "traced to the YouTube like behind them"
        )


@app.command("compare-likes")
def compare_likes_cmd() -> None:
    """Align the latest YouTube liked-videos and YT Music liked-songs snapshots."""
    cfg, factory = _bootstrap()
    if cfg.fuzzy_threshold is not None:
        console.print(
            "[yellow]⚠[/yellow] config.json [cyan]fuzzy_threshold[/cyan] is deprecated and "
            "ignored: compare-likes aligns the two lists by order, not by title matching."
        )
    try:
        with session_scope(factory) as session:
            outcome = compare_and_persist(session, cfg)
    except MissingSnapshotsError as e:
        _fail(str(e), code=2)

    for warning in outcome.warnings:
        console.print(warning)
    console.print(f"[green]✓[/green] Diagnosis [bold]#{outcome.diagnosis_id}[/bold] saved.")
    table = Table(title="compare-likes summary")
    table.add_column("Bucket")
    table.add_column("Count", justify="right")
    table.add_row("YT Music liked songs (LM)", str(outcome.lm_count))
    table.add_row("YouTube liked videos (LL)", str(outcome.ll_count))
    table.add_row("LM entries backed by their own video", str(outcome.anchor_count))
    eligible = outcome.findings.eligible
    for issue_type, total in outcome.findings.total.items():
        if issue_type in eligible:
            table.add_row(
                f"{issue_type} (write-eligible / total)", f"{eligible[issue_type]} / {total}"
            )
        else:
            table.add_row(issue_type, str(total))
    console.print(table)
    console.print("Run [cyan]likesurgeon issues[/cyan] for the full per-item breakdown.")


@app.command()
def issues(
    type: Annotated[
        str | None,
        typer.Option(
            "--type",
            help=f"Filter findings by issue type. One of: {' | '.join(ALIGNMENT_ISSUE_TYPES)}.",
        ),
    ] = None,
    min_confidence: Annotated[
        float,
        typer.Option(
            "--min-confidence",
            help="Hide items with confidence below this (0.0–1.0).",
        ),
    ] = 0.0,
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: 'table' (default) or 'json'."),
    ] = "table",
) -> None:
    """List issues from the latest diagnosis."""
    import json as jsonlib

    fmt = format.lower()
    if fmt not in {"table", "json"}:
        _fail(f"Unsupported format: {format!r}. Use 'table' or 'json'.", code=2)
    if type is not None and type not in ALIGNMENT_ISSUE_TYPES:
        _fail(f"Unknown issue type: {type!r}. One of: {', '.join(ALIGNMENT_ISSUE_TYPES)}.", code=2)

    _, factory = _bootstrap()
    with session_scope(factory) as session:
        diag = latest_diagnosis(session)
        if diag is None:
            console.print(
                "[yellow]No diagnosis yet.[/yellow] "
                "Run [cyan]likesurgeon compare-likes[/cyan] first."
            )
            return
        items = diagnosis_items(session, diag.id)

        if type is not None:
            items = [it for it in items if it.issue_type == type]
        items = [it for it in items if it.confidence >= min_confidence]

        # Bulk-fetch the Tracks referenced by the filtered items so we
        # can surface video_id alongside the internal track_id PKs.
        # Done before leaving the session so the lookup can use the
        # same connection.
        track_ids: set[int] = {
            tid
            for it in items
            for tid in (it.source_track_id, it.related_track_id)
            if tid is not None
        }
        video_id_by_track = _video_ids_for_tracks(session, track_ids)

    def _vid(track_id: int | None) -> str | None:
        if track_id is None:
            return None
        return video_id_by_track.get(track_id)

    if fmt == "json":
        payload = {
            "diagnosis_id": diag.id,
            "items": [
                {
                    "id": it.id,
                    "issue_type": it.issue_type,
                    "confidence": it.confidence,
                    "reason": it.reason,
                    "source_track_id": it.source_track_id,
                    "source_video_id": _vid(it.source_track_id),
                    "related_track_id": it.related_track_id,
                    "related_video_id": _vid(it.related_track_id),
                    "status": it.status,
                }
                for it in items
            ],
        }
        print(jsonlib.dumps(payload, indent=2, ensure_ascii=False))
        return

    table = Table(title=f"Diagnosis #{diag.id} — issues ({len(items)})")
    table.add_column("ID", justify="right")
    table.add_column("Type")
    table.add_column("Conf", justify="right")
    table.add_column("Source VID")
    table.add_column("Related VID")
    table.add_column("Status")
    table.add_column("Reason")
    for it in items:
        table.add_row(
            str(it.id),
            it.issue_type,
            f"{it.confidence:.2f}",
            _vid(it.source_track_id) or "",
            _vid(it.related_track_id) or "",
            it.status,
            it.reason,
        )
    if not items:
        console.print("[dim]No issues match the filter.[/dim]")
        return
    console.print(table)


def _set_findings_status(item_ids: list[int], *, to: str) -> None:
    """Flip findings of the latest diagnosis between 'open' and 'skipped'.

    All-or-nothing: every id is validated first, so a typo changes nothing.
    'skipped' carries over to later diagnoses (``carry_over_skipped``), so
    this is the durable way to keep ``sync`` off a finding.
    """
    from .models import DiagnosisItem

    source = "open" if to == "skipped" else "skipped"
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        diag = latest_diagnosis(session)
        if diag is None:
            _fail("No diagnosis yet. Run `likesurgeon compare-likes` first.", code=1)
        errors: list[str] = []
        targets: list[DiagnosisItem] = []
        for item_id in item_ids:
            it = session.get(DiagnosisItem, item_id)
            if it is None or it.diagnosis_id != diag.id:
                errors.append(
                    f"#{item_id} is not a finding of the latest diagnosis #{diag.id} "
                    "(ids come from `likesurgeon issues`)"
                )
            elif it.status not in (source, to):
                errors.append(f"#{item_id} is '{it.status}'; only '{source}' findings can change")
            else:
                targets.append(it)
        if errors:
            _fail("nothing changed:\n  " + "\n  ".join(errors), code=2)
        for it in targets:
            it.status = to
    console.print(f"[green]✓[/green] {len(targets)} finding(s) now '{to}'.")


@app.command()
def skip(
    item_ids: Annotated[
        list[int], typer.Argument(help="Finding ids from `likesurgeon issues` (latest diagnosis).")
    ],
) -> None:
    """Keep `sync` off these findings, across future diagnoses too."""
    _set_findings_status(item_ids, to="skipped")


@app.command()
def unskip(
    item_ids: Annotated[
        list[int], typer.Argument(help="Finding ids from `likesurgeon issues` (latest diagnosis).")
    ],
) -> None:
    """Re-open skipped findings so the next `sync` acts on them again."""
    _set_findings_status(item_ids, to="open")


@app.command()
def sync(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Print the plan and exit without applying any writes.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes/--no-yes",
            help="Skip the interactive confirmation prompt.",
        ),
    ] = False,
    include_playable: Annotated[
        bool,
        typer.Option(
            "--include-playable/--no-include-playable",
            help=(
                "Also act on findings whose YouTube video is still playable (an official "
                "MV, a fan upload): re-point rendered_as_other and unlike a playable "
                "shadow_duplicate. Off by default — it removes a like you made yourself; "
                "keep single findings out with `likesurgeon skip`."
            ),
        ),
    ] = False,
    relike_unrendered: Annotated[
        bool,
        typer.Option(
            "--relike-unrendered/--no-relike-unrendered",
            help=(
                "Also un-like and re-like each unrendered_music video on YouTube so YT Music "
                "picks it up (103 units each). Off by default: every re-like moves that video "
                "to the top of your Liked videos (and of Liked songs, if it then shows). "
                "Oldest first; a video YT Music still doesn't show afterwards is marked "
                "skipped (`likesurgeon unskip` to retry)."
            ),
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=0,
            help=(
                "Process at most N actions this run. Useful for ramped first runs; "
                "the next sync needs a re-scan of both sources (and compare-likes) first."
            ),
        ),
    ] = None,
) -> None:
    """Apply the latest diagnosis's write-eligible findings on YouTube."""
    from datetime import UTC, datetime

    cfg, factory = _bootstrap()
    with session_scope(factory) as session:
        diag = latest_diagnosis(session)
        if diag is None:
            console.print(
                "[yellow]No diagnosis yet.[/yellow] "
                "Run [cyan]likesurgeon compare-likes[/cyan] first."
            )
            raise typer.Exit(1)
        items = diagnosis_items(session, diag.id)
        # Whether each A was playable at scan time decides if its shadow is
        # unliked by default; its position orders the relikes.
        ll = (
            get_snapshot_items(session, diag.youtube_snapshot_id)
            if diag.youtube_snapshot_id
            else []
        )
        actions, skips = plan(
            items,
            resolve_video_ids(session, items),
            {it.track_id: it.is_available for it in ll},
            include_playable=include_playable,
            relike_unrendered=relike_unrendered,
            ll_index={it.track_id: it.position for it in ll},
        )
        if limit is not None and limit < len(actions):
            console.print(
                f"[yellow]--limit {limit}: applying first {limit} of "
                f"{len(actions)} actions.[/yellow]"
            )
            actions = actions[:limit]
        console.print(summarize(actions, skips))
        for warning in diagnosis_staleness(session, diag, now=datetime.now(UTC)):
            console.print(f"[yellow]⚠ Stale diagnosis:[/yellow] {warning}")
        if stranded := sorted(stranded_unliked_video_ids(session)):
            console.print(
                f"[bold red]{len(stranded)} video(s) an earlier sync unliked on YouTube "
                "without confirming the result:[/bold red] "
                f"{', '.join(stranded)}. For each, check whether the song is still in your "
                "YT Music Liked songs; if it isn't, re-like that original YouTube video by hand."
            )
        if attempts := sync_attempts_since_older_scan(session, diag):
            console.print(
                f"[bold red]Refusing to sync:[/bold red] {attempts} sync attempt(s) ran after "
                f"the older of the two scans diagnosis #{diag.id} is built from, so it no longer "
                "describes your likes (and a write between the scans misaligns them). Re-scan "
                "both sources ([cyan]scan youtube-likes[/cyan] and [cyan]scan ytmusic[/cyan]), "
                "then re-run [cyan]compare-likes[/cyan]."
            )
            raise typer.Exit(1)

        if dry_run:
            return

        if not yes and not typer.confirm("Proceed?", default=False):
            raise typer.Abort()

        # Function-local so tests can swap the class on the youtube_client module.
        from .youtube_client import YouTubeClient

        yt = YouTubeClient(
            client_secrets_path=cfg.youtube_oauth_client_path,
            token_path=cfg.youtube_token_path,
        )
        if actions and not yt.has_write_scope():
            console.print(
                "[yellow]YouTube write scope not granted.[/yellow] "
                "Run [cyan]likesurgeon auth youtube[/cyan] to re-grant it."
            )
            raise typer.Exit(1)

        ytm = YTMusicClient(browser_path=cfg.ytmusic_browser_path)
        if actions:
            # Every action is checked against YT Music liked songs read before
            # and after it; cookies from a browser go stale fast, so fail here
            # rather than mid-run.
            try:
                ytm.fetch_liked_songs(limit=1)
            except (AuthFileMissingError, UnexpectedResponseError) as e:
                console.print(f"[yellow]YouTube Music auth check failed:[/yellow] {e}")
                raise typer.Exit(1) from e
        result = execute(session, actions, skips, ytm=ytm, yt=yt)
        # A relike's unlike may land after every read said "liked": its pending row
        # keeps A stranded without a ``left_unliked`` entry, so say so now.
        newly_stranded = sorted(
            stranded_unliked_video_ids(session) - set(stranded) - set(result.left_unliked)
        )

    console.print(
        f"[bold]Sync result:[/bold] "
        f"applied=[green]{result.applied}[/green] "
        f"failed=[red]{result.failed}[/red] "
        f"skipped=[yellow]{result.skipped}[/yellow] "
        f"restored=[yellow]{result.restored}[/yellow]"
    )
    if result.restored:
        b_note = (
            "restored (undoing a B like this run added failed — see below)"
            if result.left_liked
            else "restored (and any B like this run added was removed)"
        )
        console.print(
            f"[yellow]{result.restored} action(s) were undone:[/yellow] the unlike didn't "
            f"take or YT Music liked songs didn't change as expected, so the YouTube like was "
            f"{b_note} and the finding marked "
            "'skipped'. The run stopped there: re-scan both sources and re-run compare-likes "
            "before syncing again. See [cyan]likesurgeon issues[/cyan]."
        )
    if result.quota_exhausted:
        console.print(
            f"[red]YouTube daily quota exhausted — stopped early; {result.unattempted} "
            "action(s) not attempted.[/red] After it resets (midnight Pacific), re-scan both "
            "sources and re-run compare-likes before syncing again."
        )
    if result.aborted:
        console.print(
            f"[red]Stopped early: {escape(result.aborted)}; {result.unattempted} action(s) "
            "not attempted.[/red]"
        )
    if result.left_unliked:
        console.print(
            "[bold red]These videos were unliked on YouTube and re-liking them failed, so "
            "they may be liked nowhere right now:[/bold red] "
            + ", ".join(result.left_unliked)
            + "\nRe-like them on YouTube by hand."
        )
    if newly_stranded:
        console.print(
            "[bold red]These videos were unliked on YouTube and their re-like was confirmed, "
            "but the unlike may still have landed afterwards, so they may be liked "
            "nowhere:[/bold red] "
            + ", ".join(newly_stranded)
            + "\nCheck whether the song is in your YT Music Liked songs; if it isn't, re-like "
            "that original YouTube video by hand. Re-scan YouTube likes to clear this warning."
        )
    if result.left_liked:
        console.print(
            "[bold red]A re-point liked these videos on YouTube, and undoing that like failed "
            "or couldn't be confirmed, so they may still be liked:[/bold red] "
            + ", ".join(result.left_liked)
            + "\nNo song is lost, but unlike them on YouTube by hand if you don't want them."
        )
    if result.failed or result.aborted:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
