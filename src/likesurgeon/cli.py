"""Typer CLI for likesurgeon. Read-only except for local DB writes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session, sessionmaker

from . import __version__
from .compare import CompareInput, CompareResult, compare_likes
from .config import Config, InvalidRegionError, _validate_region
from .db import init_db, make_engine, make_session_factory, session_scope
from .diff import diff_snapshots
from .doctor import health_summary
from .export import export_snapshot_json
from .snapshot import create_snapshot, list_snapshots
from .sync import _TRACK_LOOKUP_BATCH_SIZE, _video_ids_for_tracks  # noqa: F401 — re-exported
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
    help="Sync, backup, and repair your YouTube Music liked songs.",
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
    """Wrap ``Config.load`` so a ``config.json`` typo on the ``region``
    key fails fast with a friendly exit-2 instead of a traceback.

    Used by every CLI entrypoint that reads config — ``_bootstrap`` for
    DB-backed commands plus the ``auth`` setup commands that don't need
    a session. CLI flag validation lives elsewhere in the Typer callback
    ``_parse_region_flag``.
    """
    try:
        return Config.load()
    except InvalidRegionError as e:
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
            "6. Re-run [cyan]uv run likesurgeon auth youtube[/cyan]."
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


@scan_app.command("ytmusic")
def scan_ytmusic(
    limit: Annotated[int, typer.Option(help="Maximum number of liked songs to fetch.")] = 5000,
) -> None:
    """Fetch YouTube Music liked songs and store a snapshot."""
    cfg, factory = _bootstrap()
    client = YTMusicClient(browser_path=cfg.ytmusic_browser_path)
    try:
        items = client.fetch_liked_songs(limit=limit)
    except (AuthFileMissingError, UnexpectedResponseError) as e:
        _fail(str(e), code=2)

    with session_scope(factory) as session:
        snap = create_snapshot(session, "ytmusic_liked_songs", items)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored ({len(items)} tracks)."
        )


@scan_app.command("youtube-likes")
def scan_youtube_likes(
    limit: Annotated[int, typer.Option(help="Maximum number of liked videos to fetch.")] = 5000,
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
    """Fetch YouTube liked videos (LL playlist) and store a snapshot.

    Also runs a per-video availability check (`videos.list?part=status,contentDetails`)
    and persists the result as `SnapshotItem.is_available` /
    `unavailable_reason` so `compare-likes` can surface ghost videos.
    Region-aware detection runs when ``region`` is provided either via
    ``--region`` or ``config.json``.
    """
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
        items = client.fetch_liked_videos(limit=limit)
    except (ClientSecretsMissingError, AuthorizationRequiredError) as e:
        _fail(str(e), code=2)

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
        from .snapshot import get_snapshot_items

        scan_items = get_snapshot_items(session, snap.id)
        music_like = sum(1 for it in scan_items if it.is_music_candidate)
        unavailable = sum(1 for it in scan_items if it.is_available is False)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored "
            f"({len(items)} videos, [bold]{music_like}[/bold] music-like, "
            f"[bold]{unavailable}[/bold] unavailable)."
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
            f"{d.possibly_missing_from_ytmusic} possibly missing from YT Music · "
            f"{d.pointer_drift} pointer drift · "
            f"{d.ytmusic_only} YT Music only · "
            f"{d.unavailable_videos} unavailable videos · "
            f"{d.metadata_drift} metadata drift"
        )

    if report.match_rate_percent is None:
        console.print("[dim]Match-rate health score: N/A (no music candidates).[/dim]")
    else:
        score = report.match_rate_percent
        color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
        console.print(f"[bold]Match-rate health score:[/bold] [{color}]{score:.1f}%[/{color}]")


@dataclass(frozen=True)
class _PipelineResult:
    """Compound return value for `_compare_and_persist` so the CLI wrapper
    can render the existing summary table AND the two new finding-type
    rows from a single call. Tests typically only need `.diagnosis_id`.
    """

    diagnosis_id: int
    compare_result: CompareResult
    unavailable_count: int
    drift_count: int


def _compare_and_persist(session: Session) -> _PipelineResult:
    """Run the full 0.3 compare-likes pipeline against the current session
    and return the persisted Diagnosis id plus the finding counts the CLI
    summary needs.

    Pipeline:
      1. Cross-source matcher (existing 0.2 buckets) → CompareResult.
      2. Persist a Diagnosis with the existing buckets via `create_diagnosis`.
      3. Append ghost findings (latest YouTube snapshot's is_available=False rows).
      4. Append drift findings per source (latest, prev) via `detect_drift`.
      All findings live on a single Diagnosis row.
    """
    from .diagnosis import (
        DiagnosisInput,
        build_metadata_drift_items,
        build_unavailable_video_items,
        create_diagnosis,
    )
    from .drift import detect_drift
    from .snapshot import (
        YOUTUBE_LIKED_VIDEOS,
        YTMUSIC_LIKED_SONGS,
        get_snapshot_items,
        latest_snapshot,
        latest_snapshots_for_source,
    )

    yt_snap = latest_snapshot(session, source=YOUTUBE_LIKED_VIDEOS)
    ytm_snap = latest_snapshot(session, source=YTMUSIC_LIKED_SONGS)
    if yt_snap is None or ytm_snap is None:
        missing: list[str] = []
        if ytm_snap is None:
            missing.append("[cyan]likesurgeon scan ytmusic[/cyan]")
        if yt_snap is None:
            missing.append("[cyan]likesurgeon scan youtube-likes[/cyan]")
        _fail(
            "Need both a ytmusic_liked_songs and a youtube_liked_videos "
            f"snapshot first. Run: {', '.join(missing)}.",
            code=2,
        )

    yt_items = get_snapshot_items(session, yt_snap.id)
    ytm_items = get_snapshot_items(session, ytm_snap.id)

    # Stage A — existing cross-source matcher.
    cmp_result = compare_likes(CompareInput(ytmusic=ytm_items, youtube=yt_items))

    # Stage B — persist Diagnosis + the existing 0.2 finding buckets.
    diagnosis = create_diagnosis(
        session,
        DiagnosisInput(
            ytmusic_snapshot_id=ytm_snap.id,
            youtube_snapshot_id=yt_snap.id,
            result=cmp_result,
        ),
    )

    # Stage C — append ghost findings.
    ghost_items = build_unavailable_video_items(diagnosis.id, yt_items)
    for it in ghost_items:
        session.add(it)

    # Stage D — append drift findings per source against (latest, prev).
    drift_total = 0
    for source in (YOUTUBE_LIKED_VIDEOS, YTMUSIC_LIKED_SONGS):
        snaps = latest_snapshots_for_source(session, source, limit=2)
        if len(snaps) < 2:
            continue
        curr_snap, prev_snap = snaps[0], snaps[1]
        curr_items = get_snapshot_items(session, curr_snap.id)
        prev_items = get_snapshot_items(session, prev_snap.id)
        findings = detect_drift(prev_items, curr_items, source=source)
        drift_items = build_metadata_drift_items(diagnosis.id, findings, curr_items)
        drift_total += len(drift_items)
        for it in drift_items:
            session.add(it)

    session.flush()
    return _PipelineResult(
        diagnosis_id=diagnosis.id,
        compare_result=cmp_result,
        unavailable_count=len(ghost_items),
        drift_count=drift_total,
    )


@app.command("compare-likes")
def compare_likes_cmd() -> None:
    """Compare latest YouTube Music vs. YouTube liked-videos snapshots."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        outcome = _compare_and_persist(session)

    result = outcome.compare_result
    console.print(f"[green]✓[/green] Diagnosis [bold]#{outcome.diagnosis_id}[/bold] saved.")
    table = Table(title="compare-likes summary")
    table.add_column("Bucket")
    table.add_column("Count", justify="right")
    table.add_row("YouTube Music liked songs", str(result.ytmusic_count))
    table.add_row("YouTube liked videos (total)", str(result.youtube_total_count))
    table.add_row("YouTube liked videos (music-like)", str(result.youtube_music_count))
    table.add_row("Matched (any stage)", str(len(result.matched)))
    table.add_row(
        "Possibly missing from YT Music",
        str(len(result.possibly_missing_from_ytmusic)),
    )
    table.add_row(
        "YT Music only (not liked on YouTube)",
        str(len(result.ytmusic_only_likes)),
    )
    table.add_row("Pointer-drift candidates", str(len(result.pointer_drift_candidates)))
    table.add_row("Unavailable videos (ghost)", str(outcome.unavailable_count))
    table.add_row("Metadata drift candidates", str(outcome.drift_count))
    console.print(table)
    console.print("Run [cyan]likesurgeon issues[/cyan] for the full per-item breakdown.")


@app.command()
def issues(
    type: Annotated[
        str | None,
        typer.Option(
            "--type",
            help=(
                "Filter findings by issue type. One of: "
                "possibly_missing_from_ytmusic | possible_pointer_drift | "
                "ytmusic_only | unavailable_video | metadata_drift."
            ),
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

    from .diagnosis import diagnosis_items, latest_diagnosis

    fmt = format.lower()
    if fmt not in {"table", "json"}:
        _fail(f"Unsupported format: {format!r}. Use 'table' or 'json'.", code=2)

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
    table.add_column("Reason")
    for it in items:
        table.add_row(
            str(it.id),
            it.issue_type,
            f"{it.confidence:.2f}",
            _vid(it.source_track_id) or "",
            _vid(it.related_track_id) or "",
            it.reason,
        )
    if not items:
        console.print("[dim]No issues match the filter.[/dim]")
        return
    console.print(table)


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
    drift_min_confidence: Annotated[
        float,
        typer.Option(
            "--drift-min-confidence",
            help="Auto-apply pointer-drift fixes only when confidence ≥ this value.",
        ),
    ] = 0.95,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help=(
                "Process at most N actions this run; the rest stay 'open' "
                "for the next sync. Useful for ramped first runs."
            ),
        ),
    ] = None,
) -> None:
    """Apply the latest diagnosis's actionable findings to YouTube / YT Music."""
    from .diagnosis import diagnosis_items, latest_diagnosis
    from .sync import execute, plan, resolve_video_ids, summarize

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
        video_ids = resolve_video_ids(session, items)
        actions, skips = plan(items, video_ids, drift_min_confidence=drift_min_confidence)
        if limit is not None and limit < len(actions):
            console.print(
                f"[yellow]--limit {limit}: applying first {limit} of "
                f"{len(actions)} actions; rest stay open for next run.[/yellow]"
            )
            actions = actions[:limit]
        console.print(summarize(actions, skips))

        if dry_run:
            return

        if not yes and not typer.confirm("Proceed?", default=False):
            raise typer.Abort()

        # Build the YouTube client only if the plan needs it. ytm-only
        # runs (no yt_unlike / yt_relike actions) skip the scope check
        # entirely so a user without the YouTube write scope can still
        # apply the YT Music half.
        needs_youtube = any(a.kind in {"yt_unlike", "yt_relike"} for a in actions)
        from .youtube_client import YouTubeClient

        yt = YouTubeClient(
            client_secrets_path=cfg.youtube_oauth_client_path,
            token_path=cfg.youtube_token_path,
        )
        if needs_youtube and not yt.has_write_scope():
            console.print(
                "[yellow]YouTube write scope not granted.[/yellow] "
                "Run [cyan]likesurgeon auth youtube[/cyan] to re-grant it."
            )
            raise typer.Exit(1)

        ytm = YTMusicClient(browser_path=cfg.ytmusic_browser_path)
        result = execute(session, actions, skips, ytm=ytm, yt=yt)

    console.print(
        f"[bold]Sync result:[/bold] "
        f"applied=[green]{result.applied}[/green] "
        f"failed=[red]{result.failed}[/red] "
        f"skipped=[yellow]{result.skipped}[/yellow]"
    )
    if result.failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
