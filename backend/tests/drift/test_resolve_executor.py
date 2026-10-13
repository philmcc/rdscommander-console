import subprocess
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from fastapi.testclient import TestClient

from rdscommander.control_db.models import (
    ChangeSet,
    DriftFinding,
    ExecutionRecord,
    InventorySnapshot,
    Operation,
    ResourceAdoption,
    TerraformWorkspace,
)
from rdscommander.drift import resolve_executor as resolve_executor_module
from rdscommander.drift import restore_change as restore_change_module
from rdscommander.main import create_application
from rdscommander.operations.change_strategy import DIRECT_STEPS, TERRAFORM_STEPS
from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService
from rdscommander.repo.commit import GitCommitError
from rdscommander.terraform.record import read_record
from rdscommander.terraform.renderer import render_workspace_files, write_workspace_files
from tests.drift.test_detect_executor import (
    GROUP_ADDRESS,
    ORDERS_ADDRESS,
    REPLICA_ADDRESS,
    _adopt_estate,
    _adopt_group,
    _plan_changing,
    _refresh_and_detect,
    _set_work_mem,
    _state_plan,
)
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import _git

WORKSPACE_RELATIVE_DIR = "terraform/sandbox/000000000000/eu-west-2"


def _workspace_dir(operator_settings):
    return operator_settings.repo_root / WORKSPACE_RELATIVE_DIR


def _workspace_changes(operator_settings) -> list[str]:
    """`git status --porcelain` for the workspace: modified, staged and untracked files."""
    return _git(
        "status", "--porcelain", "--untracked-files=all", "--", "terraform", cwd=operator_settings.repo_root
    ).splitlines()


def _head(operator_settings) -> str:
    return _git("rev-parse", "HEAD", cwd=operator_settings.repo_root)


def _open_finding_count(session_factory) -> int:
    with session_factory() as session:
        return session.query(DriftFinding).filter_by(resolved_at_utc=None).count()


def _open_finding_identifiers(session_factory) -> set[str]:
    with session_factory() as session:
        return {
            f.resource_arn.rsplit(":", 1)[-1]
            for f in session.query(DriftFinding).filter_by(resolved_at_utc=None)
        }


def _resolve(service, resource_key: str, choice: str, key: str, target_key: str = TARGET_KEY):
    return service.create(
        "drift_resolve",
        "sandbox",
        target_key,
        {"options": {"resource_arn": resource_key, "choice": choice}, "resource_arn": resource_key},
        key,
        "phil",
        resource_arn=resource_key,
        title=f"Resolve drift: {resource_key.rsplit(':', 1)[-1]}",
    )


def _drifted_group(session_factory, operator_settings, config_store, console_config, sandbox_snapshot, key):
    """An adopted group whose work_mem was changed in AWS and detected; returns runner, service, group ARN."""
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, key)
    return runner, service, arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")


def _change_orders_instance_class(instance_class: str = "db.r6g.xlarge") -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", DBInstanceClass=instance_class, ApplyImmediately=True
    )


def _work_mem_in_record(operator_settings) -> str:
    groups = read_record(_workspace_dir(operator_settings)).render_input.instance_parameter_groups
    return next(p.value for g in groups for p in g.parameters if p.name == "work_mem")


def _mark_orders_adopted_without_recording_it(session_factory, sandbox_snapshot) -> None:
    """`demo-orders` is adopted according to the control database but was never recorded or committed."""
    with session_factory() as session:
        workspace = session.query(TerraformWorkspace).filter_by(target_key=TARGET_KEY).one()
        session.add(
            ResourceAdoption(
                workspace_id=workspace.id,
                resource_arn=arn_for(session_factory, sandbox_snapshot, "demo-orders"),
                resource_identifier="demo-orders",
                resource_type="aws_db_instance",
                terraform_address=ORDERS_ADDRESS,
                adoption_status="adopted",
            )
        )
        session.commit()


def test_accept_rerenders_commits_and_closes_the_finding(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[
            {
                "ParameterName": "work_mem",
                "ParameterValue": "65536",
                "ApplyMethod": "immediate",
                "Source": "user",
            }
        ],
    )
    service.create("inventory_refresh", "sandbox", None, {}, "accept-refresh", "phil")
    runner.run_once()
    service.create("drift_detect", "sandbox", None, {"options": {}}, "accept-detect", "phil")
    runner.run_once()
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    resolve = service.create(
        "drift_resolve",
        "sandbox",
        TARGET_KEY,
        {"options": {"resource_arn": group_arn, "choice": "accept"}, "resource_arn": group_arn},
        "accept-1",
        "phil",
        resource_arn=group_arn,
        title="Resolve drift: demo-postgres16-tuned",
    )
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "65536" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    assert _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root).startswith(
        "Resolve drift: demo-postgres16-tuned"
    )
    with session_factory() as session:
        assert session.query(DriftFinding).filter_by(resolved_at_utc=None).count() == 0


