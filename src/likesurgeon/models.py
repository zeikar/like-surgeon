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


class PlaylistScan(Base):
    """One ``scan playlists`` run; the playlist snapshots it took point back here."""

    __tablename__ = "playlist_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Snapshot(Base):
    """A point-in-time capture of liked songs or one playlist from a single source.

    The five ``playlist_*`` columns are NULL for likes snapshots.
    ``playlist_owned`` and ``playlist_raw_json`` are set on the
    ``ytmusic_playlist`` side only.
    """

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    raw_count: Mapped[int] = mapped_column(Integer)

    # Plain Integer (no FK, no index) so fresh and migrated DBs share one shape,
    # as with ``diagnoses.kind``.
    playlist_scan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    playlist_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    playlist_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    playlist_owned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    playlist_raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    # items aren't guaranteed-music (youtube_liked_videos, youtube_playlist). NULL for
    # ytmusic_liked_songs since those are always music by definition.
    is_music_candidate: Mapped[bool | None] = mapped_column(nullable=True)
    music_candidate_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    music_candidate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ghost detection (0.3): availability of the underlying YouTube video at
    # scan time. ``None`` means "unknown" (snapshot taken before 0.3, or
    # status check failed). Only populated for ``youtube_liked_videos`` and
    # ``youtube_playlist`` rows.
    is_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    unavailable_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # YT Music's ``videoType`` (e.g. MUSIC_VIDEO_TYPE_ATV); NULL on YouTube rows.
    video_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
    """A persisted ``compare-likes``, ``canonicalize``, ``repoint-playlists`` or
    ``canonicalize-playlists`` run.

    ``kind`` is ``None`` for a ``compare-likes`` (alignment) run,
    ``'canonicalize'`` for a ``canonicalize`` report, ``'playlists'`` for a
    ``repoint-playlists`` report and ``'playlist_canonicalize'`` for a
    ``canonicalize-playlists`` report, so ``sync`` and ``doctor`` never mistake
    one for another. A playlist report (either of the last two) sets
    ``playlist_scan_id`` to the scan it was built from (a plain integer, no FK,
    like ``snapshots.playlist_scan_id``).
    """

    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ytmusic_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True
    )
    youtube_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    playlist_scan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    items: Mapped[list[DiagnosisItem]] = relationship(
        back_populates="diagnosis", cascade="all, delete-orphan"
    )


class DiagnosisItem(Base):
    """A single finding produced by a ``compare-likes`` or ``canonicalize`` run.

    ``issue_type`` is one of (see ``diagnosis.ALIGNMENT_ISSUE_TYPES``):
      - ``relinked`` / ``rendered_as_other`` / ``shadow_duplicate`` — pair
        findings: YouTube like A and the YT Music entry B that renders it
        (``source_track_id`` = A, ``related_track_id`` = B). Only these can
        be write-eligible (``confidence == 1.0``).
      - ``dead_unrendered`` — a dead YouTube like with no YT Music entry.
      - ``unrendered_music`` — a playable music like with no YT Music entry;
        ``sync --relike-unrendered`` acts on it.
      - ``unbacked_lm_entry`` — a YT Music entry with no YouTube like behind it.
      - ``metadata_drift`` — the same ``video_id`` appears in two snapshots
        of one source with meaningfully different title or artists.

    A ``canonicalize`` report (``Diagnosis.kind == 'canonicalize'``) uses
    ``canonical_version`` / ``canonical_ambiguous`` instead (see
    ``diagnosis.CANONICAL_ISSUE_TYPES``): a liked video that has a better
    version, or one whose better version can't be picked unambiguously.

    A playlists report (``Diagnosis.kind == 'playlists'``) uses
    ``playlist_relinked`` (see ``diagnosis.PLAYLIST_ISSUE_TYPES``): A is a dead
    YouTube playlist item and B the video YT Music shows in its place.
    A playlist canonicalize report (``Diagnosis.kind ==
    'playlist_canonicalize'``) uses ``playlist_canonical_version`` (see
    ``diagnosis.PLAYLIST_CANONICAL_ISSUE_TYPES``): A is a playable playlist
    item YT Music shows as itself and B the release its artist page lists.
    For both, ``source_item_id`` is A's ``youtube_playlist`` row and
    ``related_item_id`` the ``ytmusic_playlist`` row rendering A's item (B for
    ``playlist_relinked``, A itself for ``playlist_canonical_version``);
    ``related_track_id`` is B's ``Track``.

    Rows written before the LL → LM alignment redesign may carry legacy
    values (``possibly_missing_from_ytmusic``, ``possible_pointer_drift``,
    ``ytmusic_only``, ``unavailable_video``, ``duplicate_in_source``); nothing
    produces or acts on them any more.
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
    source_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    related_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")

    diagnosis: Mapped[Diagnosis] = relationship(back_populates="items")
    sync_attempts: Mapped[list[SyncAttempt]] = relationship(
        back_populates="diagnosis_item", cascade="all, delete-orphan"
    )


class SyncAttempt(Base):
    """One row per ``sync`` / ``canonicalize --apply`` / playlist repoint API call,
    check, or skip decision.

    ``kind`` names the step: ``repoint_precheck_a``,
    ``repoint_precheck_b``, ``repoint_like``, ``repoint_verify``,
    ``repoint_unlike``, ``repoint_unlike_verify``; ``unlike_shadow_precheck``,
    ``unlike_shadow_precheck_a``, ``unlike_shadow_unlike``,
    ``unlike_shadow_verify``; ``lm_check``; ``restore_like``,
    ``restore_verify``; ``repoint_rollback``,
    ``repoint_rollback_verify``; ``relike_precheck``, ``relike_unlike``,
    ``relike_unlike_verify``, ``relike_like``, ``relike_verify``,
    ``relike_lm_check``; ``canonicalize --apply`` writes ``canonical_precheck_lm``,
    ``canonical_precheck_a``, ``canonical_precheck_b``, ``canonical_like``,
    ``canonical_verify``, ``canonical_unlike``, ``canonical_unlike_verify``,
    ``canonical_rollback``, ``canonical_rollback_verify`` and the shared
    ``lm_check`` / ``restore_*`` rows; or the action kind
    (``repoint`` / ``unlike_shadow`` / ``relike``) for a plan-time skip, and ``interrupted`` when an
    action stopped after A's unlike call started. See ``sync_dispatch.py`` for the
    authoritative list. Playlist actions (``playlist_dispatch.py``: repoints and,
    for ``canonicalize-playlists``, merges) write
    ``playlist_precheck``, ``playlist_insert``, ``playlist_insert_verify``,
    ``playlist_ytm_verify``, ``playlist_delete``, ``playlist_delete_verify``,
    ``playlist_check``, ``playlist_rollback``, ``playlist_rollback_verify``,
    ``playlist_restore``, ``playlist_restore_verify`` and
    ``playlist_interrupted``; none count toward ``sync``'s refusal. Rows
    written before the LL → LM alignment redesign may carry legacy kinds
    (``yt_unlike``, ``ytm_like_*``, ``yt_like_*``, ``yt_relike_*``,
    ``ytm_dedupe``). ``status`` is one of ``applied``,
    ``failed``, ``skipped``. ``reason`` carries sync-side detail (error
    message, LM diff, missing video_id, etc.) — the originating
    ``DiagnosisItem.reason`` is never overwritten.
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
