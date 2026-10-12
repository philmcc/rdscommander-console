"""The major upgrade's preflight checks, against the seeded simulated estate."""

from __future__ import annotations

from typing import Any

import boto3
import pytest

from rdscommander.control_db.models import DiagnosticReport, InventoryDbInstance, InventoryPendingMaintenance
from rdscommander.operations.service import OperationService
from rdscommander.resources.queries import load_resource
from rdscommander.upgrades.preflight import (
    REPLICAS_NOT_CARRIED_SENTENCE,
    PreflightCheck,
    record_preflight_checks,
    run_preflight,
)
from tests.terraform.conftest import TARGET_KEY, arn_for

TARGET_VERSION = "17.2"


def _checks(
    session_factory, sandbox_snapshot, make_step_context, identifier: str
) -> dict[str, PreflightCheck]:
    """Run the preflight of `identifier` for 17.2 and key the checks by name."""
    arn = arn_for(session_factory, sandbox_snapshot, identifier)
    with session_factory() as session:
        context = make_step_context(session, arn)
        record = load_resource(session, context.console_config, arn)
        assert record is not None
        checks = run_preflight(context, record, TARGET_VERSION)
    names = [check.name for check in checks]
    assert len(names) == len(set(names))
    return {check.name: check for check in checks}


def _orderable_options_are(monkeypatch: pytest.MonkeyPatch, options: list[dict[str, Any]]) -> None:
    """Make the simulated account answer `DescribeOrderableDBInstanceOptions` with `options`."""
    from moto.rds.models import RDSBackend

    monkeypatch.setattr(
        RDSBackend,
        "describe_orderable_db_instance_options",
        lambda self, engine, engine_version: [
            option
            for option in options
            if option["Engine"] == engine and option["EngineVersion"] == engine_version
        ],
    )


def test_preflight_of_demo_orders_passes_backups_and_warns_about_its_replica_and_the_simulator_limits(
    session_factory, sandbox_snapshot, make_step_context
) -> None:
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")
    assert set(checks) == {
        "pending_maintenance",
        "backups",
        "in_progress_operations",
        "replicas",
        "class_and_storage",
        "extensions",
        "blue_side_name",
        "logical_replication",
    }
    assert checks["pending_maintenance"].outcome == "pass"
    assert checks["backups"].outcome == "pass"
    assert checks["in_progress_operations"].outcome == "pass"
    assert checks["replicas"].outcome == "warn"
    assert "demo-orders-replica" in checks["replicas"].detail
    assert REPLICAS_NOT_CARRIED_SENTENCE in checks["replicas"].detail
    assert REPLICAS_NOT_CARRIED_SENTENCE == "replicas are not carried to green; recreate them after"
    # moto lists no orderable options for postgres; the simulated account cannot say.
    assert checks["class_and_storage"].outcome == "warn"
    assert checks["extensions"].outcome == "warn"
    assert "not available on this account" in checks["extensions"].detail
    assert checks["blue_side_name"].outcome == "pass"
    # the seeded group turns logical replication on, which a blue/green deployment needs
    assert checks["logical_replication"].outcome == "pass"
    assert "demo-postgres16-tuned" in checks["logical_replication"].detail


def test_preflight_fails_backups_when_the_retention_is_zero(
    session_factory, sandbox_snapshot, make_step_context
) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=0, ApplyImmediately=True
    )
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")
    assert checks["backups"].outcome == "fail"
    assert "0" in checks["backups"].detail and checks["backups"].fix_suggestion


def test_preflight_fails_when_another_operation_on_the_resource_is_in_progress(
    session_factory, sandbox_snapshot, make_step_context
) -> None:
    arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    other = OperationService(session_factory).create(
        "reboot_instance",
        "sandbox",
        TARGET_KEY,
        {"options": {}, "resource_arn": arn},
        "reboot-in-progress",
        "phil",
        resource_arn=arn,
        title="Reboot: demo-orders",
    )
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")
    assert checks["in_progress_operations"].outcome == "fail"
    assert "Reboot: demo-orders" in checks["in_progress_operations"].detail
    assert str(other.id) in checks["in_progress_operations"].detail


def test_preflight_warns_about_pending_maintenance_on_the_resource(
    session_factory, sandbox_snapshot, make_step_context
) -> None:
    with session_factory() as session:
        row = session.query(InventoryDbInstance).filter_by(db_instance_identifier="demo-orders").one()
        session.add(
            InventoryPendingMaintenance(
                snapshot_id=row.snapshot_id,
                resource_arn=row.db_instance_arn,
                action="system-update",
                description="A pending update",
            )
        )
        session.commit()
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")
    assert checks["pending_maintenance"].outcome == "warn"
    assert "system-update" in checks["pending_maintenance"].detail


def test_preflight_fails_the_class_check_when_the_target_version_does_not_offer_the_class(
    session_factory, sandbox_snapshot, make_step_context, monkeypatch
) -> None:
    _orderable_options_are(
        monkeypatch,
        [{"Engine": "postgres", "EngineVersion": TARGET_VERSION, "DBInstanceClass": "db.r6g.xlarge"}],
    )
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")
    assert checks["class_and_storage"].outcome == "fail"
    assert "db.m6g.large" in checks["class_and_storage"].detail
    assert TARGET_VERSION in checks["class_and_storage"].detail


