"""The scheduler creates due inventory refreshes and drift checks, and never doubles up on either."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from fastapi.testclient import TestClient

from rdscommander.catalogue import catalogue as operation_catalogue
from rdscommander.config.models import ConsoleConfig
from rdscommander.config.settings import OperatorSettings
from rdscommander.config.store import StaticConfigStore
from rdscommander.control_db.models import InventorySnapshot, Operation
from rdscommander.logging_setup import configure_structured_logging
from rdscommander.main import create_application
from rdscommander.operations.scheduler import (
    SCHEDULER_OPERATOR_NAME,
    OperationScheduler,
    back_off_remaining,
    drift_check_is_due,
    recently_failed,
    refresh_is_due,
)
from rdscommander.operations.service import OperationService

SANDBOX_TARGET_KEY = "sandbox/000000000000/eu-west-2"
PRODUCTION_TARGET_KEY = "production/123456789012/eu-west-2"


@pytest.fixture
def synced_config_store(config_store: StaticConfigStore) -> StaticConfigStore:
    """The shared test configuration, synced so snapshots and operations can reference its rows."""
    config_store.reload()
    return config_store


@pytest.fixture
def recent_drift_checks(session_factory, synced_config_store) -> None:
    """A drift check succeeded an hour ago in every environment, so none is due.

    The catalogue defines `drift_detect`, so a tick that finds nothing else to do would otherwise create a
    drift check; the tests using this fixture are about what the scheduler does with refreshes.
    """
    for environment_slug in ("sandbox", "production"):
        _operation(session_factory, "drift_detect", environment_slug, "SUCCEEDED", timedelta(hours=1))


@pytest.fixture
def drift_detect_absent_from_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the catalogue not know `drift_detect`, whatever tasks have landed."""
    without_drift = tuple(d for d in operation_catalogue.OPERATION_DEFINITIONS if d.kind != "drift_detect")
    monkeypatch.setattr(operation_catalogue, "OPERATION_DEFINITIONS", without_drift)


def _snapshot(session_factory, target_key: str, age: timedelta) -> None:
    with session_factory() as session:
        session.add(
            InventorySnapshot(
                target_key=target_key,
                operation_id=None,
                captured_at_utc=datetime.now(UTC) - age,
                instance_count=0,
                cluster_count=0,
                parameter_group_count=0,
                subnet_group_count=0,
            )
        )
        session.commit()


def _operation(
    session_factory,
    kind: str,
    environment_slug: str,
    state: str,
    finished_age: timedelta | None = None,
) -> uuid.UUID:
    """An operation somebody else created, in `state`; finished `finished_age` ago when given."""
    operation = OperationService(session_factory).create(
        kind, environment_slug, None, {"options": {}}, f"test:{uuid.uuid4()}", "phil"
    )
    with session_factory() as session:
        row = session.get(Operation, operation.id)
        row.state = state
        row.finished_at_utc = datetime.now(UTC) - finished_age if finished_age is not None else None
        session.commit()
    return operation.id


def _operations_of_kind(session_factory, kind: str, environment_slug: str) -> list[Operation]:
    with session_factory() as session:
        return (
            session.query(Operation).filter_by(operation_kind=kind, environment_slug=environment_slug).all()
        )


def _staging_config(inventory_refresh_minutes: int = 15, drift_check_hours: int = 24) -> ConsoleConfig:
    """One environment with two targets, so per-target staleness can be told apart."""
    return ConsoleConfig.model_validate(
        {
            "terraform": {},
            "environments": [
                {
                    "slug": "staging",
                    "display_name": "Staging",
                    "inventory_refresh_minutes": inventory_refresh_minutes,
                    "drift_check_hours": drift_check_hours,
                    "targets": [
                        {
                            "aws_account_id": "111111111111",
                            "aws_region": "eu-west-2",
                            "aws_profile_name": "p",
                        },
                        {
                            "aws_account_id": "111111111111",
                            "aws_region": "us-east-1",
                            "aws_profile_name": "p",
                        },
                    ],
                }
            ],
        }
    )


