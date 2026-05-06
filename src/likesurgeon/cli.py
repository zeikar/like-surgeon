"""Typer CLI for likesurgeon. Read-only except for local DB writes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

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
from .ytmusic_client import AuthFileMissingError, YTMusicClient

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


def _fail(msg: str, code: int = 1) -> None:
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


@scan_app.command("ytmusic")
def scan_ytmusic(
    limit: Annotated[int, typer.Option(help="Maximum number of liked songs to fetch.")] = 5000,
) -> None:
    """Fetch YouTube Music liked songs and store a snapshot."""
    cfg, factory = _bootstrap()
    client = YTMusicClient(browser_path=cfg.ytmusic_browser_path)
    try:
        items = client.fetch_liked_songs(limit=limit)
    except AuthFileMissingError as e:
        _fail(str(e), code=2)

    with session_scope(factory) as session:
        snap = create_snapshot(session, "ytmusic", items)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored ({len(items)} tracks)."
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
    """Print a basic health summary using stored data."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        report = health_summary(session, source="ytmusic")

    console.print(f"[bold]Total snapshots:[/bold] {report.total_snapshots}")
    if report.latest_source is None:
        console.print(
            "[yellow]No snapshots yet.[/yellow] "
            "Run [cyan]likesurgeon scan ytmusic[/cyan] to capture your liked songs."
        )
        return
    console.print(f"[bold]Latest source:[/bold] {report.latest_source}")
    console.print(f"[bold]Latest liked songs count:[/bold] {report.latest_count}")
    if report.last_diff is None:
        console.print("[dim]No previous snapshot to compare against yet.[/dim]")
    else:
        console.print(
            f"[bold]Compared to previous:[/bold] "
            f"+{len(report.last_diff.added)} added, "
            f"-{len(report.last_diff.removed)} removed, "
            f"{report.last_diff.common_count} common."
        )


if __name__ == "__main__":
    app()
