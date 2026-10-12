"""The window a point-in-time restore of an instance or cluster can land in, read from the inventory.

AWS reports the latest restorable time on every instance and cluster with automated backups
(`LatestRestorableTime`). The earliest is the cluster's `EarliestRestorableTime` when AWS reports it; for an
instance it is the backup retention before the latest, but never before the instance was created
(`InstanceCreateTime`, or `ClusterCreateTime` for a cluster). The inventory keeps the whole describe entry in
`record.raw`, so nothing here calls AWS.

Example:
    >>> window = restore_window(orders_record)  # doctest: +SKIP
    >>> window.contains(datetime(2026, 10, 11, 10, tzinfo=UTC))  # doctest: +SKIP
    True
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from rdscommander.resources.model import ResourceKind, ResourceRecord

__all__ = ["RestoreWindow", "format_utc", "parse_utc", "restore_window", "restore_window_unknown_reason"]


class RestoreWindow(NamedTuple):
    """The earliest and latest times a point-in-time restore can choose, both in UTC.

    Attributes:
        earliest_utc: The earliest restorable time.
        latest_restorable_utc: The latest restorable time AWS reports.
    """

    earliest_utc: datetime
    latest_restorable_utc: datetime

    def contains(self, moment_utc: datetime) -> bool:
        """Whether `moment_utc` lies inside the window, both ends included."""
        return self.earliest_utc <= moment_utc <= self.latest_restorable_utc

    def describe(self) -> str:
        """The window as a sentence fragment, such as `2026-10-04T10:00:00Z to 2026-10-11T09:55:00Z`."""
        return f"{format_utc(self.earliest_utc)} to {format_utc(self.latest_restorable_utc)}"


def parse_utc(value: Any) -> datetime | None:
    """A describe timestamp (ISO 8601, as the inventory stores it) as an aware UTC datetime.

    A value without a zone is read as UTC; anything that is not a readable time is None.

    Example:
        >>> parse_utc("2026-10-11 09:55:00.123+00:00").isoformat()
        '2026-10-11T09:55:00.123000+00:00'
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_utc(moment: datetime) -> str:
    """`moment` as ISO 8601 UTC with seconds and a `Z`, the form the restore APIs and the form use.

    Example:
        >>> format_utc(datetime(2026, 10, 11, 10, 0, 0, 500000, tzinfo=UTC))
        '2026-10-11T10:00:00Z'
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def restore_window_unknown_reason(record: ResourceRecord) -> str | None:
    """Why the window of `record` cannot be read from the inventory, or None when it can.

    Example:
        >>> restore_window_unknown_reason(orders_without_backups)  # doctest: +SKIP
        'AWS does not report a latest restorable time for demo-orders'
    """
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return f"{record.identifier} is not an instance or a cluster"
    if parse_utc(record.raw.get("LatestRestorableTime")) is None:
        return f"AWS does not report a latest restorable time for {record.identifier}"
    if _earliest_utc(record) is None:
        return (
            f"AWS reports neither the earliest restorable time, the creation time nor the backup retention "
            f"of {record.identifier}"
        )
    return None


def _earliest_utc(record: ResourceRecord) -> datetime | None:
    """The earliest restorable time, or None when the inventory does not say enough to work it out."""
    reported = parse_utc(record.raw.get("EarliestRestorableTime"))
    if reported is not None:
        return reported
    latest = parse_utc(record.raw.get("LatestRestorableTime"))
    created = parse_utc(
        record.raw.get("ClusterCreateTime" if record.kind == ResourceKind.CLUSTER else "InstanceCreateTime")
    )
    retention_days = record.facts.get("backup_retention_period")
    by_retention = (
        latest - timedelta(days=retention_days)
        if latest is not None and isinstance(retention_days, int) and retention_days > 0
        else None
    )
    candidates = [moment for moment in (created, by_retention) if moment is not None]
    return max(candidates) if candidates else None


def restore_window(record: ResourceRecord) -> RestoreWindow | None:
    """The point-in-time restore window of an instance or cluster, or None when the inventory cannot say.

    Args:
        record: The instance or cluster, as the latest inventory has it.

    Returns:
        The window in whole seconds (each end truncated, so a time shown as either end is inside it), with
        the earliest never after the latest; None when AWS reported no latest restorable
        time (automated backups off, or an account that does not report it) or nothing to work the earliest
        out from (see `restore_window_unknown_reason`).

    Example:
        >>> restore_window(orders_record).describe()  # doctest: +SKIP
        '2026-10-04T09:55:00Z to 2026-10-11T09:55:00Z'
    """
    if restore_window_unknown_reason(record) is not None:
        return None
    latest = parse_utc(record.raw.get("LatestRestorableTime"))
    earliest = _earliest_utc(record)
    assert latest is not None and earliest is not None
    # Whole seconds, as the window is shown and a time is chosen: a time shown as the latest must pass.
    latest = latest.replace(microsecond=0)
    return RestoreWindow(
        earliest_utc=min(earliest.replace(microsecond=0), latest), latest_restorable_utc=latest
    )
