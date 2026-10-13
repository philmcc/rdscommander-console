"""Minor engine upgrades and the auto minor upgrade setting, in direct and Terraform mode."""

from __future__ import annotations

import re
from typing import Any

import boto3
import pytest
from pydantic import ValidationError

from rdscommander.catalogue.catalogue import (
    GROUP_UPGRADES,
    definition_for,
    instance_or_cluster_changeable,
    version_changeable,
)
from rdscommander.operations.enums import OperationState
from rdscommander.parameters.metadata import (
    EngineVersionInfo,
    UnknownParameterFamilyError,
    UpgradeTarget,
    load_engine_versions,
)
from rdscommander.resources.queries import load_resource
from rdscommander.upgrades.options import AutoMinorUpgradeOptions, MinorUpgradeOptions
from rdscommander.upgrades.targets import (
    UpgradeTargetNotValid,
    ensure_valid_upgrade_target,
    valid_upgrade_targets,
)
from tests.drift.test_detect_executor import _adopt_estate
from tests.lifecycle.test_settings_changes import (
    AURORA_WRITER_ADDRESS,
    _adopt_aurora_estate,
    _recorded,
    _run,
    _set_live_and_refresh,
    _workspace_dir,
)
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner

MINOR_UPGRADE_DOWNTIME = "a restart of the instance (Multi-AZ fails over)"
AUTO_MINOR_TURNED_OFF_IMPACT = "auto minor upgrade is turned off so the version can be applied"
ORDERS_ADDRESS = "aws_db_instance.demo_orders"


def _bundled_versions(session_factory, engine: str) -> list[EngineVersionInfo]:
    with session_factory() as session:
        return load_engine_versions(session, engine, None, simulated=True)