def test_refresh_is_created_when_the_latest_snapshot_is_stale(session_factory, synced_config_store) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=20))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    created = scheduler.tick()
    with session_factory() as session:
        refreshes = session.query(Operation).filter_by(operation_kind="inventory_refresh").all()
    assert len(created) >= 1 and {r.environment_slug for r in refreshes} >= {"sandbox"}
    assert all(r.created_by_operator_name == "scheduler" for r in refreshes)


def test_scheduled_refresh_covers_every_target_of_its_environment(
    session_factory, synced_config_store
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=20))
    OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60).tick()
    (refresh,) = _operations_of_kind(session_factory, "inventory_refresh", "sandbox")
    assert refresh.plan == {"options": {"target_keys": []}}
    assert refresh.target_key is None
    assert refresh.title == "Refresh inventory: sandbox"


def test_refresh_is_created_for_an_environment_that_has_never_been_refreshed(
    session_factory, synced_config_store
) -> None:
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    created = scheduler.tick()
    assert len(created) == 2
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "sandbox")) == 1
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "production")) == 1


def test_no_second_refresh_while_one_is_queued_or_running(
    session_factory, synced_config_store, recent_drift_checks
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=20))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    first = scheduler.tick()
    second = scheduler.tick()
    with session_factory() as session:
        row = session.get(Operation, first[0]) if first else None
        if row is not None:
            row.state = "RUNNING"
            session.commit()
    third = scheduler.tick()
    with session_factory() as session:
        count = (
            session.query(Operation)
            .filter_by(operation_kind="inventory_refresh", environment_slug="sandbox")
            .count()
        )
    assert count == 1 and second == [] and third == []


@pytest.mark.parametrize("active_state", ["QUEUED", "RUNNING", "AWAITING_APPROVAL"])
def test_no_refresh_is_scheduled_while_a_refresh_made_by_someone_else_is_in_state(
    session_factory, synced_config_store, active_state: str, recent_drift_checks
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=20))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    manual_refresh_id = _operation(session_factory, "inventory_refresh", "sandbox", active_state)
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert scheduler.tick() == []
    sandbox_refreshes = _operations_of_kind(session_factory, "inventory_refresh", "sandbox")
    assert [r.id for r in sandbox_refreshes] == [manual_refresh_id]


@pytest.mark.parametrize(
    ("finished_state", "finished_minutes_ago"), [("SUCCEEDED", 5), ("FAILED", 20), ("CANCELLED", 20)]
)
def test_a_finished_refresh_does_not_stop_the_next_one_when_the_snapshot_is_still_stale(
    session_factory, synced_config_store, finished_state: str, finished_minutes_ago: int, recent_drift_checks
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=30))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    _operation(
        session_factory,
        "inventory_refresh",
        "sandbox",
        finished_state,
        timedelta(minutes=finished_minutes_ago),
    )
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert len(scheduler.tick()) == 1
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "sandbox")) == 2


@pytest.mark.parametrize("ended_state", ["FAILED", "CANCELLED"])
def test_a_refresh_that_ended_two_minutes_ago_in_state_blocks_a_new_refresh(
    session_factory, synced_config_store, ended_state: str, recent_drift_checks
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=30))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    failed_refresh_id = _operation(
        session_factory, "inventory_refresh", "sandbox", ended_state, timedelta(minutes=2)
    )
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert scheduler.tick() == []
    assert scheduler.tick() == []
    sandbox_refreshes = _operations_of_kind(session_factory, "inventory_refresh", "sandbox")
    assert [r.id for r in sandbox_refreshes] == [failed_refresh_id]


def test_back_off_after_a_failed_refresh_is_judged_on_the_latest_refresh_only(
    session_factory, synced_config_store, recent_drift_checks
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=30))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    _operation(session_factory, "inventory_refresh", "sandbox", "FAILED", timedelta(minutes=10))
    _operation(session_factory, "inventory_refresh", "sandbox", "SUCCEEDED", timedelta(minutes=3))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert len(scheduler.tick()) == 1
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "sandbox")) == 3


