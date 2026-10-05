"""The ``compare-likes`` pipeline: align the latest snapshots and persist one Diagnosis.

Kept out of ``cli.py`` so the CLI stays command wiring. Nothing here prints:
the outcome carries warning strings (rich markup) for the CLI to show, and a
missing snapshot raises ``MissingSnapshotsError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.markup import escape
from sqlalchemy.orm import Session

from .align import align
from .compare import CanonicalMetadata, dedupe_by_video_id
from .config import Config
from .diagnosis import (
    FindingCounts,
    build_metadata_drift_items,
    carry_over_skipped,
    count_findings,
    create_alignment_diagnosis,
)
from .drift import detect_drift
from .snapshot import (
    YOUTUBE_LIKED_VIDEOS,
    YTMUSIC_LIKED_SONGS,
    get_snapshot_items,
    latest_snapshot,
    latest_snapshots_for_source,
)
from .sync_preflight import sync_attempts_since_older_scan


class MissingSnapshotsError(Exception):
    """One or both source snapshots are missing; the message names the scans to run."""


@dataclass(frozen=True)
class PipelineResult:
    """What the ``compare-likes`` summary prints. Tests typically only need
    ``.diagnosis_id``. ``lm_count`` / ``ll_count`` are raw snapshot rows, LM
    duplicates included. ``warnings`` are rich-markup lines in the order they arose.
    """

    diagnosis_id: int
    lm_count: int
    ll_count: int
    anchor_count: int
    findings: FindingCounts
    warnings: list[str] = field(default_factory=list)


def _fetch_pair_metadata(
    cfg: Config | None, video_ids: list[str], warnings: list[str]
) -> dict[str, CanonicalMetadata]:
    """``videos.list`` metadata for the aligned pairs' same-recording check.

    Without it every pair is merely report-only, so any failure (no auth,
    HTTP, transport) is reported in one line instead of failing the run.
    """
    # Resolved at call time so tests can patch ``youtube_client.YouTubeClient``.
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
    warnings.append(
        f"[yellow]⚠[/yellow] videos.list metadata unavailable ({escape(reason)}); "
        "every aligned pair is report-only."
    )
    return {}


def compare_and_persist(session: Session, cfg: Config | None = None) -> PipelineResult:
    """Align the latest LL and LM snapshots and persist the findings as one Diagnosis.

    Pipeline:
      1. ``align`` the raw snapshot rows (spec §3). Not deduped: an LM video
         shown twice is how a shadow like behind it shows up.
      2. Fetch ``videos.list`` metadata for both sides of every rendered pair;
         without it (``cfg`` is None, or the fetch fails) pairs are report-only.
      3. Persist the alignment findings; warn when one of our own ``sync``
         attempts ran after the older scan (spec §5.1): the lists may then
         misalign or no longer be current. Then metadata-drift findings per
         source (latest vs previous scan).
      4. Carry earlier diagnoses' 'skipped' statuses forward.
    """
    yt_snap = latest_snapshot(session, source=YOUTUBE_LIKED_VIDEOS)
    ytm_snap = latest_snapshot(session, source=YTMUSIC_LIKED_SONGS)
    if yt_snap is None or ytm_snap is None:
        missing: list[str] = []
        if ytm_snap is None:
            missing.append("[cyan]likesurgeon scan ytmusic[/cyan]")
        if yt_snap is None:
            missing.append("[cyan]likesurgeon scan youtube-likes[/cyan]")
        raise MissingSnapshotsError(
            "Need both a ytmusic_liked_songs and a youtube_liked_videos "
            f"snapshot first. Run: {', '.join(missing)}."
        )

    warnings: list[str] = []
    ll = get_snapshot_items(session, yt_snap.id)
    lm = get_snapshot_items(session, ytm_snap.id)
    result = align(ll, lm)

    pair_vids = sorted(
        {
            vid
            for b in result.backings
            if b.kind == "rendered"
            for vid in (b.ll_item.video_id, b.lm_item.video_id)
            if vid
        }
    )
    metadata = _fetch_pair_metadata(cfg, pair_vids, warnings) if pair_vids else {}
    diagnosis = create_alignment_diagnosis(
        session,
        ytmusic_snapshot_id=ytm_snap.id,
        youtube_snapshot_id=yt_snap.id,
        result=result,
        metadata=metadata,
    )
    if attempts := sync_attempts_since_older_scan(session, diagnosis):
        warnings.append(
            f"[yellow]⚠[/yellow] {attempts} sync attempt(s) ran after the older of the two "
            f"scans (YouTube #{yt_snap.id}, YT Music #{ytm_snap.id}), so this diagnosis may "
            "not match your current likes. Re-scan both sources ([cyan]scan youtube-likes[/cyan]"
            " and [cyan]scan ytmusic[/cyan]), then re-run compare-likes."
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
        warnings.append(f"Kept {carried} finding(s) 'skipped' from the previous diagnosis")
    session.flush()

    return PipelineResult(
        diagnosis_id=diagnosis.id,
        lm_count=len(lm),
        ll_count=len(ll),
        anchor_count=result.anchor_count,
        findings=count_findings(session, diagnosis.id),
        warnings=warnings,
    )
