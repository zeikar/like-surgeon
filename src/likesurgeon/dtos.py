"""Pydantic DTOs for serializing tracks/snapshots over CLI/JSON boundaries."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from .models import Snapshot, SnapshotItem, Track


class TrackDTO(BaseModel):
    """JSON-serializable view of a track.

    Constructed from either:
      * ``from_orm_track`` — *latest* known metadata (master row).
      * ``from_snapshot_item`` — *point-in-time* metadata (snapshot row), with
        ``first_seen_at``/``last_seen_at`` filled from the linked master Track.
        Use this for export so old snapshots return their original metadata.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    video_id: str | None
    title: str
    artists: list[str]
    album: str | None
    duration_seconds: int | None
    thumbnails: list[dict[str, Any]] | None
    canonical_key: str
    first_seen_at: datetime
    last_seen_at: datetime

    @classmethod
    def from_orm_track(cls, track: Track) -> TrackDTO:
        return cls(
            id=track.id,
            source=track.source,
            video_id=track.video_id,
            title=track.title,
            artists=json.loads(track.artists) if track.artists else [],
            album=track.album,
            duration_seconds=track.duration_seconds,
            thumbnails=json.loads(track.thumbnails_json) if track.thumbnails_json else None,
            canonical_key=track.canonical_key,
            first_seen_at=track.first_seen_at,
            last_seen_at=track.last_seen_at,
        )

    @classmethod
    def from_snapshot_item(cls, item: SnapshotItem, source: str) -> TrackDTO:
        """Build a DTO from a snapshot's point-in-time captured metadata.

        ``source`` is taken from the parent ``Snapshot`` (callers already have
        it, so we don't pay an extra DB lookup). ``first_seen_at`` /
        ``last_seen_at`` come from the linked ``Track`` row — eager-load via
        ``selectinload(SnapshotItem.track)`` to avoid N+1 queries.
        """
        return cls(
            id=item.track_id,
            source=source,
            video_id=item.video_id,
            title=item.title,
            artists=json.loads(item.artists) if item.artists else [],
            album=item.album,
            duration_seconds=item.duration_seconds,
            thumbnails=json.loads(item.thumbnails_json) if item.thumbnails_json else None,
            canonical_key=item.canonical_key,
            first_seen_at=item.track.first_seen_at,
            last_seen_at=item.track.last_seen_at,
        )


class SnapshotDTO(BaseModel):
    """JSON-serializable view of a snapshot row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    created_at: datetime
    raw_count: int

    @classmethod
    def from_orm_snapshot(cls, snapshot: Snapshot) -> SnapshotDTO:
        return cls(
            id=snapshot.id,
            source=snapshot.source,
            created_at=snapshot.created_at,
            raw_count=snapshot.raw_count,
        )