def test_back_off_after_a_failed_refresh_does_not_hold_back_other_environments(
    session_factory, synced_config_store, recent_drift_checks
) -> None:
    _operation(session_factory, "inventory_refresh", "sandbox", "FAILED", timedelta(minutes=2))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert len(scheduler.tick()) == 1
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "sandbox")) == 1
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "production")) == 1


def test_back_off_is_logged_with_the_environment_kind_and_time_until_the_next_attempt(
    session_factory, synced_config_store, capsys
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=30))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    _operation(session_factory, "inventory_refresh", "sandbox", "FAILED", timedelta(minutes=5))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    configure_structured_logging()
    capsys.readouterr()  # discard output written before JSON logging was configured
    try:
        scheduler.tick()
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    finally:
        structlog.reset_defaults()
    [backoff_event] = [e for e in events if e["event"] == "scheduled_operation_backoff"]
    assert backoff_event["level"] == "info"
    assert backoff_event["environment_slug"] == "sandbox"
    assert backoff_event["operation_kind"] == "inventory_refresh"
    assert 9 * 60 - 30 <= backoff_event["seconds_until_next_attempt"] <= 10 * 60
    assert "next_attempt_at_utc" in backoff_event


def test_recently_failed_follows_the_inventory_refresh_minutes_of_the_environment(session_factory) -> None:
    config = _staging_config(inventory_refresh_minutes=60)
    StaticConfigStore(config, session_factory).reload()
    environment = config.environments[0]
    now = datetime.now(UTC)
    _operation(session_factory, "inventory_refresh", "staging", "FAILED", timedelta(minutes=30))
    with session_factory() as session:
        assert recently_failed(session, "inventory_refresh", environment, now) is True
        assert (
            recently_failed(session, "inventory_refresh", environment, now + timedelta(minutes=45)) is False
        )


def test_recently_failed_follows_the_drift_check_hours_of_the_environment(session_factory) -> None:
    config = _staging_config(drift_check_hours=6)
    StaticConfigStore(config, session_factory).reload()
    environment = config.environments[0]
    now = datetime.now(UTC)
    _operation(session_factory, "drift_detect", "staging", "FAILED", timedelta(hours=2))
    with session_factory() as session:
        assert recently_failed(session, "drift_detect", environment, now) is True
        assert recently_failed(session, "drift_detect", environment, now + timedelta(hours=5)) is False


def test_recently_failed_is_false_for_an_environment_with_no_operations_of_the_kind(session_factory) -> None:
    config = _staging_config()
    StaticConfigStore(config, session_factory).reload()
    _operation(session_factory, "drift_detect", "staging", "FAILED", timedelta(minutes=1))
    with session_factory() as session:
        assert (
            recently_failed(session, "inventory_refresh", config.environments[0], datetime.now(UTC)) is False
        )


def test_back_off_remaining_is_the_cadence_minus_the_time_since_the_failure(session_factory) -> None:
    config = _staging_config(inventory_refresh_minutes=15)
    StaticConfigStore(config, session_factory).reload()
    _operation(session_factory, "inventory_refresh", "staging", "FAILED", timedelta(minutes=5))
    with session_factory() as session:
        remaining = back_off_remaining(
            session, "inventory_refresh", config.environments[0], datetime.now(UTC)
        )
    assert remaining is not None
    assert timedelta(minutes=9, seconds=30) <= remaining <= timedelta(minutes=10)


def test_back_off_remaining_rejects_an_operation_kind_the_scheduler_does_not_run(session_factory) -> None:
    config = _staging_config()
    StaticConfigStore(config, session_factory).reload()
    with session_factory() as session, pytest.raises(ValueError, match="no cadence"):
        back_off_remaining(session, "terraform_adoption", config.environments[0], datetime.now(UTC))