def test_accept_in_terraform_mode_guards_refreshes_state_verifies_a_clean_plan_and_commits_once(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "tf"
    )
    fake = runner.services.terraform_runner
    calls_before = len(fake.calls)
    resolve = _resolve(service, group_arn, "accept", "accept-tf")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert fake.calls[calls_before:] == ["apply_refresh_only", "plan:verify_clean_plan"]
    assert [a.step_name for a in final.step_attempts] == [
        "check_checkout",
        "render_live",
        "refresh_state",
        "verify_clean_plan",
        "commit_record",
        "close_findings",
    ]
    message = _git("log", "-1", "--format=%B", cwd=operator_settings.repo_root).strip()
    assert message == "Resolve drift: demo-postgres16-tuned (phil)", "accepting has no approval to name"
    assert _workspace_changes(operator_settings) == []
    assert _work_mem_in_record(operator_settings) == "65536", "the record now holds what AWS has"
    assert _open_finding_count(session_factory) == 0


def test_accept_then_detect_finds_nothing_left_to_resolve(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "again"
    )
    _resolve(service, group_arn, "accept", "accept-again")
    runner.run_once()
    detect = _refresh_and_detect(runner, service, "again-2")
    assert detect.state == OperationState.SUCCEEDED
    with session_factory() as session:
        assert session.query(DriftFinding).count() == 1, "the same drift is not found a second time"
    assert _open_finding_count(session_factory) == 0


