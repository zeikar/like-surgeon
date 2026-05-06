"""Typer CLI for likesurgeon. Read-only except for local DB writes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import sessionmaker

from . import __version__
from .config import Config
from .db import init_db, make_engine, make_session_factory, session_scope
from .diff import diff_snapshots
from .doctor import health_summary
from .export import export_snapshot_json
from .snapshot import create_snapshot, list_snapshots
from .ytmusic_client import AuthFileMissingError, UnexpectedResponseError, YTMusicClient

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


def _bootstrap() -> tuple[Config, sessionmaker]:
    """Resolve config, ensure app dir + schema, return a session factory."""
    cfg = Config.load()
    cfg.ensure_app_dir()
    engine = make_engine(cfg.db_path)
    init_db(engine)
    return cfg, make_session_factory(engine)


def _fail(msg: str, code: int = 1) -> NoReturn:
    err_console.print(f"[bold red]Error:[/bold red] {msg}")
    raise typer.Exit(code)


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
def auth_ytmusic() -> None:
    """Print instructions for setting up ytmusicapi browser-header auth.

    OAuth is intentionally not supported in MVP 0.1 — see README roadmap.
    """
    cfg = Config.load()
    cfg.ensure_app_dir()
    target = cfg.ytmusic_browser_path
    console.print("[bold]YouTube Music browser-header setup[/bold]")
    console.print(
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
    cfg = Config.load()
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
) -> None:
    """Fetch YouTube liked videos (LL playlist) and store a snapshot."""
    from .youtube_client import (
        AuthorizationRequiredError,
        ClientSecretsMissingError,
        YouTubeClient,
    )

    cfg, factory = _bootstrap()
    client = YouTubeClient(
        client_secrets_path=cfg.youtube_oauth_client_path,
        token_path=cfg.youtube_token_path,
    )
    try:
        items = client.fetch_liked_videos(limit=limit)
    except (ClientSecretsMissingError, AuthorizationRequiredError) as e:
        _fail(str(e), code=2)

    with session_scope(factory) as session:
        snap = create_snapshot(session, "youtube_liked_videos", items)
        # Quick music-candidate count for the post-scan summary.
        from .snapshot import get_snapshot_items

        scan_items = get_snapshot_items(session, snap.id)
        music_like = sum(1 for it in scan_items if it.is_music_candidate)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored "
            f"({len(items)} videos, [bold]{music_like}[/bold] music-like)."
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
            f"{d.ytmusic_only} YT Music only"
        )

    if report.match_rate_percent is None:
        console.print("[dim]Match-rate health score: N/A (no music candidates).[/dim]")
    else:
        score = report.match_rate_percent
        color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
        console.print(f"[bold]Match-rate health score:[/bold] [{color}]{score:.1f}%[/{color}]")


@app.command("compare-likes")
def compare_likes_cmd() -> None:
    """Compare latest YouTube Music vs. YouTube liked-videos snapshots."""
    from .compare import CompareInput, compare_likes
    from .diagnosis import DiagnosisInput, create_diagnosis
    from .snapshot import get_snapshot_items, latest_snapshot

    _, factory = _bootstrap()
    with session_scope(factory) as session:
        ytm_snap = latest_snapshot(session, source="ytmusic_liked_songs")
        yt_snap = latest_snapshot(session, source="youtube_liked_videos")
        if ytm_snap is None or yt_snap is None:
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

        ytm_items = get_snapshot_items(session, ytm_snap.id)
        yt_items = get_snapshot_items(session, yt_snap.id)

        result = compare_likes(CompareInput(ytmusic=ytm_items, youtube=yt_items))
        diag = create_diagnosis(
            session,
            DiagnosisInput(
                ytmusic_snapshot_id=ytm_snap.id,
                youtube_snapshot_id=yt_snap.id,
                result=result,
            ),
        )

    console.print(f"[green]✓[/green] Diagnosis [bold]#{diag.id}[/bold] saved.")
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
    console.print(table)
    console.print("Run [cyan]likesurgeon issues[/cyan] for the full per-item breakdown.")


@app.command()
def issues(
    type: Annotated[
        str | None,
        typer.Option(
            "--type",
            help=(
                "Filter by issue type "
                "(possibly_missing_from_ytmusic | possible_pointer_drift | "
                "ytmusic_only)."
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
                    "related_track_id": it.related_track_id,
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
    table.add_column("Source TID", justify="right")
    table.add_column("Related TID", justify="right")
    table.add_column("Reason")
    for it in items:
        table.add_row(
            str(it.id),
            it.issue_type,
            f"{it.confidence:.2f}",
            str(it.source_track_id) if it.source_track_id is not None else "",
            str(it.related_track_id) if it.related_track_id is not None else "",
            it.reason,
        )
    if not items:
        console.print("[dim]No issues match the filter.[/dim]")
        return
    console.print(table)


if __name__ == "__main__":
    app()