def test_a_drift_detection_that_failed_two_minutes_ago_blocks_a_new_one(
    session_factory, synced_config_store
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=1))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    failed_detection_id = _operation(
        session_factory, "drift_detect", "sandbox", "FAILED", timedelta(minutes=2)
    )
    _operation(session_factory, "drift_detect", "production", "SUCCEEDED", timedelta(hours=1))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert scheduler.tick() == []
    assert [d.id for d in _operations_of_kind(session_factory, "drift_detect", "sandbox")] == [
        failed_detection_id
    ]


def test_a_drift_detection_that_failed_longer_ago_than_the_cadence_does_not_block_a_new_one(
    session_factory, synced_config_store
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=1))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    _operation(session_factory, "drift_detect", "sandbox", "FAILED", timedelta(hours=25))
    _operation(session_factory, "drift_detect", "production", "SUCCEEDED", timedelta(hours=1))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert len(scheduler.tick()) == 1
    assert len(_operations_of_kind(session_factory, "drift_detect", "sandbox")) == 2


def test_fresh_snapshot_means_nothing_to_do(
    session_factory, synced_config_store, recent_drift_checks
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=1))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    assert (
        OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60).tick()
        == []
    )


def test_refresh_is_due_when_one_target_of_the_environment_is_stale(session_factory) -> None:
    config = _staging_config()
    StaticConfigStore(config, session_factory).reload()
    _snapshot(session_factory, "staging/111111111111/eu-west-2", timedelta(minutes=1))
    _snapshot(session_factory, "staging/111111111111/us-east-1", timedelta(minutes=30))
    with session_factory() as session:
        assert refresh_is_due(session, config.environments[0], datetime.now(UTC)) is True


def test_refresh_is_due_when_one_target_of_the_environment_has_no_snapshot(session_factory) -> None:
    config = _staging_config()
    StaticConfigStore(config, session_factory).reload()
    _snapshot(session_factory, "staging/111111111111/eu-west-2", timedelta(minutes=1))
    with session_factory() as session:
        assert refresh_is_due(session, config.environments[0], datetime.now(UTC)) is True


def test_refresh_is_not_due_when_every_target_snapshot_is_fresh(session_factory) -> None:
    config = _staging_config()
    StaticConfigStore(config, session_factory).reload()
    _snapshot(session_factory, "staging/111111111111/eu-west-2", timedelta(minutes=1))
    _snapshot(session_factory, "staging/111111111111/us-east-1", timedelta(minutes=14))
    with session_factory() as session:
        assert refresh_is_due(session, config.environments[0], datetime.now(UTC)) is False


def test_refresh_is_judged_on_the_newest_snapshot_of_a_target_not_the_oldest(session_factory) -> None:
    config = _staging_config()
    StaticConfigStore(config, session_factory).reload()
    for target_key in ("staging/111111111111/eu-west-2", "staging/111111111111/us-east-1"):
        _snapshot(session_factory, target_key, timedelta(hours=3))
        _snapshot(session_factory, target_key, timedelta(minutes=2))
    with session_factory() as session:
        assert refresh_is_due(session, config.environments[0], datetime.now(UTC)) is False


def test_refresh_is_due_follows_the_inventory_refresh_minutes_of_the_environment(session_factory) -> None:
    config = _staging_config(inventory_refresh_minutes=60)
    StaticConfigStore(config, session_factory).reload()
    _snapshot(session_factory, "staging/111111111111/eu-west-2", timedelta(minutes=45))
    _snapshot(session_factory, "staging/111111111111/us-east-1", timedelta(minutes=45))
    with session_factory() as session:
        assert refresh_is_due(session, config.environments[0], datetime.now(UTC)) is False
        assert (
            refresh_is_due(session, config.environments[0], datetime.now(UTC) + timedelta(minutes=30)) is True
        )


def test_drift_check_is_due_when_the_environment_has_never_had_one(session_factory) -> None:
    config = _staging_config()
    StaticConfigStore(config, session_factory).reload()
    with session_factory() as session:
        assert drift_check_is_due(session, config.environments[0], datetime.now(UTC)) is True