def test_accepting_one_drifted_resource_leaves_the_other_one_and_its_recorded_values_untouched(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    recorded_before = read_record(_workspace_dir(operator_settings)).render_input
    orders_class_before = next(i for i in recorded_before.instances if i.identifier == "demo-orders")
    instances_file_before = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    _set_work_mem("65536")
    _change_orders_instance_class()
    _refresh_and_detect(runner, service, "two")
    assert _open_finding_identifiers(session_factory) == {"demo-postgres16-tuned", "demo-orders"}

    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    resolve = _resolve(service, group_arn, "accept", "accept-one-of-two")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]

    assert _work_mem_in_record(operator_settings) == "65536"
    recorded_after = read_record(_workspace_dir(operator_settings)).render_input
    orders_after = next(i for i in recorded_after.instances if i.identifier == "demo-orders")
    assert orders_after.instance_class == orders_class_before.instance_class != "db.r6g.xlarge"
    assert (_workspace_dir(operator_settings) / "instances.tf").read_text() == instances_file_before
    assert _open_finding_identifiers(session_factory) == {"demo-orders"}
    changed_files = _git(
        "show", "--name-only", "--format=", "HEAD", cwd=operator_settings.repo_root
    ).splitlines()
    assert sorted(changed_files) == [
        f"{WORKSPACE_RELATIVE_DIR}/console-record.json",
        f"{WORKSPACE_RELATIVE_DIR}/parameter_groups.tf",
    ]
    [render_attempt] = [a for a in final.step_attempts if a.step_name == "render_live"]
    assert render_attempt.output["address"] == GROUP_ADDRESS

    detect = _refresh_and_detect(runner, service, "two-after")
    assert detect.state == OperationState.SUCCEEDED
    with session_factory() as session:
        [open_finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
    assert [a["attribute"] for a in open_finding.attributes] == ["instance_class"]
    assert open_finding.attributes[0]["recorded"] == orders_class_before.instance_class

    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    second = _resolve(service, orders_arn, "accept", "accept-the-other")
    runner.run_once()
    assert service.get(second.id).state == OperationState.SUCCEEDED
    assert _open_finding_count(session_factory) == 0
    orders_accepted = next(
        i
        for i in read_record(_workspace_dir(operator_settings)).render_input.instances
        if i.identifier == "demo-orders"
    )
    assert orders_accepted.instance_class == "db.r6g.xlarge"


def test_accept_does_not_wait_for_other_resources_that_still_show_their_own_plan_changes(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _set_work_mem("65536")
    _change_orders_instance_class()
    _refresh_and_detect(runner, service, "others")
    runner.services.terraform_runner.plan_scripts[:] = [
        _plan_changing(
            ORDERS_ADDRESS, {"instance_class": "db.r6g.xlarge"}, {"instance_class": "db.m6g.large"}
        )
    ]
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    resolve = _resolve(service, group_arn, "accept", "accept-others-dirty")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    [verified] = [a for a in final.step_attempts if a.step_name == "verify_clean_plan"]
    assert verified.output == {"address": GROUP_ADDRESS, "other_addresses_with_changes": [ORDERS_ADDRESS]}


def test_accept_fails_and_reverts_the_workspace_when_the_chosen_resource_does_not_plan_clean(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "dirty"
    )
    head_before = _head(operator_settings)
    record_before = (_workspace_dir(operator_settings) / "console-record.json").read_text()
    runner.services.terraform_runner.plan_scripts[:] = [_state_plan("in state", "in files")]
    resolve = _resolve(service, group_arn, "accept", "accept-dirty")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = final.diagnostic_reports
    assert diagnostic.diagnostic_kind == "residual_plan_changes" and diagnostic.fix_suggestion
    assert f"{GROUP_ADDRESS}: update on description" in diagnostic.detail
    assert "The workspace files were reverted to the last commit." in diagnostic.detail
    assert _head(operator_settings) == head_before
    assert _workspace_changes(operator_settings) == []
    assert (_workspace_dir(operator_settings) / "console-record.json").read_text() == record_before
    assert _open_finding_count(session_factory) == 1, "an unverified acceptance must not close the finding"


def test_accept_fails_and_reverts_the_workspace_when_the_state_refresh_fails(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "refresh"
    )
    fake = runner.services.terraform_runner
    fake.apply_refresh_only = lambda *args, **kwargs: replace(
        fake._ok(), exit_code=1, standard_error="Error: state lock held"
    )
    head_before = _head(operator_settings)
    calls_before = len(fake.calls)
    resolve = _resolve(service, group_arn, "accept", "accept-refresh")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = final.diagnostic_reports
    assert diagnostic.diagnostic_kind == "terraform_refresh_failed" and "state lock held" in diagnostic.detail
    assert "reverted" in diagnostic.detail
    assert fake.calls[calls_before:] == [], "a failed refresh stops before the clean-plan check"
    assert _head(operator_settings) == head_before
    assert _workspace_changes(operator_settings) == []
    assert _open_finding_count(session_factory) == 1


def test_accept_reverts_staged_files_when_the_commit_fails(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "commit"
    )

    def stage_then_fail(recorder, repo_root, relative_paths, *arguments, **keywords):
        subprocess.run(
            ["git", "add", "-A", "--", *[p.as_posix() for p in relative_paths]], cwd=repo_root, check=True
        )
        raise GitCommitError("hook rejected the commit")

    monkeypatch.setattr(resolve_executor_module, "commit_paths", stage_then_fail)
    head_before = _head(operator_settings)
    resolve = _resolve(service, group_arn, "accept", "accept-commit")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = final.diagnostic_reports
    assert diagnostic.diagnostic_kind == "commit_failed" and "hook rejected the commit" in diagnostic.detail
    assert "reverted" in diagnostic.detail
    assert _head(operator_settings) == head_before
    assert _workspace_changes(operator_settings) == [], (
        "neither the staged nor the working-tree changes remain"
    )
    assert _open_finding_count(session_factory) == 1


def test_accept_reverts_the_workspace_when_a_step_raises_unexpectedly(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "raises"
    )

    def explode(plan_json):
        raise RuntimeError("plan json could not be summarised")

    monkeypatch.setattr(resolve_executor_module, "summarise_plan_json", explode)
    runner.services.terraform_runner.plan_scripts[:] = [_state_plan("a", "b")]
    resolve = _resolve(service, group_arn, "accept", "accept-raises")
    runner.run_once()
    assert service.get(resolve.id).state == OperationState.FAILED
    assert _workspace_changes(operator_settings) == []
    assert _open_finding_count(session_factory) == 1


def test_a_failed_accept_removes_files_it_created_that_the_last_commit_does_not_have(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _mark_orders_adopted_without_recording_it(session_factory, sandbox_snapshot)
    _refresh_and_detect(runner, service, "unrecorded")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
    assert finding.resource_arn == orders_arn and finding.attributes[0]["recorded"] is False
    assert not (_workspace_dir(operator_settings) / "instances.tf").exists()

    runner.services.terraform_runner.plan_scripts[:] = [
        _plan_changing(ORDERS_ADDRESS, {"allocated_storage": 20}, {"allocated_storage": 100})
    ]
    resolve = _resolve(service, orders_arn, "accept", "accept-unrecorded")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    assert [d.diagnostic_kind for d in final.diagnostic_reports] == ["residual_plan_changes"]
    assert not (_workspace_dir(operator_settings) / "instances.tf").exists(), (
        "created by the render, reverted"
    )
    assert _workspace_changes(operator_settings) == []


def test_accepting_a_resource_that_is_adopted_but_not_recorded_adds_it_to_the_record(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _mark_orders_adopted_without_recording_it(session_factory, sandbox_snapshot)
    _refresh_and_detect(runner, service, "add")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    resolve = _resolve(service, orders_arn, "accept", "accept-add")
    runner.run_once()
    assert service.get(resolve.id).state == OperationState.SUCCEEDED
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert [i.identifier for i in recorded.instances] == ["demo-orders"]
    assert (_workspace_dir(operator_settings) / "instances.tf").exists()
    assert _open_finding_count(session_factory) == 0


def test_accept_in_direct_mode_runs_no_terraform_and_still_commits(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, fake = direct_mode_runner
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "direct")
    assert _open_finding_count(session_factory) == 1
    resolve = _resolve(service, group_arn, "accept", "accept-direct")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert fake.calls == []
    assert _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root).startswith(
        "Resolve drift: demo-postgres16-tuned"
    )
    assert _work_mem_in_record(operator_settings) == "65536"
    assert _open_finding_count(session_factory) == 0


def _live_work_mem() -> str:
    live = boto3.client("rds", region_name="eu-west-2").describe_db_parameters(
        DBParameterGroupName="demo-postgres16-tuned", Source="user"
    )["Parameters"]
    return next(p["ParameterValue"] for p in live if p["ParameterName"] == "work_mem")


def _direct_mode_runner_after_adoption(session_factory, operator_settings, config_store):
    """A second runner for the same sandbox whose environment now changes AWS directly."""
    from tests.operations.test_change_strategy import _direct_store, _runner

    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    return runner, OperationService(session_factory)


def test_restore_reverses_the_drift_through_the_mode_strategy(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _adopt_group(session_factory, operator_settings, config_store, console_config, sandbox_snapshot)
    runner, service = _direct_mode_runner_after_adoption(session_factory, operator_settings, config_store)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[
            {
                "ParameterName": "work_mem",
                "ParameterValue": "65536",
                "ApplyMethod": "immediate",
                "Source": "user",
            }
        ],
    )
    service.create("inventory_refresh", "sandbox", None, {}, "restore-refresh", "phil")
    runner.run_once()
    service.create("drift_detect", "sandbox", None, {"options": {}}, "restore-detect", "phil")
    runner.run_once()
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    resolve = service.create(
        "drift_resolve",
        "sandbox",
        TARGET_KEY,
        {"options": {"resource_arn": group_arn, "choice": "restore"}, "resource_arn": group_arn},
        "restore-1",
        "phil",
        resource_arn=group_arn,
        title="Resolve drift: demo-postgres16-tuned",
    )
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert [a.step_name for a in paused.step_attempts] == DIRECT_STEPS[:4]
    with session_factory() as session:
        [change_set] = session.query(ChangeSet).filter_by(operation_id=resolve.id).all()
        assert change_set.entries[0]["current"] == "65536" and change_set.entries[0]["desired"] == "16384"
        assert (change_set.entries[0]["apply_type"], change_set.entries[0]["requires_reboot"]) == (
            "immediate",
            False,
        ), "work_mem is dynamic in postgres16, so the restore applies it at once without a reboot"
    assert paused.plan["requires_reboot"] is False
    assert _live_work_mem() == "65536", "nothing changes in AWS before approval"
    service.approve(resolve.id, "sam")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    completed = [a.step_name for a in final.step_attempts if a.outcome_status == "completed"]
    assert completed[-5:] == [*DIRECT_STEPS[-4:], "close_findings"]
    assert _live_work_mem() == "16384"
    assert _work_mem_in_record(operator_settings) == "16384"
    assert _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root) == (
        "Resolve drift: demo-postgres16-tuned (phil, approved by sam)"
    )
    with session_factory() as session:
        group_findings = session.query(DriftFinding).filter_by(resource_arn=group_arn).all()
        assert group_findings and all(f.resolved_by_operation_id == resolve.id for f in group_findings)
        [modify] = (
            session.query(ExecutionRecord)
            .filter_by(operation_id=resolve.id, api_name="rds:ModifyDBParameterGroup")
            .all()
        )
        assert modify.request_parameters["Parameters"][0]["ApplyMethod"] == "immediate"


def test_restore_of_a_static_parameter_applies_at_the_next_reboot_and_says_so(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _adopt_group(session_factory, operator_settings, config_store, console_config, sandbox_snapshot)
    runner, service = _direct_mode_runner_after_adoption(session_factory, operator_settings, config_store)
    boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[
            {
                "ParameterName": "shared_buffers",
                "ParameterValue": "{DBInstanceClassMemory/16384}",
                "ApplyMethod": "pending-reboot",
                "Source": "user",
            }
        ],
    )
    service.create("inventory_refresh", "sandbox", None, {}, "static-refresh", "phil")
    runner.run_once()
    service.create("drift_detect", "sandbox", None, {"options": {}}, "static-detect", "phil")
    runner.run_once()
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    resolve = service.create(
        "drift_resolve",
        "sandbox",
        TARGET_KEY,
        {"options": {"resource_arn": group_arn, "choice": "restore"}, "resource_arn": group_arn},
        "restore-static",
        "phil",
        resource_arn=group_arn,
        title="Resolve drift: demo-postgres16-tuned",
    )
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    with session_factory() as session:
        [change_set] = session.query(ChangeSet).filter_by(operation_id=resolve.id).all()
        [entry] = change_set.entries
        assert entry["attribute"] == "parameters.shared_buffers"
        assert (entry["apply_type"], entry["requires_reboot"]) == ("pending-reboot", True)
    assert paused.plan["requires_reboot"] is True
    assert paused.plan["downtime"] == "a reboot of every instance using the group applies the restored values"
    assert paused.approval_required_text is None, "nothing is interrupted now; the review flags the reboot"


def test_restore_in_terraform_mode_plans_applies_the_recorded_files_and_closes_the_finding(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "restore-tf"
    )
    update_plan = _plan_changing(
        GROUP_ADDRESS,
        {"parameter": [{"name": "work_mem", "value": "65536"}]},
        {"parameter": [{"name": "work_mem", "value": "16384"}]},
    )
    fake = runner.services.terraform_runner
    fake.plan_scripts[:] = [update_plan, update_plan, {"resource_changes": []}]
    applied_before_restore = len(fake.applied_plan_files)
    resolve = _resolve(service, group_arn, "restore", "restore-tf-1")
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert [a.step_name for a in paused.step_attempts] == TERRAFORM_STEPS[
        : TERRAFORM_STEPS.index("await_approval") + 1
    ]
    assert paused.plan["downtime"] == "none" and paused.approval_required_text is None  # work_mem is dynamic
    assert _work_mem_in_record(operator_settings) == "16384"
    assert "65536" not in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    service.approve(resolve.id, "sam")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts if a.outcome_status == "completed"][-4:] == [
        *TERRAFORM_STEPS[-3:],
        "close_findings",
    ]
    assert fake.applied_plan_files[applied_before_restore:] == ["console.tfplan"]
    assert _open_finding_count(session_factory) == 0
    assert _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root) == (
        "Resolve drift: demo-postgres16-tuned (phil, approved by sam)"
    )
    assert _workspace_changes(operator_settings) == []


