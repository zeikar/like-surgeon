"""SQLAlchemy ORM models for snapshots, tracks, and snapshot membership."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
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

    # Music-candidate classifier output (Task 2). Populated by sources whose
    # items aren't guaranteed-music (i.e. youtube_liked_videos). NULL for
    # ytmusic_liked_songs since those are always music by definition.
    is_music_candidate: Mapped[bool | None] = mapped_column(nullable=True)
    music_candidate_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    music_candidate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ghost detection (0.3): availability of the underlying YouTube video at
    # scan time. ``None`` means "unknown" (snapshot taken before 0.3, or
    # status check failed). Only populated for ``youtube_liked_videos`` rows.
    is_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    unavailable_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

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


class Diagnosis(Base):
    """A persisted ``compare-likes`` run."""

    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ytmusic_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True
    )
    youtube_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True
    )

    items: Mapped[list[DiagnosisItem]] = relationship(
        back_populates="diagnosis", cascade="all, delete-orphan"
    )


class DiagnosisItem(Base):
    """A single finding produced by a ``compare-likes`` run.

    ``issue_type`` is one of:
      - ``possibly_missing_from_ytmusic`` — music candidate liked on YouTube
        but with no corresponding YT Music track (the high-priority bucket).
      - ``possible_pointer_drift`` — fuzzy match between sources, neither
        ``video_id`` nor ``canonical_key`` exact.
      - ``ytmusic_only`` — YT Music has it but no YT like (informational —
        often just "user never liked it on YouTube").
      - ``unavailable_video`` — the underlying YouTube video is no longer
        playable (deleted/private/unavailable), detected at scan time via
        ``videos.list``.
      - ``metadata_drift`` — the same ``video_id`` appears in two snapshots
        of one source with meaningfully different title or artists.
      - ``duplicate_in_source`` — the same ``video_id`` appears more than
        once in a single snapshot (ytmusic accumulates these over time).
        Actionable via ``ytm_dedupe`` (ytmusic source, N=2 only) as of 0.5.
        Non-idempotent — ``status='applied'`` is set after attempt regardless
        of outcome (carve-out from the standard 'applied = full success'
        invariant; see ``sync.execute()`` docstring).
    """

    __tablename__ = "diagnosis_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    diagnosis_id: Mapped[int] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="CASCADE"), index=True
    )
    issue_type: Mapped[str] = mapped_column(String(64), index=True)
    confidence: Mapped[float] = mapped_column()  # SQLAlchemy maps Python float -> SQLite REAL
    reason: Mapped[str] = mapped_column(Text)
    source_track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True
    )
    related_track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="open")

    diagnosis: Mapped[Diagnosis] = relationship(back_populates="items")
    sync_attempts: Mapped[list[SyncAttempt]] = relationship(
        back_populates="diagnosis_item", cascade="all, delete-orphan"
    )


class SyncAttempt(Base):
    """One row per ``sync`` API call (or skip decision).

    ``kind`` is one of ``yt_unlike``, ``ytm_like``, ``yt_relike_like``,
    ``yt_relike_unlike``, ``ytm_dedupe`` — the two halves of a drift fix get separate rows
    so the audit trail stays atomic per HTTP call. ``status`` is one of
    ``applied``, ``failed``, ``skipped``. ``reason`` carries sync-side
    detail (error message, threshold note, missing video_id, etc.) — the
    originating ``DiagnosisItem.reason`` is never overwritten.
    """

    __tablename__ = "sync_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    diagnosis_item_id: Mapped[int] = mapped_column(
        ForeignKey("diagnosis_items.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    diagnosis_item: Mapped[DiagnosisItem] = relationship(back_populates="sync_attempts")