def test_drift_check_is_not_due_after_a_recent_successful_detection(session_factory) -> None:
    config = _staging_config(drift_check_hours=24)
    StaticConfigStore(config, session_factory).reload()
    _operation(session_factory, "drift_detect", "staging", "SUCCEEDED", timedelta(hours=2))
    with session_factory() as session:
        assert drift_check_is_due(session, config.environments[0], datetime.now(UTC)) is False


def test_drift_check_is_due_once_the_last_success_is_older_than_drift_check_hours(session_factory) -> None:
    config = _staging_config(drift_check_hours=6)
    StaticConfigStore(config, session_factory).reload()
    _operation(session_factory, "drift_detect", "staging", "SUCCEEDED", timedelta(hours=7))
    _operation(session_factory, "drift_detect", "staging", "SUCCEEDED", timedelta(hours=9))
    with session_factory() as session:
        assert drift_check_is_due(session, config.environments[0], datetime.now(UTC)) is True


def test_drift_check_is_due_when_the_only_recent_detection_failed(session_factory) -> None:
    config = _staging_config()
    StaticConfigStore(config, session_factory).reload()
    _operation(session_factory, "drift_detect", "staging", "FAILED", timedelta(minutes=5))
    with session_factory() as session:
        assert drift_check_is_due(session, config.environments[0], datetime.now(UTC)) is True


def test_no_drift_check_is_scheduled_while_the_catalogue_has_no_drift_detect(
    session_factory, synced_config_store, drift_detect_absent_from_catalogue
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=1))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert scheduler.tick() == []
    assert _operations_of_kind(session_factory, "drift_detect", "sandbox") == []


def test_drift_check_is_scheduled_when_the_catalogue_has_drift_detect_and_inventory_is_fresh(
    session_factory, synced_config_store
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=1))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    created = scheduler.tick()
    assert len(created) == 2
    (sandbox_drift_check,) = _operations_of_kind(session_factory, "drift_detect", "sandbox")
    assert sandbox_drift_check.created_by_operator_name == "scheduler"
    assert sandbox_drift_check.plan == {"options": {}}
    assert sandbox_drift_check.title == "Detect drift: sandbox"
    assert len(_operations_of_kind(session_factory, "drift_detect", "production")) == 1


def test_drift_check_waits_for_the_refresh_scheduled_in_the_same_tick(
    session_factory, synced_config_store
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=30))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    scheduler.tick()
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "sandbox")) == 1
    assert _operations_of_kind(session_factory, "drift_detect", "sandbox") == []
    assert len(_operations_of_kind(session_factory, "drift_detect", "production")) == 1


def test_no_second_drift_check_while_one_is_queued_running_or_awaiting_approval(
    session_factory, synced_config_store
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=1))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    _operation(session_factory, "drift_detect", "sandbox", "QUEUED")
    _operation(session_factory, "drift_detect", "production", "RUNNING")
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert scheduler.tick() == []


def test_no_drift_check_is_scheduled_after_a_recent_successful_one(
    session_factory, synced_config_store
) -> None:
    _snapshot(session_factory, SANDBOX_TARGET_KEY, timedelta(minutes=1))
    _snapshot(session_factory, PRODUCTION_TARGET_KEY, timedelta(minutes=1))
    _operation(session_factory, "drift_detect", "sandbox", "SUCCEEDED", timedelta(hours=1))
    _operation(session_factory, "drift_detect", "production", "SUCCEEDED", timedelta(hours=1))
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    assert scheduler.tick() == []


def test_a_failure_scheduling_one_environment_does_not_stop_the_others(
    session_factory, synced_config_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_create = OperationService.create

    def create_failing_for_sandbox(self, operation_kind, environment_slug, *arguments, **keywords):
        if environment_slug == "sandbox":
            raise RuntimeError("control database hiccup")
        return real_create(self, operation_kind, environment_slug, *arguments, **keywords)

    monkeypatch.setattr(OperationService, "create", create_failing_for_sandbox)
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    created = scheduler.tick()
    assert len(created) == 1
    assert _operations_of_kind(session_factory, "inventory_refresh", "sandbox") == []
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "production")) == 1