def test_restore_of_a_resource_that_left_the_inventory_is_refused_and_points_at_accepting(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    boto3.client("rds", region_name="eu-west-2").delete_db_instance(
        DBInstanceIdentifier="demo-orders-replica", SkipFinalSnapshot=True
    )
    _refresh_and_detect(runner, service, "restore-vanished")
    head_before = _head(operator_settings)
    resolve = _resolve(service, REPLICA_ADDRESS, "restore", "restore-vanished-1")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = final.diagnostic_reports
    assert diagnostic.diagnostic_kind == "restore_not_possible"
    assert "no longer exists" in diagnostic.detail and "accept the drift" in diagnostic.fix_suggestion
    assert _head(operator_settings) == head_before and _workspace_changes(operator_settings) == []
    assert _open_finding_count(session_factory) == 1


def test_restore_of_an_attribute_the_console_cannot_write_is_refused_before_approval(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", PubliclyAccessible=True, ApplyImmediately=True
    )
    _refresh_and_detect(runner, service, "restore-unsupported")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    resolve = _resolve(service, orders_arn, "restore", "restore-unsupported-1")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = final.diagnostic_reports
    assert (
        diagnostic.diagnostic_kind == "restore_not_supported" and "publicly_accessible" in diagnostic.detail
    )
    assert diagnostic.fix_suggestion == "accept this drift into the record or change the attribute by hand"
    assert "await_approval" not in [a.step_name for a in final.step_attempts]


def test_restore_of_a_drifted_engine_version_is_refused_because_aws_never_downgrades() -> None:
    # A minor upgrade made engine_version writable; the drift restore must not offer a downgrade, which AWS
    # refuses, so the attribute stays one the restore cannot write back.
    from rdscommander.drift.restore_change import RestoreFromRecordChange
    from rdscommander.parameters.direct_api import UnsupportedRestoreError
    from rdscommander.terraform.render_input import RenderWorkspaceInput

    change = RestoreFromRecordChange(
        "arn:aws:rds:eu-west-2:000000000000:db:demo-orders",
        "aws_db_instance.demo_orders",
        [{"address": "aws_db_instance.demo_orders", "attribute": "engine_version", "recorded": "16.4"}],
        RenderWorkspaceInput.model_construct(),
    )
    with pytest.raises(UnsupportedRestoreError) as refused:
        change.change_set(None)  # type: ignore[arg-type]
    assert refused.value.attribute == "engine_version"


def test_restore_changes_an_instance_attribute_directly_and_records_the_result(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    rds = boto3.client("rds", region_name="eu-west-2")
    original = rds.describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"][0][
        "BackupRetentionPeriod"
    ]
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=original + 4, ApplyImmediately=True
    )
    _refresh_and_detect(runner, service, "restore-instance")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    resolve = _resolve(service, orders_arn, "restore", "restore-instance-1")
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    with session_factory() as session:
        [change_set] = session.query(ChangeSet).filter_by(operation_id=resolve.id).all()
        assert [(e["attribute"], e["current"], e["desired"]) for e in change_set.entries] == [
            ("backup_retention_period", original + 4, original)
        ]
        assert [(e["apply_type"], e["requires_reboot"], e["downtime"]) for e in change_set.entries] == [
            ("immediate", False, "none")
        ]
    assert not [d for d in paused.diagnostic_reports if d.severity == "warning"], "nothing was queued in AWS"
    service.approve(resolve.id, "sam")
    runner.run_once()
    assert service.get(resolve.id).state == OperationState.SUCCEEDED
    after = rds.describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"][0]
    assert after["BackupRetentionPeriod"] == original
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert (
        next(i for i in recorded.instances if i.identifier == "demo-orders").backup_retention_period
        == original
    )
    assert _open_finding_count(session_factory) == 0