def _record_modify_calls(calls: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Record the name and arguments of every ModifyDBInstance/ModifyDBCluster call, then pass it on."""
    from botocore.client import BaseClient

    original = BaseClient._make_api_call

    def recording(self, operation_name, api_params):  # noqa: ANN001, ANN202
        if operation_name in ("ModifyDBInstance", "ModifyDBCluster"):
            calls.append((operation_name, dict(api_params)))
        return original(self, operation_name, api_params)

    monkeypatch.setattr(BaseClient, "_make_api_call", recording)


def _record_for(session_factory, console_config, sandbox_snapshot, identifier: str):
    with session_factory() as session:
        return load_resource(session, console_config, arn_for(session_factory, sandbox_snapshot, identifier))


# --- options and targets -----------------------------------------------------------------------------------


def test_minor_upgrade_options_ask_the_version_picker_for_minor_targets_and_default_to_apply_now() -> None:
    options = MinorUpgradeOptions(target_engine_version="16.6")
    assert options.apply_at == "now"
    hints = MinorUpgradeOptions.model_json_schema()["properties"]["target_engine_version"]["ui"]
    assert (hints["widget"], hints["upgrade_kind"], hints["label"]) == (
        "engine_version",
        "minor",
        "Target version",
    )
    with pytest.raises(ValidationError):
        MinorUpgradeOptions(target_engine_version="sixteen")
    with pytest.raises(ValidationError):
        MinorUpgradeOptions(target_engine_version="16.6", allow_major_version_upgrade=True)


def test_auto_minor_upgrade_options_carry_the_flag() -> None:
    assert AutoMinorUpgradeOptions(auto_minor_version_upgrade=False).auto_minor_version_upgrade is False
    with pytest.raises(ValidationError):
        AutoMinorUpgradeOptions()


def test_valid_upgrade_targets_split_the_listed_targets_into_minor_and_major(session_factory) -> None:
    versions = _bundled_versions(session_factory, "postgres")
    assert valid_upgrade_targets(versions, "postgres", "16.4", "minor") == ["16.6"]
    assert valid_upgrade_targets(versions, "postgres", "16.4", "major") == ["17.2"]
    assert valid_upgrade_targets(versions, "postgres", "16.6", "minor") == []
    assert valid_upgrade_targets(versions, "postgres", "9.6", "minor") == []


def test_a_target_the_table_does_not_list_is_refused_naming_the_listed_ones(session_factory) -> None:
    versions = _bundled_versions(session_factory, "postgres")
    with pytest.raises(UpgradeTargetNotValid) as refused:
        ensure_valid_upgrade_target(versions, "postgres", "16.4", "17.2", "minor", "demo-orders")
    assert refused.value.diagnostic_kind == "upgrade_target_not_valid"
    assert "16.6" in refused.value.fix_suggestion and "17.2" in str(refused.value)
    ensure_valid_upgrade_target(versions, "postgres", "16.4", "16.6", "minor", "demo-orders")


# --- catalogue ---------------------------------------------------------------------------------------------


def test_both_kinds_are_in_the_upgrades_group_and_only_the_version_change_is_disruptive() -> None:
    minor = definition_for("minor_upgrade")
    auto_minor = definition_for("auto_minor_upgrade_change")
    assert minor.group == auto_minor.group == GROUP_UPGRADES == "Upgrades"
    assert minor.disruptive is True and minor.destructive is False and minor.changes_the_resource
    assert auto_minor.disruptive is False and auto_minor.changes_the_resource
    assert minor.options_model is MinorUpgradeOptions
    assert auto_minor.options_model is AutoMinorUpgradeOptions
    assert auto_minor.applicability is instance_or_cluster_changeable
    assert minor.applicability is version_changeable


def test_a_minor_upgrade_is_offered_on_instances_and_clusters_but_refused_on_an_aurora_member(
    session_factory, console_config, sandbox_snapshot
) -> None:
    environment = console_config.environment("sandbox").model_copy(update={"change_management": "direct"})
    for identifier in ("demo-orders", "demo-aurora"):
        record = _record_for(session_factory, console_config, sandbox_snapshot, identifier)
        assert version_changeable(record, environment).ok, identifier
    member = _record_for(session_factory, console_config, sandbox_snapshot, "demo-aurora-writer")
    refusal = version_changeable(member, environment)
    assert refusal.ok is False and "cluster" in refusal.reason
    assert instance_or_cluster_changeable(member, environment).ok


# --- direct mode -------------------------------------------------------------------------------------------


def test_direct_minor_upgrade_of_demo_orders_sends_the_version_alone_and_records_it(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    _record_modify_calls(calls, monkeypatch)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "minor_upgrade",
        "demo-orders",
        {"target_engine_version": "16.6"},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    [entry] = finished.plan["change_set"]
    assert (entry["attribute"], entry["current"], entry["desired"]) == ("engine_version", "16.4", "16.6")
    assert entry["downtime"] == MINOR_UPGRADE_DOWNTIME and finished.plan["downtime"] == MINOR_UPGRADE_DOWNTIME
    [(operation_name, arguments)] = calls
    assert operation_name == "ModifyDBInstance"
    assert arguments == {
        "DBInstanceIdentifier": "demo-orders",
        "ApplyImmediately": True,
        "EngineVersion": "16.6",
    }
    assert _recorded(operator_settings, "instances", "demo-orders").engine_version == "16.6"
    described = boto3.client("rds", region_name="eu-west-2").describe_db_instances(
        DBInstanceIdentifier="demo-orders"
    )["DBInstances"][0]
    assert described["EngineVersion"] == "16.6"


@pytest.mark.parametrize("unlisted_target", ["16.5", "17.2"])
def test_direct_minor_upgrade_to_an_unlisted_target_is_refused_before_approval_naming_the_listed_ones(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, unlisted_target
) -> None:
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "minor_upgrade",
        "demo-orders",
        {"target_engine_version": unlisted_target},
    )
    assert finished.state == OperationState.FAILED
    [diagnostic] = [d for d in finished.diagnostic_reports if d.diagnostic_kind == "upgrade_target_not_valid"]
    assert unlisted_target in diagnostic.detail and "16.4" in diagnostic.detail
    assert "16.6" in diagnostic.fix_suggestion
    assert "await_approval" not in [attempt.step_name for attempt in finished.step_attempts]


def test_a_listed_target_whose_family_metadata_is_missing_still_upgrades(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    # Review Focus 3: only the engine-version table decides; the parameter group family of the target is never
    # consulted, so a version AWS lists before the console knows its family metadata is still a valid target.
    import rdscommander.parameters.changes as parameter_changes
    import rdscommander.parameters.metadata as metadata
    import rdscommander.upgrades.targets as targets

    listed = [
        EngineVersionInfo(
            engine="postgres",
            engine_version="16.4",
            parameter_group_family="postgres16",
            valid_upgrade_targets=[UpgradeTarget(engine_version="16.8", is_major=False)],
        )
    ]
    monkeypatch.setattr(targets, "load_engine_versions", lambda *args, **kwargs: listed)

    def no_family_metadata(*args: Any, **kwargs: Any) -> Any:
        raise UnknownParameterFamilyError("postgres16")

    monkeypatch.setattr(metadata, "load_family_metadata", no_family_metadata)
    monkeypatch.setattr(parameter_changes, "load_family_metadata", no_family_metadata)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "minor_upgrade",
        "demo-orders",
        {"target_engine_version": "16.8"},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert finished.plan["change_set"][0]["desired"] == "16.8"


def test_direct_minor_upgrade_waits_until_aws_reports_the_instance_available_again(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    import rdscommander.upgrades.minor as minor

    statuses = iter(["upgrading", "upgrading", "available"])
    read_statuses: list[str] = []

    def status_while_upgrading(*args: Any, **kwargs: Any) -> str:
        status = next(statuses)
        read_statuses.append(status)
        return status

    monkeypatch.setattr(minor, "read_resource_status", status_while_upgrading)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "minor_upgrade",
        "demo-orders",
        {"target_engine_version": "16.6"},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert read_statuses == ["upgrading", "upgrading", "available"]


def test_direct_minor_upgrade_of_a_cluster_records_and_explains_the_members_new_version(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.control_db.models import utc_now
    from rdscommander.history.change_events import explain_event

    calls: list[tuple[str, dict[str, Any]]] = []
    _record_modify_calls(calls, monkeypatch)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "minor_upgrade",
        "demo-aurora",
        {"target_engine_version": "16.6"},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert [name for name, _ in calls] == ["ModifyDBCluster"]
    assert calls[0][1]["EngineVersion"] == "16.6" and "AllowMajorVersionUpgrade" not in calls[0][1]
    assert _recorded(operator_settings, "clusters", "demo-aurora").engine_version == "16.6"
    for member in ("demo-aurora-writer", "demo-aurora-reader"):
        assert _recorded(operator_settings, "cluster_instances", member).engine_version == "16.6"
    member_arns = {
        arn_for(session_factory, sandbox_snapshot, member)
        for member in ("demo-aurora-writer", "demo-aurora-reader")
    }
    assert set(finished.plan["affected_resource_arns"]) == member_arns
    with session_factory() as session:
        for member_arn in member_arns:
            assert explain_event(session, member_arn, None, utc_now()) == ("operation", finished.id)


def test_direct_auto_minor_upgrade_change_turns_it_off_on_an_aurora_member(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "auto_minor_upgrade_change",
        "demo-aurora-writer",
        {"auto_minor_version_upgrade": False},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    [entry] = finished.plan["change_set"]
    assert (entry["attribute"], entry["current"], entry["desired"]) == (
        "auto_minor_version_upgrade",
        True,
        False,
    )
    assert entry["downtime"] == "none"
    assert (
        _recorded(operator_settings, "cluster_instances", "demo-aurora-writer").auto_minor_version_upgrade
        is False
    )


# --- Terraform mode ----------------------------------------------------------------------------------------


def _create_and_review(service, session_factory, sandbox_snapshot, identifier: str, key: str):
    arn = arn_for(session_factory, sandbox_snapshot, identifier)
    return service.create(
        "minor_upgrade",
        "sandbox",
        TARGET_KEY,
        {"options": {"target_engine_version": "16.6"}, "resource_arn": arn},
        key,
        "phil",
        resource_arn=arn,
        title=f"Minor upgrade: {identifier}",
    )


def test_terraform_minor_upgrade_of_an_auto_minor_instance_turns_auto_minor_off_and_says_so(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    record = _record_for(session_factory, console_config, sandbox_snapshot, "demo-orders")
    assert record.facts["auto_minor_version_upgrade"] is True
    update = {
        "resource_changes": [
            {
                "address": ORDERS_ADDRESS,
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"engine_version": "16.4", "auto_minor_version_upgrade": True},
                    "after": {"engine_version": "16.6", "auto_minor_version_upgrade": False},
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    operation = _create_and_review(service, session_factory, sandbox_snapshot, "demo-orders", "minor-tf")
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert MINOR_UPGRADE_DOWNTIME in awaiting.plan["downtime"]
    assert AUTO_MINOR_TURNED_OFF_IMPACT in awaiting.plan["downtime"]
    rendered = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    orders_block = rendered.split('resource "aws_db_instance" "demo_orders"')[1].split("\nresource ")[0]
    assert re.search(r'engine_version\s*=\s*"16\.6"', orders_block)
    assert re.search(r"auto_minor_version_upgrade\s*=\s*false", orders_block)
    assert "engine_version]" not in orders_block and "engine_version," not in orders_block
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    recorded = _recorded(operator_settings, "instances", "demo-orders")
    assert (recorded.engine_version, recorded.auto_minor_version_upgrade) == ("16.6", False)


def test_terraform_minor_upgrade_of_an_instance_without_auto_minor_changes_only_the_version(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _set_live_and_refresh(
        session_factory,
        operator_settings,
        config_store,
        lambda rds: rds.modify_db_instance(
            DBInstanceIdentifier="demo-orders", AutoMinorVersionUpgrade=False, ApplyImmediately=True
        ),
    )
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update = {
        "resource_changes": [
            {
                "address": ORDERS_ADDRESS,
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"engine_version": "16.4"},
                    "after": {"engine_version": "16.6"},
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    operation = _create_and_review(
        service, session_factory, sandbox_snapshot, "demo-orders", "minor-tf-no-auto"
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert awaiting.plan["downtime"] == MINOR_UPGRADE_DOWNTIME


def test_terraform_minor_upgrade_of_a_cluster_renders_the_new_version_on_its_members_too(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_aurora_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update = {
        "resource_changes": [
            {
                "address": "aws_rds_cluster.demo_aurora",
                "type": "aws_rds_cluster",
                "change": {
                    "actions": ["update"],
                    "before": {"engine_version": "16.4"},
                    "after": {"engine_version": "16.6"},
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    operation = _create_and_review(
        service, session_factory, sandbox_snapshot, "demo-aurora", "minor-tf-cluster"
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert set(awaiting.plan["touched_addresses"]) == {
        "aws_rds_cluster.demo_aurora",
        AURORA_WRITER_ADDRESS,
        "aws_rds_cluster_instance.demo_aurora_reader",
    }
    rendered = (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    assert len(re.findall(r'engine_version\s*=\s*"16\.6"', rendered)) == 3
    assert not re.search(r'engine_version\s*=\s*"16\.4"', rendered)
