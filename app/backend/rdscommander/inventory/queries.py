"""Read-side queries over snapshots."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.control_db.models import InventorySnapshot


def latest_snapshot_for_target(session: Session, target_key: str) -> InventorySnapshot | None:
    """The most recent snapshot of `target_key`, or None."""
    return session.execute(
        select(InventorySnapshot)
        .where(InventorySnapshot.target_key == target_key)
        .order_by(InventorySnapshot.id.desc())
        .limit(1)
    ).scalar_one_or_none()