def test_restoring_the_instance_class_says_the_instance_restarts(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    _change_orders_instance_class("db.r6g.xlarge")
    _refresh_and_detect(runner, service, "restore-class")
    resolve = _resolve(
        service, arn_for(session_factory, sandbox_snapshot, "demo-orders"), "restore", "restore-class-1"
    )
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    [entry] = paused.plan["change_set"]
    assert (entry["attribute"], entry["apply_type"], entry["requires_reboot"]) == (
        "instance_class",
        "immediate",
        False,
    )
    assert entry["downtime"] == "a restart of the instance (Multi-AZ fails over to the standby)"
    assert paused.plan["downtime"] == entry["downtime"]


def test_restore_warns_when_aws_has_modifications_queued_that_the_restore_will_apply_too(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    runner, service, _ = direct_mode_runner
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=11, ApplyImmediately=True
    )
    _refresh_and_detect(runner, service, "restore-queued")
    monkeypatch.setattr(
        restore_change_module,
        "read_pending_modifications",
        lambda context, address, step_name="compute_change_set": {
            "DBInstanceClass": "db.r6g.2xlarge",
            "MultiAZ": False,
            "ResumeFullAutomationModeTime": datetime(2026, 10, 11, 3, 0, tzinfo=UTC),
        },
    )
    resolve = _resolve(
        service, arn_for(session_factory, sandbox_snapshot, "demo-orders"), "restore", "restore-queued-1"
    )
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    [warning] = [
        d for d in paused.diagnostic_reports if d.diagnostic_kind == "pending_modifications_will_apply"
    ]
    assert warning.severity == "warning" and warning.step_name == "compute_change_set"
    assert "DBInstanceClass" in warning.detail and "db.r6g.2xlarge" in warning.detail
    assert '"MultiAZ": false' in warning.detail, "a queued False is a modification"
    assert "2026-10-11 03:00:00+00:00" in warning.detail, "a datetime value is written out, not a crash"
    assert warning.fix_suggestion


def test_restore_stops_waiting_when_aws_reports_a_status_that_will_not_recover(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    runner, service, _ = direct_mode_runner
    sleeps: list[float] = []
    runner.services.sleep = sleeps.append
    rds = boto3.client("rds", region_name="eu-west-2")
    original = rds.describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"][0][
        "BackupRetentionPeriod"
    ]
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=original + 4, ApplyImmediately=True
    )
    _refresh_and_detect(runner, service, "restore-terminal")
    resolve = _resolve(
        service, arn_for(session_factory, sandbox_snapshot, "demo-orders"), "restore", "restore-terminal-1"
    )
    runner.run_once()
    # AWS accepts the call but the modification fails: the value never changes and the status says why.
    monkeypatch.setattr(
        restore_change_module, "apply_attribute_values", lambda context, address, values, **_: dict(values)
    )
    monkeypatch.setattr(
        restore_change_module, "read_resource_status", lambda context, address: "storage-full"
    )
    service.approve(resolve.id, "sam")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    assert sleeps == [], "a terminal status ends the wait at once"
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "direct_change_not_verified"]
    assert "storage-full" in diagnostic.detail and "backup_retention_period" in diagnostic.detail
    assert "submitted to AWS" in diagnostic.detail
    assert final.plan["applied_without_record"] is True
    assert _open_finding_count(session_factory) == 1, "the drift stays open"