def test_preflight_passes_the_class_check_when_the_target_version_offers_the_class_and_storage(
    session_factory, sandbox_snapshot, make_step_context, monkeypatch
) -> None:
    storage_type = boto3.client("rds", region_name="eu-west-2").describe_db_instances(
        DBInstanceIdentifier="demo-orders"
    )["DBInstances"][0]["StorageType"]
    _orderable_options_are(
        monkeypatch,
        [
            {
                "Engine": "postgres",
                "EngineVersion": TARGET_VERSION,
                "DBInstanceClass": "db.m6g.large",
                "StorageType": storage_type,
            }
        ],
    )
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")
    assert checks["class_and_storage"].outcome == "pass"


def test_preflight_fails_the_storage_check_when_the_class_is_offered_only_with_other_storage(
    session_factory, sandbox_snapshot, make_step_context, monkeypatch
) -> None:
    _orderable_options_are(
        monkeypatch,
        [
            {
                "Engine": "postgres",
                "EngineVersion": TARGET_VERSION,
                "DBInstanceClass": "db.m6g.large",
                "StorageType": "io2",
            }
        ],
    )
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")
    assert checks["class_and_storage"].outcome == "fail"
    assert "io2" in checks["class_and_storage"].detail


def test_preflight_of_the_aurora_cluster_checks_each_member_class_and_lists_no_replicas(
    session_factory, sandbox_snapshot, make_step_context, monkeypatch
) -> None:
    _orderable_options_are(
        monkeypatch,
        [
            {
                "Engine": "aurora-postgresql",
                "EngineVersion": TARGET_VERSION,
                "DBInstanceClass": "db.r6g.xlarge",
                "StorageType": "aurora",
            }
        ],
    )
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-aurora")
    assert checks["backups"].outcome == "pass"
    assert checks["replicas"].outcome == "pass"
    assert checks["class_and_storage"].outcome == "fail"
    assert "demo-aurora-writer" in checks["class_and_storage"].detail
    assert "db.r6g.large" in checks["class_and_storage"].detail


def test_record_preflight_checks_files_each_check_as_a_diagnostic_with_its_severity(
    session_factory, sandbox_snapshot, make_step_context
) -> None:
    arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    checks = [
        PreflightCheck(name="backups", outcome="pass", detail="retention 7 days"),
        PreflightCheck(name="replicas", outcome="warn", detail="demo-orders-replica", fix_suggestion="later"),
        PreflightCheck(name="in_progress_operations", outcome="fail", detail="busy", fix_suggestion="wait"),
    ]
    with session_factory() as session:
        context = make_step_context(session, arn)
        record_preflight_checks(context.session, context.operation.id, "preflight", checks)
        context.session.commit()
        rows = (
            session.query(DiagnosticReport)
            .filter_by(operation_id=context.operation.id)
            .order_by(DiagnosticReport.id)
            .all()
        )
        assert [(row.diagnostic_kind, row.severity, row.step_name) for row in rows] == [
            ("preflight_backups", "info", "preflight"),
            ("preflight_replicas", "warning", "preflight"),
            ("preflight_in_progress_operations", "error", "preflight"),
        ]
        assert all(row.title and row.fix_suggestion for row in rows)


def test_preflight_fails_while_a_previous_upgrades_blue_side_still_holds_the_old1_name(
    session_factory, sandbox_snapshot, make_step_context
) -> None:
    boto3.client("rds", region_name="eu-west-2").create_db_instance(
        DBInstanceIdentifier="demo-orders-old1",
        DBInstanceClass="db.t4g.micro",
        Engine="postgres",
        EngineVersion="16.4",
        MasterUsername="postgres",
        MasterUserPassword="not-a-secret-1",
        AllocatedStorage=20,
    )
    checks = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")
    check = checks["blue_side_name"]
    assert check.outcome == "fail"
    assert "demo-orders-old1" in check.detail
    assert "decommission" in check.fix_suggestion.lower() and "demo-orders-old1" in check.fix_suggestion


def test_preflight_fails_logical_replication_when_the_instance_group_has_it_off(
    session_factory, operator_settings, config_store, sandbox_snapshot, make_step_context
) -> None:
    from tests.lifecycle.test_settings_changes import _set_live_and_refresh

    _set_live_and_refresh(
        session_factory,
        operator_settings,
        config_store,
        lambda rds: rds.modify_db_parameter_group(
            DBParameterGroupName="demo-postgres16-tuned",
            Parameters=[
                {
                    "ParameterName": "rds.logical_replication",
                    "ParameterValue": "0",
                    "ApplyMethod": "pending-reboot",
                }
            ],
        ),
    )
    check = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")[
        "logical_replication"
    ]
    assert check.outcome == "fail"
    assert "demo-postgres16-tuned" in check.detail
    assert check.fix_suggestion == (
        "set rds.logical_replication to 1 on demo-postgres16-tuned (Edit parameters), reboot demo-orders "
        "(it is a static parameter), then run the upgrade"
    )


def test_preflight_passes_logical_replication_of_the_aurora_cluster_from_its_cluster_group(
    session_factory, sandbox_snapshot, make_step_context
) -> None:
    check = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-aurora")[
        "logical_replication"
    ]
    assert check.outcome == "pass"
    assert "demo-aurora-pg16-tuned" in check.detail


def test_preflight_warns_when_the_group_holding_logical_replication_cannot_be_read(
    session_factory, sandbox_snapshot, make_step_context, monkeypatch
) -> None:
    import rdscommander.upgrades.preflight as preflight_module

    monkeypatch.setattr(preflight_module, "find_parameter_group", lambda *args: None)
    check = _checks(session_factory, sandbox_snapshot, make_step_context, "demo-orders")[
        "logical_replication"
    ]
    assert check.outcome == "warn"
    assert "demo-postgres16-tuned" in check.detail and "inventory" in check.detail
    assert check.fix_suggestion
