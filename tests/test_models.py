"""Schema smoke tests for new ORM tables."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from likesurgeon.models import Diagnosis, DiagnosisItem, SyncAttempt


def _make_diagnosis_item(session: Session) -> DiagnosisItem:
    diagnosis = Diagnosis()
    session.add(diagnosis)
    session.flush()
    item = DiagnosisItem(
        diagnosis_id=diagnosis.id,
        issue_type="unavailable_video",
        confidence=1.0,
        reason="deleted",
    )
    session.add(item)
    session.flush()
    return item


def test_sync_attempt_round_trips(session: Session) -> None:
    """A SyncAttempt can be inserted with an FK to DiagnosisItem and read
    back with all fields intact — proves the table was created from
    ``Base.metadata`` without a separate migration step."""
    item = _make_diagnosis_item(session)

    attempt = SyncAttempt(
        diagnosis_item_id=item.id,
        kind="yt_unlike",
        status="applied",
        reason="ok",
    )
    session.add(attempt)
    session.flush()

    fetched = session.execute(select(SyncAttempt)).scalar_one()
    assert fetched.diagnosis_item_id == item.id
    assert fetched.kind == "yt_unlike"
    assert fetched.status == "applied"
    assert fetched.reason == "ok"
    assert fetched.created_at is not None


def test_sync_attempt_cascade_delete_on_diagnosis_item(session: Session) -> None:
    """Deleting the parent DiagnosisItem must take its SyncAttempt rows with
    it (FK ``ondelete=CASCADE`` + ``PRAGMA foreign_keys=ON`` from
    ``db._sqlite_fk_pragma``). Otherwise orphaned attempts would dangle on
    diagnoses pruned by future cleanup paths."""
    item = _make_diagnosis_item(session)
    session.add(
        SyncAttempt(diagnosis_item_id=item.id, kind="ytm_like", status="failed", reason="x")
    )
    session.add(
        SyncAttempt(diagnosis_item_id=item.id, kind="yt_unlike", status="applied", reason="y")
    )
    session.flush()
    assert session.execute(select(SyncAttempt)).scalars().all()  # sanity

    session.delete(item)
    session.flush()

    remaining = session.execute(select(SyncAttempt)).scalars().all()
    assert remaining == []
