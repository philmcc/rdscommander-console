"""The point-in-time restore window, read from an instance's or cluster's describe entry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rdscommander.lifecycle.restore_window import (
    format_utc,
    parse_utc,
    restore_window,
    restore_window_unknown_reason,
)
from rdscommander.resources.model import ResourceKind, ResourceRecord

LATEST = datetime(2026, 10, 11, 9, 55, tzinfo=UTC)


def _record(kind: ResourceKind, raw: dict, retention_days: int | None = 7) -> ResourceRecord:
    return ResourceRecord(
        kind=kind,
        arn=f"arn:aws:rds:eu-west-2:000000000000:{'db' if kind == ResourceKind.INSTANCE else 'cluster'}:x",
        resource_id="x",
        identifier="demo-orders",
        environment_slug="sandbox",
        target_key="sandbox/000000000000/eu-west-2",
        ownership="unmanaged",
        status="available",
        facts={"backup_retention_period": retention_days},
        relationships={},
        raw=raw,
        snapshot_id=1,
    )


def test_an_old_instance_s_window_reaches_back_its_backup_retention() -> None:
    record = _record(
        ResourceKind.INSTANCE,
        {"LatestRestorableTime": str(LATEST), "InstanceCreateTime": "2025-01-01 00:00:00+00:00"},
    )
    window = restore_window(record)
    assert window is not None
    assert (window.earliest_utc, window.latest_restorable_utc) == (LATEST - timedelta(days=7), LATEST)
    assert window.describe() == "2026-10-04T09:55:00Z to 2026-10-11T09:55:00Z"


def test_a_new_instance_s_window_starts_when_it_was_created() -> None:
    created = LATEST - timedelta(hours=2)
    record = _record(
        ResourceKind.INSTANCE,
        {"LatestRestorableTime": LATEST.isoformat(), "InstanceCreateTime": str(created)},
    )
    window = restore_window(record)
    assert window is not None and window.earliest_utc == created
    assert window.contains(created + timedelta(minutes=1)) and not window.contains(
        created - timedelta(seconds=1)
    )


def test_a_cluster_s_window_takes_the_earliest_restorable_time_aws_reports() -> None:
    earliest = LATEST - timedelta(days=1)
    record = _record(
        ResourceKind.CLUSTER,
        {
            "LatestRestorableTime": str(LATEST),
            "EarliestRestorableTime": str(earliest),
            "ClusterCreateTime": "2025-01-01T00:00:00Z",
        },
    )
    window = restore_window(record)
    assert window is not None and window.earliest_utc == earliest


def test_the_window_is_unknown_without_a_latest_restorable_time_or_anything_to_reach_back_from() -> None:
    no_latest = _record(ResourceKind.INSTANCE, {"InstanceCreateTime": str(LATEST)})
    assert restore_window(no_latest) is None
    assert "latest restorable time" in restore_window_unknown_reason(no_latest)
    nothing_earlier = _record(
        ResourceKind.INSTANCE, {"LatestRestorableTime": str(LATEST)}, retention_days=None
    )
    assert restore_window(nothing_earlier) is None
    assert "backup retention" in restore_window_unknown_reason(nothing_earlier)
    by_retention_only = _record(
        ResourceKind.INSTANCE, {"LatestRestorableTime": str(LATEST)}, retention_days=1
    )
    assert restore_window(by_retention_only).earliest_utc == LATEST - timedelta(days=1)


def test_the_window_is_in_whole_seconds_so_either_end_as_shown_is_inside_it() -> None:
    latest = LATEST.replace(microsecond=480000)
    created = LATEST.replace(microsecond=400000)
    record = _record(
        ResourceKind.INSTANCE, {"LatestRestorableTime": str(latest), "InstanceCreateTime": str(created)}
    )
    window = restore_window(record)
    assert window is not None and window.describe() == "2026-10-11T09:55:00Z to 2026-10-11T09:55:00Z"
    assert window.contains(parse_utc("2026-10-11T09:55:00Z"))


def test_times_are_read_and_written_as_utc() -> None:
    assert parse_utc("2026-10-11T10:00:00Z") == datetime(2026, 10, 11, 10, tzinfo=UTC)
    assert parse_utc("2026-10-11T12:00:00+02:00") == datetime(2026, 10, 11, 10, tzinfo=UTC)
    assert parse_utc("2026-10-11T10:00:00") == datetime(2026, 10, 11, 10, tzinfo=UTC)
    assert parse_utc("not a time") is None and parse_utc(None) is None
    assert format_utc(datetime(2026, 10, 11, 12, 0, 1, 999, tzinfo=UTC)) == "2026-10-11T12:00:01Z"