def test_started_scheduler_ticks_on_its_interval_and_stops_cleanly(
    session_factory, synced_config_store
) -> None:
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=0.05)
    assert scheduler.is_running is False
    scheduler.start()
    try:
        assert scheduler.is_running is True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _operations_of_kind(
            session_factory, "inventory_refresh", "sandbox"
        ):
            time.sleep(0.05)
    finally:
        scheduler.stop()
    assert len(_operations_of_kind(session_factory, "inventory_refresh", "sandbox")) == 1
    assert scheduler.is_running is False


def test_scheduler_loop_survives_a_failing_tick(
    session_factory, synced_config_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=0.02)
    tick_count = 0
    second_tick_reached = threading.Event()

    def tick_that_always_fails() -> list[uuid.UUID]:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 2:
            second_tick_reached.set()
        raise RuntimeError("control database unreachable")

    monkeypatch.setattr(scheduler, "tick", tick_that_always_fails)
    scheduler.start()
    try:
        assert second_tick_reached.wait(timeout=10)
    finally:
        scheduler.stop()
    assert scheduler.is_running is False


def test_stopping_a_scheduler_that_was_never_started_is_harmless(
    session_factory, synced_config_store
) -> None:
    scheduler = OperationScheduler(session_factory, synced_config_store, "scheduler", interval_seconds=60)
    scheduler.stop()
    assert scheduler.is_running is False


def _application_with_scheduler(
    session_factory,
    operator_settings: OperatorSettings,
    console_config: ConsoleConfig,
    scheduler_enabled: bool,
    start_runner: bool,
):
    settings = operator_settings.model_copy(
        update={"scheduler_enabled": scheduler_enabled, "scheduler_interval_seconds": 3600.0}
    )
    return create_application(
        settings=settings,
        console_config=console_config,
        session_factory=session_factory,
        start_runner=start_runner,
    )


def test_application_exposes_a_scheduler_attributed_to_the_scheduler_not_the_human_operator(
    session_factory, operator_settings, console_config
) -> None:
    app = _application_with_scheduler(
        session_factory, operator_settings, console_config, scheduler_enabled=True, start_runner=False
    )
    scheduler = app.state.scheduler
    assert isinstance(scheduler, OperationScheduler)
    assert scheduler.operator_name == SCHEDULER_OPERATOR_NAME == "scheduler"
    assert scheduler.operator_name != operator_settings.operator_name
    assert scheduler.interval_seconds == 3600.0
    assert scheduler.is_running is False


def test_scheduler_runs_inside_the_application_lifespan_and_stops_with_it(
    session_factory, operator_settings, console_config
) -> None:
    app = _application_with_scheduler(
        session_factory, operator_settings, console_config, scheduler_enabled=True, start_runner=True
    )
    with TestClient(app):
        assert app.state.scheduler.is_running is True
    assert app.state.scheduler.is_running is False


def test_scheduler_stays_off_when_scheduler_enabled_is_false(
    session_factory, operator_settings, console_config
) -> None:
    app = _application_with_scheduler(
        session_factory, operator_settings, console_config, scheduler_enabled=False, start_runner=True
    )
    with TestClient(app):
        assert app.state.scheduler.is_running is False


def test_scheduler_stays_off_when_the_application_does_not_start_background_workers(
    session_factory, operator_settings, console_config
) -> None:
    app = _application_with_scheduler(
        session_factory, operator_settings, console_config, scheduler_enabled=True, start_runner=False
    )
    with TestClient(app):
        assert app.state.scheduler.is_running is False


def test_operator_settings_enable_the_scheduler_every_minute_by_default() -> None:
    settings = OperatorSettings(_env_file=None)
    assert settings.scheduler_enabled is True
    assert settings.scheduler_interval_seconds == 60.0


def test_the_test_settings_fixture_keeps_the_scheduler_off(operator_settings) -> None:
    assert operator_settings.scheduler_enabled is False
