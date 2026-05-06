"""SQLAlchemy ORM models for snapshots, tracks, and snapshot membership."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Project-wide declarative base."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Track(Base):
    """A normalized, deduplicated track record.

    Identity (``source``, ``dedupe_key``):
      - ``dedupe_key`` is the provider's video id when present.
      - Otherwise it falls back to ``"canon:<canonical_key>"``.
    This lets us merge re-likes of the same video and still group cross-source
    matches by canonical key when video ids are missing.
    """

    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(Text)
    artists: Mapped[str] = mapped_column(Text)  # JSON-encoded list[str]
    album: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnails_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_key: Mapped[str] = mapped_column(Text, index=True)
    dedupe_key: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (UniqueConstraint("source", "dedupe_key", name="uq_track_source_dedupe"),)


class Snapshot(Base):
    """A point-in-time capture of liked songs from a single source."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    raw_count: Mapped[int] = mapped_column(Integer)

    items: Mapped[list[SnapshotItem]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class SnapshotItem(Base):
    """Membership row linking a snapshot to a track at a given position.

    **Point-in-time metadata.** ``Track`` holds the *latest* known metadata
    for an identity (and gets overwritten on each scan). The columns below
    capture the values seen *at the time this snapshot was taken*, so that
    exporting an old snapshot returns the metadata from when it was scanned —
    even if the same video has since been renamed, re-credited, or had its
    raw payload reshaped by ytmusicapi. This is the whole point of "backup".
    """

    __tablename__ = "snapshot_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="RESTRICT"))
    position: Mapped[int] = mapped_column(Integer)

    # Point-in-time captured metadata (frozen at scan time).
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(Text)
    artists: Mapped[str] = mapped_column(Text)  # JSON-encoded list[str]
    album: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnails_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_key: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    snapshot: Mapped[Snapshot] = relationship(back_populates="items")
    track: Mapped[Track] = relationship()

    __table_args__ = (
        # We deliberately do NOT constrain ``(snapshot_id, track_id)``: a scan
        # that returns the same dedupe_key twice (rare — only when ``video_id``
        # is absent and two items share a ``canonical_key``) should still be
        # recorded faithfully rather than failing with IntegrityError, since
        # the whole point of a snapshot is to preserve what was scanned.
        # Position uniqueness within a snapshot is enough to keep ordering
        # deterministic and also covers ordered-read queries.
        UniqueConstraint("snapshot_id", "position", name="uq_snapitem_snap_pos"),
    )
