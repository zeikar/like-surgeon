"""Snapshot ingestion and retrieval.

This module is the only writer of ``Track`` and ``Snapshot`` rows. Callers
hand it raw provider dicts; ``_TRANSLATORS`` dispatches to the source's
translator, which normalizes the dict into a ``record`` shape that
``_upsert_track`` and ``create_snapshot`` consume.

**Point-in-time guarantee.** ``Track`` holds the *latest* known metadata
(overwritten on every scan), but ``SnapshotItem`` freezes the values seen
at scan time — including the music-candidate classifier output for sources
that aren't guaranteed-music (i.e. youtube_liked_videos).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .classify import classify
from .models import Snapshot, SnapshotItem, Track
from .normalize import canonical_key

YTMUSIC_LIKED_SONGS = "ytmusic_liked_songs"
YOUTUBE_LIKED_VIDEOS = "youtube_liked_videos"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_duration(text: str) -> int | None:
    """Parse ``M:SS`` or ``H:MM:SS`` into seconds; ``None`` if unparseable."""
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _coerce_artists(raw: Any) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                out.append(str(name))
        elif entry:
            out.append(str(entry))
    return out


def _ytmusic_to_record(item: dict[str, Any]) -> dict[str, Any]:
    """Translate one ytmusicapi liked-song dict to a normalized track record."""
    video_id = item.get("videoId")
    title = str(item.get("title") or "")
    artists = _coerce_artists(item.get("artists"))
    album_obj = item.get("album")
    album = album_obj.get("name") if isinstance(album_obj, dict) else None

    duration_seconds = item.get("duration_seconds")
    if duration_seconds is None and item.get("duration"):
        duration_seconds = _parse_duration(str(item["duration"]))

    canon = canonical_key(title, artists)
    dedupe = video_id or f"canon:{canon}"

    return {
        "video_id": video_id,
        "title": title,
        "artists": artists,
        "album": album,
        "duration_seconds": duration_seconds,
        "thumbnails": item.get("thumbnails"),
        "canonical_key": canon,
        "dedupe_key": dedupe,
        "raw": item,
        "is_music_candidate": None,  # ytmusic source: every track is music
        "music_candidate_score": None,
        "music_candidate_reason": None,
    }


def _youtube_to_record(item: dict[str, Any]) -> dict[str, Any]:
    """Translate one YouTube playlistItems.list entry to a track record.

    The YouTube API shape:
      {
        "snippet": {
            "title": ..., "channelTitle": ..., "description": ...,
            "thumbnails": {...}, "publishedAt": ...,
            "resourceId": {"videoId": ...},
        },
        "contentDetails": {"videoId": ..., "videoPublishedAt": ...},
        ...
      }
    Channel name maps to ``artists=[channel]`` so cross-source matching can
    use the same canonical key shape. ``description`` and ``publishedAt``
    survive in ``raw_json`` but don't get top-level columns.
    """
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}

    video_id = (
        content_details.get("videoId")
        or (snippet.get("resourceId") or {}).get("videoId")
    )
    title = str(snippet.get("title") or "")
    channel = str(snippet.get("channelTitle") or "").strip()
    artists = [channel] if channel else []
    description = snippet.get("description") or ""

    canon = canonical_key(title, artists)
    dedupe = video_id or f"canon:{canon}"

    classification = classify(title=title, channel=channel, description=description)

    return {
        "video_id": video_id,
        "title": title,
        "artists": artists,
        "album": None,
        "duration_seconds": None,
        "thumbnails": (snippet.get("thumbnails") or None),
        "canonical_key": canon,
        "dedupe_key": dedupe,
        "raw": item,
        "is_music_candidate": classification.is_music_candidate,
        "music_candidate_score": classification.score,
        "music_candidate_reason": classification.reason,
    }


_TRANSLATORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    YTMUSIC_LIKED_SONGS: _ytmusic_to_record,
    YOUTUBE_LIKED_VIDEOS: _youtube_to_record,
}


def _upsert_track(session: Session, source: str, rec: dict[str, Any]) -> Track:
    stmt = select(Track).where(
        Track.source == source, Track.dedupe_key == rec["dedupe_key"]
    )
    track = session.scalar(stmt)
    now = _utcnow()
    if track is None:
        track = Track(
            source=source,
            video_id=rec["video_id"],
            title=rec["title"],
            artists=json.dumps(rec["artists"], ensure_ascii=False),
            album=rec["album"],
            duration_seconds=rec["duration_seconds"],
            thumbnails_json=(
                json.dumps(rec["thumbnails"], ensure_ascii=False)
                if rec["thumbnails"]
                else None
            ),
            canonical_key=rec["canonical_key"],
            dedupe_key=rec["dedupe_key"],
            raw_json=json.dumps(rec["raw"], ensure_ascii=False),
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(track)
        session.flush()
        return track

    track.title = rec["title"]
    track.artists = json.dumps(rec["artists"], ensure_ascii=False)
    track.album = rec["album"]
    track.duration_seconds = rec["duration_seconds"]
    track.thumbnails_json = (
        json.dumps(rec["thumbnails"], ensure_ascii=False) if rec["thumbnails"] else None
    )
    track.canonical_key = rec["canonical_key"]
    track.raw_json = json.dumps(rec["raw"], ensure_ascii=False)
    track.last_seen_at = now
    return track


def create_snapshot(
    session: Session, source: str, items: Iterable[dict[str, Any]]
) -> Snapshot:
    """Persist a snapshot of ``items`` from ``source``. Returns the snapshot row."""
    if source not in _TRANSLATORS:
        raise ValueError(
            f"Unknown source {source!r}; expected one of {sorted(_TRANSLATORS)}"
        )
    translator = _TRANSLATORS[source]

    items_list = list(items)
    snapshot = Snapshot(source=source, created_at=_utcnow(), raw_count=len(items_list))
    session.add(snapshot)
    session.flush()
    for position, item in enumerate(items_list):
        rec = translator(item)
        track = _upsert_track(session, source, rec)
        session.add(
            SnapshotItem(
                snapshot_id=snapshot.id,
                track_id=track.id,
                position=position,
                video_id=rec["video_id"],
                title=rec["title"],
                artists=json.dumps(rec["artists"], ensure_ascii=False),
                album=rec["album"],
                duration_seconds=rec["duration_seconds"],
                thumbnails_json=(
                    json.dumps(rec["thumbnails"], ensure_ascii=False)
                    if rec["thumbnails"]
                    else None
                ),
                canonical_key=rec["canonical_key"],
                raw_json=json.dumps(rec["raw"], ensure_ascii=False),
                is_music_candidate=rec["is_music_candidate"],
                music_candidate_score=rec["music_candidate_score"],
                music_candidate_reason=rec["music_candidate_reason"],
            )
        )
    session.flush()
    return snapshot


def list_snapshots(session: Session) -> list[Snapshot]:
    stmt = select(Snapshot).order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
    return list(session.scalars(stmt).all())


def get_snapshot(session: Session, snapshot_id: int) -> Snapshot | None:
    return session.get(Snapshot, snapshot_id)


def get_snapshot_items(session: Session, snapshot_id: int) -> list[SnapshotItem]:
    """Return point-in-time snapshot rows in scan order, with master Track eager-loaded."""
    stmt = (
        select(SnapshotItem)
        .where(SnapshotItem.snapshot_id == snapshot_id)
        .order_by(SnapshotItem.position)
        .options(selectinload(SnapshotItem.track))
    )
    return list(session.scalars(stmt).all())


def latest_snapshot(session: Session, source: str | None = None) -> Snapshot | None:
    stmt = select(Snapshot)
    if source is not None:
        stmt = stmt.where(Snapshot.source == source)
    stmt = stmt.order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1)
    return session.scalar(stmt)
