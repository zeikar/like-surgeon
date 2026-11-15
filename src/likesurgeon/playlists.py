"""Load stored playlist scans, and the read-only report over the latest one.

Reads stored snapshots only; makes no provider calls and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PlaylistScan, Snapshot, SnapshotItem
from .snapshot import YOUTUBE_PLAYLIST, YTMUSIC_PLAYLIST

_SKIPPED_IDS = {"LM", "SE"}
_SKIPPED_PREFIXES = ("LRSR", "LRYR")
_MUSIC_TYPES = {
    "MUSIC_VIDEO_TYPE_ATV",
    "MUSIC_VIDEO_TYPE_OMV",
    "MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC",
}


def is_backed_up(playlist_id: str) -> bool:
    """False for generated or system playlists (liked songs, episodes, auto mixes)."""
    return playlist_id not in _SKIPPED_IDS and not playlist_id.startswith(_SKIPPED_PREFIXES)


def music_kind(video_type: str | None) -> str:
    """Bucket a YT Music ``videoType`` as ``music``, ``ugc`` or ``other``."""
    if video_type in _MUSIC_TYPES:
        return "music"
    if video_type == "MUSIC_VIDEO_TYPE_UGC":
        return "ugc"
    return "other"


@dataclass(frozen=True)
class PlaylistRow:
    playlist_id: str
    title: str
    owned: bool | None
    ytmusic_snapshot_id: int
    ytmusic_count: int
    music: int
    ugc: int
    other: int
    music_share: float | None
    # None when the playlist has no YouTube side.
    youtube_count: int | None
    dead: int | None
    unchecked: int | None


@dataclass(frozen=True)
class DeadItem:
    playlist_title: str
    position: int  # 1-based
    video_id: str | None
    title: str
    reason: str | None


@dataclass(frozen=True)
class PlaylistReport:
    scan_id: int
    created_at: datetime
    rows: tuple[PlaylistRow, ...]
    dead_items: tuple[DeadItem, ...]


@dataclass(frozen=True)
class LoadedPlaylist:
    """One playlist of a scan: its YT Music side and, if read, its YouTube side."""

    ytmusic: Snapshot
    ytmusic_items: tuple[SnapshotItem, ...]
    youtube: Snapshot | None
    youtube_items: tuple[SnapshotItem, ...] | None


@dataclass(frozen=True)
class LoadedScan:
    scan: PlaylistScan
    playlists: tuple[LoadedPlaylist, ...]  # in YT Music snapshot id order


def load_scan(session: Session, scan_id: int | None = None) -> LoadedScan | None:
    """Load a playlist scan (the latest by default), or ``None`` if there is none."""
    if scan_id is None:
        scan = session.scalars(
            select(PlaylistScan).order_by(PlaylistScan.created_at.desc(), PlaylistScan.id.desc())
        ).first()
    else:
        scan = session.get(PlaylistScan, scan_id)
    if scan is None:
        return None

    snapshots = session.scalars(
        select(Snapshot).where(Snapshot.playlist_scan_id == scan.id).order_by(Snapshot.id)
    ).all()
    # Snapshots are listed separately so empty playlists still get a row; the
    # items come in one join on the scan id, with no id list to batch.
    item_rows = session.execute(
        select(Snapshot.id, SnapshotItem)
        .join(SnapshotItem, SnapshotItem.snapshot_id == Snapshot.id)
        .where(Snapshot.playlist_scan_id == scan.id)
        .order_by(Snapshot.id, SnapshotItem.position)
    ).all()
    items: dict[int, list[SnapshotItem]] = {}
    for snapshot_id, item in item_rows:
        items.setdefault(snapshot_id, []).append(item)

    youtube_by_playlist = {s.playlist_id: s for s in snapshots if s.source == YOUTUBE_PLAYLIST}
    playlists: list[LoadedPlaylist] = []
    for snap in snapshots:
        if snap.source != YTMUSIC_PLAYLIST:
            continue
        yt_snap = youtube_by_playlist.get(snap.playlist_id)
        playlists.append(
            LoadedPlaylist(
                ytmusic=snap,
                ytmusic_items=tuple(items.get(snap.id, [])),
                youtube=yt_snap,
                youtube_items=None if yt_snap is None else tuple(items.get(yt_snap.id, [])),
            )
        )
    return LoadedScan(scan=scan, playlists=tuple(playlists))


def playlist_report(session: Session) -> PlaylistReport | None:
    """Summarize the latest playlist scan, or ``None`` if none was taken."""
    loaded = load_scan(session)
    if loaded is None:
        return None

    rows: list[PlaylistRow] = []
    dead_items: list[DeadItem] = []
    for playlist in loaded.playlists:
        snap = playlist.ytmusic
        kinds = [music_kind(i.video_type) for i in playlist.ytmusic_items]
        music, ugc = kinds.count("music"), kinds.count("ugc")

        youtube_count = dead = unchecked = None
        # Only owned playlists have a YouTube side; an older scan may hold one for a
        # saved playlist, which must not resurface its dead items.
        if playlist.youtube_items is not None and snap.playlist_owned is True:
            youtube_count = len(playlist.youtube_items)
            dead = unchecked = 0
            for i in playlist.youtube_items:
                if i.is_available is None:
                    unchecked += 1
                elif i.is_available is False:
                    dead += 1
                    dead_items.append(
                        DeadItem(
                            playlist_title=snap.playlist_title,
                            position=i.position + 1,
                            video_id=i.video_id,
                            title=i.title,
                            reason=i.unavailable_reason,
                        )
                    )

        rows.append(
            PlaylistRow(
                playlist_id=snap.playlist_id,
                title=snap.playlist_title,
                owned=snap.playlist_owned,
                ytmusic_snapshot_id=snap.id,
                ytmusic_count=len(kinds),
                music=music,
                ugc=ugc,
                other=len(kinds) - music - ugc,
                music_share=music / len(kinds) if kinds else None,
                youtube_count=youtube_count,
                dead=dead,
                unchecked=unchecked,
            )
        )
    return PlaylistReport(
        scan_id=loaded.scan.id,
        created_at=loaded.scan.created_at,
        rows=tuple(rows),
        dead_items=tuple(dead_items),
    )