def test_restore_of_a_parameter_group_is_never_terminal(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    runner, service, _ = direct_mode_runner
    sleeps: list[float] = []
    runner.services.sleep = sleeps.append
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "restore-group-wait")
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    resolve = _resolve(service, group_arn, "restore", "restore-group-wait-1")
    runner.run_once()
    monkeypatch.setattr(
        restore_change_module, "apply_attribute_values", lambda context, address, values, **_: dict(values)
    )
    service.approve(resolve.id, "sam")
    runner.settings.direct_verify_timeout_seconds = 30
    runner.settings.direct_verify_poll_seconds = 10.0
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    assert sleeps == [10.0, 10.0, 10.0], "a group has no status that could end the wait early"


def test_restore_has_nothing_to_do_when_aws_already_matches_the_record_again(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "restore-moot")
    _set_work_mem("16384")
    resolve = _resolve(service, group_arn, "restore", "restore-moot-1")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    assert [d.diagnostic_kind for d in final.diagnostic_reports] == ["nothing_to_change"]


# Guards ---------------------------------------------------------------------


def test_accept_is_refused_by_the_checkout_guard_before_anything_is_written(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "guard"
    )
    stray_file = _workspace_dir(operator_settings) / "stray.tf"
    stray_file.write_text("# someone's uncommitted work\n")
    head_before = _head(operator_settings)
    record_before = (_workspace_dir(operator_settings) / "console-record.json").read_text()
    resolve = _resolve(service, group_arn, "accept", "accept-guard")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    assert [a.step_name for a in final.step_attempts] == ["check_checkout"]
    assert [d.diagnostic_kind for d in final.diagnostic_reports] == ["checkout_guard"]
    assert stray_file.exists(), "the stranger's file is left alone"
    assert (_workspace_dir(operator_settings) / "console-record.json").read_text() == record_before
    assert _head(operator_settings) == head_before


