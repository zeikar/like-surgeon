"""Typer CLI for likesurgeon.

Every command only reads the providers and writes the local DB, except
``sync``, which writes likes to YouTube / YT Music.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy.orm import Session, sessionmaker

from . import __version__
from .align import align
from .compare import CanonicalMetadata, dedupe_by_video_id
from .config import Config, InvalidFuzzyThresholdError, InvalidRegionError, _validate_region
from .db import init_db, make_engine, make_session_factory, session_scope
from .diagnosis import (
    ALIGNMENT_ISSUE_TYPES,
    FindingCounts,
    build_metadata_drift_items,
    carry_over_skipped,
    count_findings,
    create_alignment_diagnosis,
    diagnosis_items,
    latest_diagnosis,
)
from .diff import diff_snapshots
from .doctor import health_summary
from .drift import detect_drift
from .export import export_snapshot_json
from .snapshot import (
    YOUTUBE_LIKED_VIDEOS,
    YTMUSIC_LIKED_SONGS,
    create_snapshot,
    get_snapshot_items,
    latest_snapshot,
    latest_snapshots_for_source,
    list_snapshots,
)
from .sync import _TRACK_LOOKUP_BATCH_SIZE, _video_ids_for_tracks  # noqa: F401 — re-exported
from .sync_preflight import sync_attempts_since
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
            "[cyan]uv run likesurgeon scan ytmusic[/cyan]"
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


@dataclass(frozen=True)
class _PipelineResult:
    """What the ``compare-likes`` summary prints. Tests typically only need
    ``.diagnosis_id``. ``lm_count`` / ``ll_count`` are raw snapshot rows, LM
    duplicates included.
    """

    diagnosis_id: int
    lm_count: int
    ll_count: int
    anchor_count: int
    findings: FindingCounts


def _fetch_pair_metadata(cfg: Config | None, video_ids: list[str]) -> dict[str, CanonicalMetadata]:
    """``videos.list`` metadata for the aligned pairs' same-recording check.

    Without it every pair is merely report-only, so any failure (no auth,
    HTTP, transport) is reported in one line instead of failing the run.
    """
    from .youtube_client import (
        AuthorizationRequiredError,
        ClientSecretsMissingError,
        YouTubeClient,
    )

    if cfg is None:
        reason = "no YouTube auth"
    else:
        try:
            return YouTubeClient(
                client_secrets_path=cfg.youtube_oauth_client_path,
                token_path=cfg.youtube_token_path,
            ).fetch_canonical_metadata(video_ids)
        except (AuthorizationRequiredError, ClientSecretsMissingError):
            reason = "no YouTube auth"
        except Exception as exc:  # noqa: BLE001 — boundary catch, see docstring
            reason = f"{type(exc).__name__}: {exc}"
    console.print(
        f"[yellow]⚠[/yellow] videos.list metadata unavailable ({escape(reason)}); "
        "every aligned pair is report-only."
    )
    return {}


def _compare_and_persist(session: Session, cfg: Config | None = None) -> _PipelineResult:
    """Align the latest LL and LM snapshots and persist the findings as one Diagnosis.

    Pipeline:
      1. ``align`` the raw snapshot rows (spec §3). Not deduped: an LM video
         shown twice is how a shadow like behind it shows up.
      2. Warn when one of our own ``sync`` attempts ran after the older scan
         (spec §5.1): the lists may then misalign or no longer be current.
      3. Fetch ``videos.list`` metadata for both sides of every rendered pair;
         without it (``cfg`` is None, or the fetch fails) pairs are report-only.
      4. Persist the alignment findings, then metadata-drift findings per
         source (latest vs previous scan).
      5. Carry earlier diagnoses' 'skipped' statuses forward.
    """
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

    ll = get_snapshot_items(session, yt_snap.id)
    lm = get_snapshot_items(session, ytm_snap.id)
    result = align(ll, lm)

    # Both times are UTC, but SQLite hands them back naive while a row created
    # in this session is still aware, so compare them naive.
    older_scan = min(s.created_at.replace(tzinfo=None) for s in (yt_snap, ytm_snap))
    if attempts := sync_attempts_since(session, older_scan):
        console.print(
            f"[yellow]⚠[/yellow] {attempts} sync attempt(s) ran after the older of the two "
            f"scans (YouTube #{yt_snap.id}, YT Music #{ytm_snap.id}), so this diagnosis may "
            "not match your current likes. Re-scan both sources ([cyan]scan youtube-likes[/cyan]"
            " and [cyan]scan ytmusic[/cyan]), then re-run compare-likes."
        )

    pair_vids = sorted(
        {
            vid
            for b in result.backings
            if b.kind == "rendered"
            for vid in (b.ll_item.video_id, b.lm_item.video_id)
            if vid
        }
    )
    metadata = _fetch_pair_metadata(cfg, pair_vids) if pair_vids else {}
    diagnosis = create_alignment_diagnosis(
        session,
        ytmusic_snapshot_id=ytm_snap.id,
        youtube_snapshot_id=yt_snap.id,
        result=result,
        metadata=metadata,
    )

    # Metadata drift per source against (latest, prev), each video_id once.
    for source in (YOUTUBE_LIKED_VIDEOS, YTMUSIC_LIKED_SONGS):
        snaps = latest_snapshots_for_source(session, source, limit=2)
        if len(snaps) < 2:
            continue
        curr_snap, prev_snap = snaps[0], snaps[1]
        curr_items = dedupe_by_video_id(get_snapshot_items(session, curr_snap.id))
        prev_items = dedupe_by_video_id(get_snapshot_items(session, prev_snap.id))
        findings = detect_drift(prev_items, curr_items, source=source)
        session.add_all(build_metadata_drift_items(diagnosis.id, findings, curr_items))

    session.flush()
    # Keep earlier terminal 'skipped' findings skipped, so a verify-miss or
    # manual suppression survives this re-run.
    carried = carry_over_skipped(session, diagnosis)
    if carried:
        console.print(f"Kept {carried} finding(s) 'skipped' from the previous diagnosis")
    session.flush()

    return _PipelineResult(
        diagnosis_id=diagnosis.id,
        lm_count=len(lm),
        ll_count=len(ll),
        anchor_count=result.anchor_count,
        findings=count_findings(session, diagnosis.id),
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
    with session_scope(factory) as session:
        outcome = _compare_and_persist(session, cfg)

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
    drift_min_confidence: Annotated[
        float,
        typer.Option(
            "--drift-min-confidence",
            min=0.0,
            max=1.0,
            help="Auto-apply pointer-drift fixes only when confidence ≥ this value.",
        ),
    ] = 0.95,
    include_fuzzy_drift: Annotated[
        bool,
        typer.Option(
            "--include-fuzzy-drift/--no-include-fuzzy-drift",
            help=(
                "Also auto-apply title-only fuzzy pointer drifts. Off by default: a "
                "version variant like 'Song (Remix)' scores 1.0 against 'Song', so "
                "--drift-min-confidence does NOT filter those out, and a wrong pair "
                "unlikes your original. (Stage-4 drifts are always 0.95, so a "
                "threshold above 0.95 skips them and keeps only fuzzy ones.)"
            ),
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=0,
            help=(
                "Process at most N actions this run; the rest stay 'open' "
                "for the next sync. Useful for ramped first runs."
            ),
        ),
    ] = None,
) -> None:
    """Apply the latest diagnosis's actionable findings to YouTube / YT Music."""
    from datetime import UTC, datetime

    from .diagnosis import diagnosis_items, latest_diagnosis
    from .sync import execute, plan, resolve_video_ids, summarize
    from .sync_preflight import (
        diagnosis_staleness,
        stranded_unliked_video_ids,
        youtube_liked_video_ids,
    )

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
        actions, skips = plan(
            items,
            video_ids,
            drift_min_confidence=drift_min_confidence,
            include_fuzzy_drift=include_fuzzy_drift,
            youtube_liked_video_ids=youtube_liked_video_ids(session, diag),
        )
        # Videos an earlier run unliked but couldn't re-like are liked nowhere;
        # their ytm_like goes first so --limit / a quota stop can't starve it.
        stranded = stranded_unliked_video_ids(session)
        planned_relikes = {a.primary_video_id for a in actions if a.kind == "ytm_like"}
        actions.sort(key=lambda a: not (a.kind == "ytm_like" and a.primary_video_id in stranded))
        if limit is not None and limit < len(actions):
            console.print(
                f"[yellow]--limit {limit}: applying first {limit} of "
                f"{len(actions)} actions; rest stay open for next run.[/yellow]"
            )
            actions = actions[:limit]
        console.print(summarize(actions, skips))
        for warning in diagnosis_staleness(session, diag, now=datetime.now(UTC)):
            console.print(f"[yellow]⚠ Stale diagnosis:[/yellow] {warning}")
        if pending := sorted(stranded & planned_relikes):
            console.print(
                f"[bold red]{len(pending)} video(s) an earlier sync unliked on YouTube but "
                "couldn't re-like are liked nowhere right now:[/bold red] "
                f"{', '.join(pending)}. This sync re-likes them first — run it before "
                "re-scanning (a re-scan would drop them from the diff)."
            )
        if lost := sorted(stranded - planned_relikes):
            console.print(
                f"[bold red]{len(lost)} video(s) an earlier sync left unliked are not in "
                "this diagnosis[/bold red] (re-scanned since?): "
                f"{', '.join(lost)}. Re-like them on YouTube by hand."
            )

        if dry_run:
            return

        if not yes and not typer.confirm("Proceed?", default=False):
            raise typer.Abort()

        # Always construct (cheap); only the write-scope check and write method calls are
        # gated on needs_youtube.
        # ytm_dedupe is ytmusic-only; not in needs_youtube.
        # ytm_like uses YouTube rate_video (unlike+relike) to cross-prop into LM,
        # so it needs YouTube write scope too.
        # yt_like cross-props in the reverse direction (YT Music → YouTube) via
        # videos.rate("like") + verify, so it also needs YouTube write scope.
        needs_youtube = any(
            a.kind in {"yt_unlike", "yt_relike", "ytm_like", "yt_like"} for a in actions
        )
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
        if any(a.kind in {"ytm_dedupe", "ytm_like"} for a in actions):
            # A real read, not just a file check: with missing or expired
            # cookies every ytm_like would toggle the YouTube like and then
            # fail its verify, and every ytm_dedupe would "fail" into the
            # terminal 'applied' status having done nothing.
            try:
                ytm.fetch_liked_songs(limit=1)
            except (AuthFileMissingError, UnexpectedResponseError) as e:
                console.print(f"[yellow]YouTube Music auth check failed:[/yellow] {e}")
                raise typer.Exit(1) from e
        result = execute(session, actions, skips, ytm=ytm, yt=yt)

    console.print(
        f"[bold]Sync result:[/bold] "
        f"applied=[green]{result.applied}[/green] "
        f"failed=[red]{result.failed}[/red] "
        f"skipped=[yellow]{result.skipped}[/yellow]"
    )
    if result.quota_exhausted:
        console.print(
            f"[red]YouTube daily quota exhausted — stopped early; {result.unattempted} "
            "action(s) left open.[/red] Re-run sync after the quota resets "
            "(midnight Pacific)."
        )
    if result.left_unliked:
        console.print(
            "[bold red]These videos were unliked on YouTube but the re-like failed, so "
            "they are liked nowhere right now:[/bold red] "
            + ", ".join(result.left_unliked)
            + "\nRe-run [cyan]likesurgeon sync[/cyan] on this same diagnosis (do NOT "
            "re-scan first — they'd drop out of the diff) or re-like them by hand."
        )
    if result.failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