def test_accept_is_refused_when_the_inventory_snapshot_is_too_old(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "stale"
    )
    with session_factory() as session:
        for snapshot in session.query(InventorySnapshot).filter_by(target_key=TARGET_KEY):
            snapshot.captured_at_utc = datetime.now(UTC) - timedelta(days=3)
        session.commit()
    resolve = _resolve(service, group_arn, "accept", "accept-stale")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = final.diagnostic_reports
    assert (
        diagnostic.diagnostic_kind == "stale_inventory" and "refresh inventory" in diagnostic.fix_suggestion
    )
    assert _workspace_changes(operator_settings) == []
    assert _open_finding_count(session_factory) == 1


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "AWAITING_APPROVAL"])
def test_accept_is_refused_while_an_adoption_of_the_target_is_in_progress(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, state: str
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, f"busy-{state}"
    )
    with session_factory() as session:
        session.add(
            Operation(
                operation_kind="terraform_adoption",
                state=state,
                environment_slug="sandbox",
                target_key=TARGET_KEY,
                plan={},
                idempotency_key=f"other-adoption-{state}",
                created_by_operator_name="phil",
                title="Adopt resources: demo-orders",
                lease_owner="another-runner",
                lease_expires_at_utc=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.commit()
    head_before = _head(operator_settings)
    resolve = _resolve(service, group_arn, "accept", f"accept-busy-{state}")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = final.diagnostic_reports
    assert diagnostic.diagnostic_kind == "adoption_in_progress"
    assert "finish or reject that adoption first" in diagnostic.fix_suggestion
    assert _head(operator_settings) == head_before and _workspace_changes(operator_settings) == []
    assert _open_finding_count(session_factory) == 1


def test_an_adoption_for_another_target_does_not_block_accepting(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "other-target"
    )
    with session_factory() as session:
        session.add(
            Operation(
                operation_kind="terraform_adoption",
                state="AWAITING_APPROVAL",
                environment_slug="production",
                target_key="production/123456789012/eu-west-2",
                plan={},
                idempotency_key="production-adoption",
                created_by_operator_name="phil",
            )
        )
        session.commit()
    resolve = _resolve(service, group_arn, "accept", "accept-other-target")
    runner.run_once()
    assert service.get(resolve.id).state == OperationState.SUCCEEDED


def test_accept_without_a_record_fails_with_a_diagnostic(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.main import build_executors, build_services
    from rdscommander.operations.runner import OperationRunner
    from rdscommander.operations.service import OperationService
    from tests.terraform.test_adoption_executor import _prepare_repo

    _prepare_repo(operator_settings)
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    service = OperationService(session_factory)
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    resolve = _resolve(service, group_arn, "accept", "accept-no-record")
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    assert [d.diagnostic_kind for d in final.diagnostic_reports] == ["nothing_to_accept_into"]


def test_accept_without_an_inventory_snapshot_fails_with_a_diagnostic(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.main import build_executors, build_services
    from rdscommander.operations.runner import OperationRunner
    from rdscommander.operations.service import OperationService
    from tests.terraform.test_adoption_executor import _prepare_repo

    _prepare_repo(operator_settings)
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    service = OperationService(session_factory)
    resolve = service.create(
        "drift_resolve",
        "production",
        "production/123456789012/eu-west-2",
        {"options": {"resource_arn": "arn:aws:rds:eu-west-2:123456789012:db:x", "choice": "accept"}},
        "accept-nothing",
        "phil",
        resource_arn="arn:aws:rds:eu-west-2:123456789012:db:x",
        title="Resolve drift: x",
    )
    runner.run_once()
    final = service.get(resolve.id)
    assert final.state == OperationState.FAILED
    assert [d.diagnostic_kind for d in final.diagnostic_reports] == ["no_inventory_snapshot"]


# Resources without a page --------------------------------------------------


def _client_and_token(session_factory, operator_settings, console_config):
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    return client, client.get("/api/v1/bootstrap").json()["csrf_token"]


def _create_environment_level_resolution(
    client, token, resource_key: str, key: str, environment: str = "sandbox"
):
    return client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "drift_resolve",
            "environment_slug": environment,
            "options": {"resource_arn": resource_key, "choice": "accept"},
            "idempotency_key": key,
        },
    )


def test_a_resource_that_left_the_inventory_is_accepted_out_of_the_record(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    boto3.client("rds", region_name="eu-west-2").delete_db_instance(
        DBInstanceIdentifier="demo-orders-replica", SkipFinalSnapshot=True
    )
    _refresh_and_detect(runner, service, "vanished")
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
    assert finding.resource_arn == REPLICA_ADDRESS, "a vanished resource is keyed by its Terraform address"
    assert finding.attributes == [
        {"address": REPLICA_ADDRESS, "attribute": "presence", "recorded": True, "live": False}
    ]

    client, token = _client_and_token(session_factory, operator_settings, console_config)
    created = _create_environment_level_resolution(client, token, REPLICA_ADDRESS, "vanished-1")
    assert created.status_code == 202, created.text
    operation = service.get(uuid.UUID(created.json()["operation_id"]))
    assert (operation.target_key, operation.resource_arn) == (TARGET_KEY, REPLICA_ADDRESS)
    assert operation.title == f"Resolve drift: {REPLICA_ADDRESS}"
    assert operation.plan["address"] == REPLICA_ADDRESS and "operation_fields" not in operation.plan
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]

    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert [i.identifier for i in recorded.instances] == ["demo-orders"]
    assert "demo-orders-replica" not in (_workspace_dir(operator_settings) / "instances.tf").read_text()
    assert _open_finding_count(session_factory) == 0
    assert _workspace_changes(operator_settings) == []
    assert _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root).startswith(
        f"Resolve drift: {REPLICA_ADDRESS}"
    )


def test_a_drifted_subnet_group_is_accepted_without_a_resource_page(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    # The simulator cannot modify a subnet group, so the record is edited and committed instead: from the
    # record's point of view AWS now says something else.
    workspace_dir = _workspace_dir(operator_settings)
    recorded = read_record(workspace_dir)
    live_description = recorded.render_input.subnet_groups[0].description
    edited = recorded.render_input.model_copy(deep=True)
    edited.subnet_groups[0].description = "As the record once had it"
    write_workspace_files(
        workspace_dir,
        render_workspace_files(edited, format_binary=operator_settings.terraform_binary),
        record=edited,
        snapshot_id=recorded.snapshot_id,
    )
    _git("add", "-A", cwd=operator_settings.repo_root)
    _git("commit", "-q", "-m", "Edit the record by hand", cwd=operator_settings.repo_root)
    _refresh_and_detect(runner, service, "subnets")
    subnet_group_arn = arn_for(session_factory, sandbox_snapshot, "demo-private-subnets")
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
    assert finding.resource_arn == subnet_group_arn
    assert [(a["attribute"], a["recorded"], a["live"]) for a in finding.attributes] == [
        ("description", "As the record once had it", live_description)
    ]

    client, token = _client_and_token(session_factory, operator_settings, console_config)
    created = _create_environment_level_resolution(client, token, subnet_group_arn, "subnets-1")
    assert created.status_code == 202, created.text
    operation = service.get(uuid.UUID(created.json()["operation_id"]))
    assert operation.title == "Resolve drift: demo-private-subnets"
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    accepted = read_record(workspace_dir).render_input
    assert [s.description for s in accepted.subnet_groups] == [live_description]
    assert live_description in (workspace_dir / "subnet_groups.tf").read_text()
    assert _open_finding_count(session_factory) == 0


def test_environment_level_resolution_is_refused_without_open_drift_or_for_another_environment(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, group_arn = _drifted_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "refused"
    )
    client, token = _client_and_token(session_factory, operator_settings, console_config)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    no_drift = _create_environment_level_resolution(client, token, orders_arn, "refused-1")
    assert no_drift.status_code == 422 and "has no open drift" in no_drift.json()["detail"]
    other_environment = _create_environment_level_resolution(
        client, token, group_arn, "refused-2", environment="production"
    )
    assert other_environment.status_code == 422 and "not production" in other_environment.json()["detail"]
    accepted = _create_environment_level_resolution(client, token, group_arn, "refused-3")
    assert accepted.status_code == 202


def test_a_direct_mode_restore_that_restarts_the_instance_asks_for_its_identifier(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    _change_orders_instance_class("db.r6g.xlarge")
    _refresh_and_detect(runner, service, "restore-class-identifier")
    resolve = _resolve(
        service,
        arn_for(session_factory, sandbox_snapshot, "demo-orders"),
        "restore",
        "restore-class-identifier-1",
    )
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.approval_required_text == "demo-orders"


def test_a_direct_mode_restore_without_downtime_needs_no_identifier(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    rds = boto3.client("rds", region_name="eu-west-2")
    original = rds.describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"][0][
        "BackupRetentionPeriod"
    ]
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=original + 3, ApplyImmediately=True
    )
    _refresh_and_detect(runner, service, "restore-retention-identifier")
    resolve = _resolve(
        service,
        arn_for(session_factory, sandbox_snapshot, "demo-orders"),
        "restore",
        "restore-retention-identifier-1",
    )
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.approval_required_text is None


def test_a_terraform_mode_restore_states_its_downtime_and_asks_for_the_identifier_when_it_restarts(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _change_orders_instance_class("db.r6g.xlarge")
    _refresh_and_detect(runner, service, "restore-tf-class")
    fake = runner.services.terraform_runner
    fake.plan_scripts[:] = [
        _plan_changing(
            ORDERS_ADDRESS, {"instance_class": "db.r6g.xlarge"}, {"instance_class": "db.m6g.large"}
        )
    ]
    resolve = _resolve(
        service, arn_for(session_factory, sandbox_snapshot, "demo-orders"), "restore", "restore-tf-class-1"
    )
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["downtime"] == "a restart of the instance (Multi-AZ fails over to the standby)"
    assert paused.plan["requires_reboot"] is False
    assert paused.approval_required_text == "demo-orders"


def test_a_terraform_mode_restore_of_a_static_parameter_flags_the_reboot_without_asking_for_the_identifier(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """The restored value waits for the next reboot, so nothing is interrupted when the approver approves."""
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[
            {
                "ParameterName": "shared_buffers",
                "ParameterValue": "{DBInstanceClassMemory/16384}",
                "ApplyMethod": "pending-reboot",
                "Source": "user",
            }
        ],
    )
    runner.services.terraform_runner.plan_scripts[:] = [{"resource_changes": []}]
    _refresh_and_detect(runner, service, "restore-tf-static")
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    runner.services.terraform_runner.plan_scripts[:] = [
        _plan_changing(
            GROUP_ADDRESS,
            {"parameter": [{"name": "shared_buffers", "value": "{DBInstanceClassMemory/16384}"}]},
            {"parameter": [{"name": "shared_buffers", "value": "{DBInstanceClassMemory/32768}"}]},
        )
    ]
    resolve = _resolve(service, group_arn, "restore", "restore-tf-static-1")
    runner.run_once()
    paused = service.get(resolve.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["requires_reboot"] is True
    assert paused.plan["downtime"] == "a reboot of every instance using the group applies the restored values"
    assert paused.approval_required_text is None
