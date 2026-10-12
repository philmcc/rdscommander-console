import json
import shutil
from dataclasses import replace

import boto3

from rdscommander.control_db.models import DriftFinding, InventoryDbInstance
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.main import build_executors, build_services
from rdscommander.operations.enums import OperationState
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.terraform.adoption_plan import build_adoption_plan
from rdscommander.terraform.record import read_record
from rdscommander.terraform.renderer import render_workspace_files, write_workspace_files
from rdscommander.terraform.runner import PlanRunResult
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _git, _import_plan, _prepare_repo


def _adopt(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    snapshot,
    names: list[str],
    addresses: list[str],
    key: str,
    prepare_repo: bool = True,
):
    """Adopt `names` (with their Terraform `addresses`) through the real executors and a fake Terraform."""
    if prepare_repo:
        _prepare_repo(operator_settings)
    services = build_services(operator_settings, session_factory)
    fake = FakeTerraformRunner(
        services.execution_recorder,
        plan_scripts=[
            _import_plan(addresses),
            _import_plan(addresses),
            {"resource_changes": []},
            {"resource_changes": []},
        ],
    )
    services.terraform_runner = fake
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    service = OperationService(session_factory)
    with session_factory() as session:
        plan = build_adoption_plan(
            session,
            console_config,
            TARGET_KEY,
            [arn_for(session_factory, snapshot, name) for name in names],
        )
    operation = service.create("terraform_adoption", "sandbox", TARGET_KEY, plan.model_dump(), key, "phil")
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    return runner, service


GROUP_ADDRESS = "aws_db_parameter_group.demo_postgres16_tuned"
SUBNET_GROUP_ADDRESS = "aws_db_subnet_group.demo_private_subnets"
ORDERS_ADDRESS = "aws_db_instance.demo_orders"
REPLICA_ADDRESS = "aws_db_instance.demo_orders_replica"


def _adopt_estate(session_factory, operator_settings, config_store, console_config, snapshot):
    """Adopt the tuned group, the subnet group, `demo-orders` and its replica; returns runner and service."""
    return _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        snapshot,
        ["demo-postgres16-tuned", "demo-private-subnets", "demo-orders", "demo-orders-replica"],
        [GROUP_ADDRESS, SUBNET_GROUP_ADDRESS, ORDERS_ADDRESS, REPLICA_ADDRESS],
        "drift-adopt-estate",
    )


def _adopt_group(session_factory, operator_settings, config_store, console_config, snapshot):
    return _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        snapshot,
        ["demo-postgres16-tuned"],
        ["aws_db_parameter_group.demo_postgres16_tuned"],
        "drift-adopt",
    )


def test_detect_reports_record_drift_after_an_outside_change(
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
    service.create("inventory_refresh", "sandbox", None, {}, "drift-refresh", "phil")
    runner.run_once()
    detect = service.create("drift_detect", "sandbox", None, {"options": {}}, "drift-1", "phil")
    runner.run_once()
    assert service.get(detect.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
        assert (
            finding.finding_kind == "record" and finding.attributes[0]["attribute"] == "parameters.work_mem"
        )
        assert finding.attributes[0]["recorded"] == "16384" and finding.attributes[0]["live"] == "65536"


def test_detect_resolves_findings_that_disappeared(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
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
    service.create("inventory_refresh", "sandbox", None, {}, "drift-refresh-2", "phil")
    runner.run_once()
    service.create("drift_detect", "sandbox", None, {"options": {}}, "drift-2", "phil")
    runner.run_once()
    rds.modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[
            {
                "ParameterName": "work_mem",
                "ParameterValue": "16384",
                "ApplyMethod": "immediate",
                "Source": "user",
            }
        ],
    )
    service.create("inventory_refresh", "sandbox", None, {}, "drift-refresh-3", "phil")
    runner.run_once()
    service.create("drift_detect", "sandbox", None, {"options": {}}, "drift-3", "phil")
    runner.run_once()
    with session_factory() as session:
        assert session.query(DriftFinding).filter_by(resolved_at_utc=None).count() == 0
        assert session.query(DriftFinding).count() == 1


def _set_work_mem(value: str) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[
            {
                "ParameterName": "work_mem",
                "ParameterValue": value,
                "ApplyMethod": "immediate",
                "Source": "user",
            }
        ],
    )


def _refresh_and_detect(runner, service, key: str):
    service.create("inventory_refresh", "sandbox", None, {}, f"{key}-refresh", "phil")
    runner.run_once()
    detect = service.create("drift_detect", "sandbox", None, {"options": {}}, f"{key}-detect", "phil")
    runner.run_once()
    return service.get(detect.id)


def _detect_step_output(detect) -> dict:
    [attempt] = [a for a in detect.step_attempts if a.step_name == "detect:sandbox/000000000000/eu-west-2"]
    return attempt.output


def _plan_changing(address: str, before: dict, after: dict) -> dict:
    """A plan that proposes to update `address` from `before` to `after`."""
    return {
        "resource_changes": [
            {
                "address": address,
                "type": address.split(".")[0],
                "change": {
                    "actions": ["update"],
                    "before": before,
                    "after": after,
                    "before_sensitive": {},
                    "after_sensitive": {},
                },
            }
        ]
    }


def _state_plan(before_description: str, after_description: str) -> dict:
    return _plan_changing(
        GROUP_ADDRESS, {"description": before_description}, {"description": after_description}
    )


def test_detect_reports_state_drift_from_the_plan_and_closes_it_when_the_plan_is_clean(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = runner.services.terraform_runner
    fake.plan_scripts[:] = [_state_plan("changed in AWS", "as recorded")]
    detect = _refresh_and_detect(runner, service, "state-1")
    assert detect.state == OperationState.SUCCEEDED
    assert _detect_step_output(detect)["state_findings"] == 1
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
        assert (finding.finding_kind, finding.resource_arn) == ("state", group_arn)
        assert finding.attributes == [
            {
                "address": GROUP_ADDRESS,
                "attribute": "description",
                "recorded": "as recorded",
                "live": "changed in AWS",
            }
        ]
    assert _refresh_and_detect(runner, service, "state-2").state == OperationState.SUCCEEDED
    with session_factory() as session:
        assert session.query(DriftFinding).filter_by(resolved_at_utc=None).count() == 0
        assert session.query(DriftFinding).count() == 1


def test_detect_reports_a_missing_resource_in_the_plan_as_presence_drift(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    missing = _state_plan("x", "y")
    missing["resource_changes"][0]["change"].update(actions=["create"], before=None)
    runner.services.terraform_runner.plan_scripts[:] = [missing]
    _refresh_and_detect(runner, service, "state-missing")
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
        assert finding.attributes == [
            {"address": GROUP_ADDRESS, "attribute": "presence", "recorded": True, "live": False}
        ]


def test_detect_keeps_record_findings_and_leaves_state_findings_alone_when_the_plan_fails(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = runner.services.terraform_runner
    fake.plan_scripts[:] = [_state_plan("changed in AWS", "as recorded")]
    _refresh_and_detect(runner, service, "fail-1")
    failing_result = replace(fake._ok(), exit_code=1, standard_error="Error: expired credentials")
    fake.plan = lambda *args, **kwargs: PlanRunResult(result=failing_result, has_changes=False, plan_json={})
    _set_work_mem("65536")
    detect = _refresh_and_detect(runner, service, "fail-2")
    assert detect.state == OperationState.FAILED
    [diagnostic] = detect.diagnostic_reports
    assert diagnostic.diagnostic_kind == "terraform_plan_failed" and diagnostic.fix_suggestion
    assert "expired credentials" in diagnostic.detail
    with session_factory() as session:
        open_findings = {
            f.finding_kind: f for f in session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
        }
    assert set(open_findings) == {"state", "record"}, "a failed plan must not close the state finding"
    assert open_findings["record"].attributes[0]["attribute"] == "parameters.work_mem"


def test_detect_skips_state_drift_when_the_workspace_was_never_initialised_on_this_machine(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    shutil.rmtree(workspace_dir / ".terraform")
    calls_before = list(runner.services.terraform_runner.calls)
    detect = _refresh_and_detect(runner, service, "uninitialised")
    assert detect.state == OperationState.SUCCEEDED
    assert "skipped" in _detect_step_output(detect)["state_drift"]
    assert runner.services.terraform_runner.calls == calls_before, "no plan may run without init"


def test_detect_has_nothing_to_do_before_anything_is_recorded(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    detect = OperationService(session_factory).create(
        "drift_detect", "sandbox", None, {"options": {}}, "empty", "phil"
    )
    runner.run_once()
    finished = OperationService(session_factory).get(detect.id)
    assert finished.state == OperationState.SUCCEEDED
    assert _detect_step_output(finished) == {"skipped": "nothing has been recorded yet"}
    with session_factory() as session:
        assert session.query(DriftFinding).count() == 0


def test_detect_fails_with_a_diagnostic_when_the_record_file_is_unreadable(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    (workspace_dir / "console-record.json").write_text("<<<<<<< HEAD\n")
    detect = _refresh_and_detect(runner, service, "unreadable")
    assert detect.state == OperationState.FAILED
    [diagnostic] = detect.diagnostic_reports
    assert diagnostic.diagnostic_kind == "record_unreadable" and "git" in diagnostic.fix_suggestion


def test_detect_changes_nothing_in_aws_or_the_repository(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _set_work_mem("65536")
    head_before = _git("rev-parse", "HEAD", cwd=operator_settings.repo_root)
    calls_before = list(runner.services.terraform_runner.calls)
    _refresh_and_detect(runner, service, "readonly")
    assert _git("rev-parse", "HEAD", cwd=operator_settings.repo_root) == head_before
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=operator_settings.repo_root) == ""
    new_calls = runner.services.terraform_runner.calls[len(calls_before) :]
    assert new_calls == ["plan:detect:sandbox/000000000000/eu-west-2"], "detection may plan, never apply"
    live = boto3.client("rds", region_name="eu-west-2").describe_db_parameters(
        DBParameterGroupName="demo-postgres16-tuned", Source="user"
    )["Parameters"]
    assert {p["ParameterName"]: p["ParameterValue"] for p in live}["work_mem"] == "65536"


def test_detect_in_direct_mode_compares_the_record_with_every_resource_and_runs_no_plan(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, fake = direct_mode_runner
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    recorded = read_record(workspace_dir).render_input
    assert {i.identifier for i in recorded.instances} == {"demo-orders", "demo-orders-replica"}
    assert [c.identifier for c in recorded.clusters] == ["demo-aurora"]
    recorded_instance_class = next(
        i for i in recorded.instances if i.identifier == "demo-orders"
    ).instance_class
    _set_work_mem("65536")
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", DBInstanceClass="db.r6g.xlarge", ApplyImmediately=True
    )
    detect = _refresh_and_detect(runner, service, "direct-detect")
    assert detect.state == OperationState.SUCCEEDED, [d.detail for d in detect.diagnostic_reports]
    assert _detect_step_output(detect)["state_findings"] == 0
    assert fake.calls == [], "direct mode has no state and runs no Terraform"
    with session_factory() as session:
        findings = {
            f.resource_arn.rsplit(":", 1)[-1]: f
            for f in session.query(DriftFinding).filter_by(resolved_at_utc=None)
        }
    assert set(findings) == {"demo-orders", "demo-postgres16-tuned"}
    assert {(a["attribute"], a["recorded"], a["live"]) for a in findings["demo-orders"].attributes} == {
        ("instance_class", recorded_instance_class, "db.r6g.xlarge")
    }


def test_detect_in_direct_mode_records_the_estate_when_nothing_is_recorded_yet(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, fake = direct_mode_runner
    repo_root = operator_settings.repo_root
    workspace_relative_dir = "terraform/sandbox/000000000000/eu-west-2"
    workspace_dir = repo_root / workspace_relative_dir
    _git("rm", "-r", "-q", workspace_relative_dir, cwd=repo_root)
    _git("commit", "-q", "-m", "Forget the record", cwd=repo_root)
    assert read_record(workspace_dir) is None

    first = _refresh_and_detect(runner, service, "bootstrap-1")

    assert first.state == OperationState.SUCCEEDED, [d.detail for d in first.diagnostic_reports]
    recorded = read_record(workspace_dir).render_input
    assert {i.identifier for i in recorded.instances} == {"demo-orders", "demo-orders-replica"}
    assert [c.identifier for c in recorded.clusters] == ["demo-aurora"]
    assert "demo-orders" in (workspace_dir / "instances.tf").read_text()
    assert _git("log", "-1", "--format=%s", cwd=repo_root) == "Record estate: sandbox (phil)"
    assert _git("status", "--porcelain", "--", workspace_relative_dir, cwd=repo_root) == ""
    commit_count_after_recording = _git("rev-list", "--count", "HEAD", cwd=repo_root)
    assert _detect_step_output(first)["record_findings"] == 0
    assert fake.calls == [], "direct mode has no state and runs no Terraform"
    with session_factory() as session:
        assert session.query(DriftFinding).count() == 0

    _set_work_mem("65536")
    second = _refresh_and_detect(runner, service, "bootstrap-2")

    assert second.state == OperationState.SUCCEEDED, [d.detail for d in second.diagnostic_reports]
    assert _git("rev-list", "--count", "HEAD", cwd=repo_root) == commit_count_after_recording, (
        "a record that exists is compared, not written again"
    )
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
    assert finding.finding_kind == "record"
    assert finding.resource_arn.endswith(":pg:demo-postgres16-tuned")
    assert finding.attributes[0]["attribute"] == "parameters.work_mem"


def test_detect_in_direct_mode_reverts_the_first_record_when_it_cannot_be_committed_and_retries(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    repo_root = operator_settings.repo_root
    workspace_relative_dir = "terraform/sandbox/000000000000/eu-west-2"
    workspace_dir = repo_root / workspace_relative_dir
    _git("rm", "-r", "-q", workspace_relative_dir, cwd=repo_root)
    _git("commit", "-q", "-m", "Forget the record", cwd=repo_root)
    refusing_hook = repo_root / ".git" / "hooks" / "pre-commit"
    refusing_hook.write_text("#!/bin/sh\necho refused by the test hook >&2\nexit 1\n")
    refusing_hook.chmod(0o755)

    refused = _refresh_and_detect(runner, service, "bootstrap-refused")

    assert refused.state == OperationState.FAILED
    [diagnostic] = [d for d in refused.diagnostic_reports if d.diagnostic_kind == "commit_failed"]
    assert "reverted" in diagnostic.fix_suggestion
    assert read_record(workspace_dir) is None, "an uncommitted baseline must not be left behind"
    assert not (workspace_dir / "instances.tf").exists()
    assert _git("status", "--porcelain", cwd=repo_root) == ""
    assert _git("log", "-1", "--format=%s", cwd=repo_root) == "Forget the record"

    refusing_hook.unlink()
    retried = _refresh_and_detect(runner, service, "bootstrap-retried")

    assert retried.state == OperationState.SUCCEEDED, [d.detail for d in retried.diagnostic_reports]
    assert read_record(workspace_dir) is not None
    assert _git("log", "-1", "--format=%s", cwd=repo_root) == "Record estate: sandbox (phil)"
    assert _git("status", "--porcelain", cwd=repo_root) == ""
    assert _detect_step_output(retried)["record_bootstrapped"] is True


def test_detect_in_direct_mode_writes_nothing_when_the_workspace_holds_an_uncommitted_hand_edit(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    repo_root = operator_settings.repo_root
    workspace_relative_dir = "terraform/sandbox/000000000000/eu-west-2"
    workspace_dir = repo_root / workspace_relative_dir
    _git("rm", "-q", f"{workspace_relative_dir}/console-record.json", cwd=repo_root)
    _git("commit", "-q", "-m", "Forget the record", cwd=repo_root)
    hand_edited_text = (workspace_dir / "instances.tf").read_text() + "# hand edit, not committed\n"
    (workspace_dir / "instances.tf").write_text(hand_edited_text)
    status_before = _git("status", "--porcelain", cwd=repo_root)

    detect = _refresh_and_detect(runner, service, "bootstrap-dirty")

    assert detect.state == OperationState.FAILED
    [diagnostic] = [d for d in detect.diagnostic_reports if d.diagnostic_kind == "checkout_guard"]
    assert "dirty_terraform_directory" in diagnostic.detail
    assert diagnostic.fix_suggestion
    assert (workspace_dir / "instances.tf").read_text() == hand_edited_text, "the hand edit survives"
    assert read_record(workspace_dir) is None
    assert _git("status", "--porcelain", cwd=repo_root) == status_before
    assert _git("log", "-1", "--format=%s", cwd=repo_root) == "Forget the record"
    with session_factory() as session:
        assert session.query(DriftFinding).count() == 0


def test_detect_does_not_report_resources_of_an_adoption_waiting_for_approval_as_missing(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    subnets = "aws_db_subnet_group.demo_private_subnets"
    runner.services.terraform_runner.plan_scripts[:] = [_import_plan([subnets]), _import_plan([subnets])]
    with session_factory() as session:
        plan = build_adoption_plan(
            session,
            config_store.current,
            TARGET_KEY,
            [arn_for(session_factory, sandbox_snapshot, "demo-private-subnets")],
        )
    adoption = service.create(
        "terraform_adoption", "sandbox", TARGET_KEY, plan.model_dump(), "waiting", "phil"
    )
    runner.run_once()
    assert service.get(adoption.id).state == OperationState.AWAITING_APPROVAL
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    assert "demo-private-subnets" in (workspace_dir / "subnet_groups.tf").read_text()
    detect = service.create("drift_detect", "sandbox", None, {"options": {}}, "while-waiting", "phil")
    runner.run_once()
    assert service.get(detect.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        assert session.query(DriftFinding).count() == 0, "files written for review are not drift"
    assert read_record(workspace_dir).render_input.subnet_groups == [], "and they are not in the record yet"


def test_detect_skips_state_drift_while_the_workspace_has_uncommitted_files_and_says_why(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    rendered_by_a_change_awaiting_approval = workspace_dir / "parameter_groups.tf"
    rendered_by_a_change_awaiting_approval.write_text(
        rendered_by_a_change_awaiting_approval.read_text() + "\n# rendered, not yet approved\n"
    )
    fake = runner.services.terraform_runner
    fake.plan_scripts[:] = [_state_plan("changed in AWS", "as recorded")]
    calls_before = list(fake.calls)
    detect = _refresh_and_detect(runner, service, "dirty-workspace")
    assert detect.state == OperationState.SUCCEEDED
    output = _detect_step_output(detect)
    assert output["state_drift"].startswith("skipped: the workspace has uncommitted files")
    assert "parameter_groups.tf" in output["state_drift"] and "state_findings" not in output
    assert fake.calls == calls_before, "no plan may run against files that are not committed"
    with session_factory() as session:
        assert session.query(DriftFinding).filter_by(finding_kind="state").count() == 0


def _edit_group(service, group_arn: str, key: str, work_mem: str):
    return service.create(
        "parameter_group_edit",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameters": {"work_mem": work_mem}, "reset": []}, "resource_arn": group_arn},
        key,
        "phil",
        resource_arn=group_arn,
        title="Edit parameters: demo-postgres16-tuned",
    )


def test_a_terraform_mode_change_closes_the_open_drift_findings_of_what_it_touched(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """State drift does not hold a change up (record drift does); the committed change closes it."""
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = runner.services.terraform_runner
    fake.plan_scripts[:] = [_state_plan("changed in AWS", "as recorded")]
    _refresh_and_detect(runner, service, "fold-tf")
    with session_factory() as session:
        assert {f.finding_kind for f in session.query(DriftFinding).filter_by(resolved_at_utc=None)} == {
            "state"
        }
    update = _plan_changing(
        GROUP_ADDRESS,
        {"parameter": [{"name": "work_mem", "value": "16384"}]},
        {"parameter": [{"name": "work_mem", "value": "40000"}]},
    )
    fake.plan_scripts[:] = [update, update, {"resource_changes": []}]
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    edit = _edit_group(service, group_arn, "fold-tf-edit", "40000")
    runner.run_once()
    service.approve(edit.id, "sam")
    runner.run_once()
    assert service.get(edit.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        findings = session.query(DriftFinding).all()
        assert findings and all(f.resolved_by_operation_id == edit.id for f in findings)


def test_a_direct_mode_change_on_a_resource_with_open_drift_is_refused_until_the_drift_is_resolved(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    """The change would record the outside value as part of an approved change; it is refused instead."""
    runner, service, _ = direct_mode_runner
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "fold-direct")
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
        assert finding.finding_kind == "record"
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    edit = _edit_group(service, group_arn, "fold-direct-edit", "40000")
    runner.run_once()
    refused = service.get(edit.id)
    assert refused.state == OperationState.FAILED
    [diagnostic] = [d for d in refused.diagnostic_reports if d.diagnostic_kind == "open_drift_on_resource"]
    assert diagnostic.step_name == "compute_change_set"
    assert "parameters.work_mem" in diagnostic.detail
    assert diagnostic.fix_suggestion.startswith("Resolve drift on demo-postgres16-tuned first")
    assert [a.step_name for a in refused.step_attempts][-1] == "compute_change_set"
    with session_factory() as session:
        [still_open] = session.query(DriftFinding).all()
        assert still_open.resolved_at_utc is None
    parameters = boto3.client("rds", region_name="eu-west-2").describe_db_parameters(
        DBParameterGroupName="demo-postgres16-tuned"
    )["Parameters"]
    assert next(p["ParameterValue"] for p in parameters if p["ParameterName"] == "work_mem") == "65536"


def test_a_terraform_mode_change_on_a_resource_with_open_record_drift_is_refused_and_writes_nothing(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = runner.services.terraform_runner
    fake.plan_scripts[:] = [{"resource_changes": []}]
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "fold-tf-refused")
    calls_before = list(fake.calls)
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    edit = _edit_group(service, group_arn, "fold-tf-refused-edit", "40000")
    runner.run_once()
    refused = service.get(edit.id)
    assert refused.state == OperationState.FAILED
    [diagnostic] = [d for d in refused.diagnostic_reports if d.diagnostic_kind == "open_drift_on_resource"]
    assert diagnostic.step_name == "render_desired"
    assert diagnostic.fix_suggestion.startswith("Resolve drift on demo-postgres16-tuned first")
    assert fake.calls == calls_before, "nothing is planned"
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def _recorded_work_mem(operator_settings) -> str:
    recorded = read_record(
        operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    )
    group = next(
        g for g in recorded.render_input.instance_parameter_groups if g.name == "demo-postgres16-tuned"
    )
    return next(p.value for p in group.parameters if p.name == "work_mem")


def test_adopting_another_resource_leaves_the_recorded_values_of_a_drifted_adopted_resource_alone(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """Adopting B renders the resources adopted earlier from the record, so A's drift stays open."""
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.services.terraform_runner.plan_scripts[:] = [{"resource_changes": []}]
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "adopt-over-drift")
    with session_factory() as session:
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
        assert finding.finding_kind == "record"
    _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        ["demo-private-subnets"],
        [SUBNET_GROUP_ADDRESS],
        "adopt-b",
        prepare_repo=False,
    )
    assert _recorded_work_mem(operator_settings) == "16384", "A keeps its recorded value"
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    assert '"16384"' in (workspace_dir / "parameter_groups.tf").read_text()
    recorded = read_record(workspace_dir).render_input
    assert [s.name for s in recorded.subnet_groups] == ["demo-private-subnets"], "B comes from the snapshot"
    detect = service.create("drift_detect", "sandbox", None, {"options": {}}, "adopt-over-drift-2", "phil")
    runner.run_once()
    with session_factory() as session:
        [still_open] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
        assert still_open.id == finding.id and service.get(detect.id).state == OperationState.SUCCEEDED


def test_an_adopted_resource_missing_from_the_record_is_recorded_from_the_snapshot_by_the_next_adoption(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """A workspace adopted before the record existed has nothing to keep; the snapshot fills it in."""
    _adopt_group(session_factory, operator_settings, config_store, console_config, sandbox_snapshot)
    _git(
        "rm",
        "-q",
        "terraform/sandbox/000000000000/eu-west-2/console-record.json",
        cwd=operator_settings.repo_root,
    )
    _git("commit", "-q", "-m", "a workspace from before the record", cwd=operator_settings.repo_root)
    _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        ["demo-private-subnets"],
        [SUBNET_GROUP_ADDRESS],
        "adopt-b-pre-record",
        prepare_repo=False,
    )
    assert _recorded_work_mem(operator_settings) == "16384"


def _runner_in_mode(runner, session_factory, operator_settings, console_config, change_management: str):
    """A runner over the same services and checkout, for the sandbox switched to `change_management`."""
    from rdscommander.config.store import StaticConfigStore

    switched = console_config.model_copy(deep=True)
    switched.environment("sandbox").change_management = change_management
    store = StaticConfigStore(switched, session_factory)
    store.reload()
    return OperationRunner(
        session_factory,
        build_executors(runner.services, store),
        "r",
        operator_settings,
        store,
        runner.services,
    )


def _commit_count(operator_settings) -> int:
    return int(_git("rev-list", "--count", "HEAD", cwd=operator_settings.repo_root))


def _workspace_dir(operator_settings):
    return operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"


def test_switching_a_terraform_mode_environment_to_direct_records_the_rest_of_the_estate_in_one_commit(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """The record held the adopted group only; the rest joins it at once, not as one finding per resource."""
    from rdscommander.terraform.record import resource_addresses
    from rdscommander.terraform.render_input import build_render_input

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    direct_runner = _runner_in_mode(runner, session_factory, operator_settings, console_config, "direct")
    _set_work_mem("65536")
    commits_before = _commit_count(operator_settings)
    detect = _refresh_and_detect(direct_runner, service, "t-to-d")
    assert detect.state == OperationState.SUCCEEDED, [d.detail for d in detect.diagnostic_reports]
    assert _commit_count(operator_settings) == commits_before + 1
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root) == "Record estate: sandbox (phil)"
    )
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        estate = build_render_input(
            session,
            direct_runner.config_store.current.target(TARGET_KEY),
            direct_runner.config_store.current,
            snapshot,
            set(),
            set(),
            include_imports=False,
            simulated_endpoint_url=None,
            include_all=True,
        )
        [finding] = session.query(DriftFinding).filter_by(resolved_at_utc=None).all()
    assert resource_addresses(recorded) == resource_addresses(estate)
    assert _recorded_work_mem(operator_settings) == "16384", "what was recorded keeps its recorded values"
    assert finding.finding_kind == "record"
    assert [a["attribute"] for a in finding.attributes] == ["parameters.work_mem"]
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_switching_a_direct_mode_environment_to_terraform_records_the_adopted_only_and_its_groups_adoptable(
    session_factory, operator_settings, console_config, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    """The direct record held the whole estate; Terraform mode records only what is under Terraform."""
    from rdscommander.catalogue.catalogue import actions_for_resource
    from rdscommander.resources.queries import load_resource
    from rdscommander.terraform.record import resource_addresses

    runner, service, fake = direct_mode_runner
    create = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        {
            "options": {
                "target_key": TARGET_KEY,
                "name": "orders-pg16-v2",
                "scope": "instance",
                "family": "postgres16",
                "description": "created in direct mode",
                "source": "defaults",
                "clone_from_arn": None,
                "parameters": {},
            }
        },
        "d-to-t-create",
        "phil",
        title="Create parameter group: orders-pg16-v2",
    )
    runner.run_once()
    service.approve(create.id, "sam")
    runner.run_once()
    assert service.get(create.id).state == OperationState.SUCCEEDED
    terraform_runner = _runner_in_mode(
        runner, session_factory, operator_settings, console_config, "terraform"
    )
    commits_before = _commit_count(operator_settings)
    detect = _refresh_and_detect(terraform_runner, service, "d-to-t")
    assert detect.state == OperationState.SUCCEEDED, [d.detail for d in detect.diagnostic_reports]
    assert _commit_count(operator_settings) == commits_before + 1
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == "Record adopted resources: sandbox (phil)"
    )
    assert resource_addresses(read_record(_workspace_dir(operator_settings)).render_input) == set()
    assert not (_workspace_dir(operator_settings) / "parameter_groups.tf").exists()
    with session_factory() as session:
        assert session.query(DriftFinding).filter_by(resolved_at_utc=None).count() == 0
    terraform_config = terraform_runner.config_store.current
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        created_arn = arn_for(session_factory, snapshot, "orders-pg16-v2")
        created_group = load_resource(session, terraform_config, created_arn)
    assert created_group.ownership == "unmanaged" and created_group.terraform_address is None
    actions = {
        a.kind: a for a in actions_for_resource(created_group, terraform_config.environment("sandbox"))
    }
    assert actions["terraform_adoption"].applicable, actions["terraform_adoption"].reason
    assert not actions["parameter_group_edit"].applicable
    address = "aws_db_parameter_group.orders_pg16_v2"
    fake.plan_scripts[:] = [_import_plan([address]), _import_plan([address]), {"resource_changes": []}]
    with session_factory() as session:
        plan = build_adoption_plan(session, terraform_config, TARGET_KEY, [created_arn])
    adoption = service.create(
        "terraform_adoption", "sandbox", TARGET_KEY, plan.model_dump(), "d-to-t-adopt", "phil"
    )
    terraform_runner.run_once()
    service.approve(adoption.id, "sam")
    terraform_runner.run_once()
    adopted = service.get(adoption.id)
    assert adopted.state == OperationState.SUCCEEDED, [d.detail for d in adopted.diagnostic_reports]
    assert resource_addresses(read_record(_workspace_dir(operator_settings)).render_input) == {address}


def test_a_resource_in_terraform_state_counts_as_adopted_on_a_console_that_never_adopted_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """Another console with its own control database adopted the group: state says so, detection learns it."""
    from rdscommander.control_db.models import ResourceAdoption, TerraformWorkspace
    from rdscommander.resources.queries import load_resource

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    with session_factory() as session:
        workspace = session.query(TerraformWorkspace).one()
        bucket, key = workspace.state_bucket_name, workspace.state_key
        session.query(ResourceAdoption).delete()
        session.commit()
    state = {
        "version": 4,
        "resources": [
            {
                "mode": "managed",
                "type": "aws_db_parameter_group",
                "name": "demo_postgres16_tuned",
                "instances": [],
            }
        ],
    }
    boto3.client("s3", region_name="eu-west-2").put_object(Bucket=bucket, Key=key, Body=json.dumps(state))
    commits_before = _commit_count(operator_settings)
    runner.services.terraform_runner.plan_scripts[:] = [{"resource_changes": []}]
    detect = _refresh_and_detect(runner, service, "state-presence")
    assert detect.state == OperationState.SUCCEEDED, [d.detail for d in detect.diagnostic_reports]
    assert _commit_count(operator_settings) == commits_before, "the record already holds only what is adopted"
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        group = load_resource(session, config_store.current, group_arn)
        assert group.terraform_address == GROUP_ADDRESS and group.ownership == "adopted"
        assert session.query(DriftFinding).filter_by(resolved_at_utc=None).count() == 0


def test_an_adoption_keeps_what_state_holds_although_this_console_never_adopted_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """Rendering only this console's adoptions would plan to destroy what another console adopted."""
    from rdscommander.control_db.models import ResourceAdoption, TerraformWorkspace
    from rdscommander.terraform.record import resource_addresses

    _adopt_group(session_factory, operator_settings, config_store, console_config, sandbox_snapshot)
    with session_factory() as session:
        workspace = session.query(TerraformWorkspace).one()
        bucket, key = workspace.state_bucket_name, workspace.state_key
        session.query(ResourceAdoption).delete()
        session.commit()
    state = {
        "version": 4,
        "resources": [{"mode": "managed", "type": "aws_db_parameter_group", "name": "demo_postgres16_tuned"}],
    }
    boto3.client("s3", region_name="eu-west-2").put_object(Bucket=bucket, Key=key, Body=json.dumps(state))
    _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        ["demo-private-subnets"],
        [SUBNET_GROUP_ADDRESS],
        "adopt-beside-state",
        prepare_repo=False,
    )
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert resource_addresses(recorded) == {GROUP_ADDRESS, SUBNET_GROUP_ADDRESS}
    assert "demo-postgres16-tuned" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()


def test_a_value_queued_for_the_maintenance_window_is_not_record_drift(
    session_factory, operator_settings, direct_mode_runner, sandbox_snapshot, moto_aws
) -> None:
    runner, service, _ = direct_mode_runner
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    # the record holds what a queued change asked for (14 days); AWS still has the old value live
    recorded = read_record(workspace_dir).render_input
    next(i for i in recorded.instances if i.identifier == "demo-orders").backup_retention_period = 14
    write_workspace_files(
        workspace_dir,
        render_workspace_files(recorded, format_binary=operator_settings.terraform_binary),
        record=recorded,
        snapshot_id=sandbox_snapshot.id,
    )
    _git("add", "-A", cwd=operator_settings.repo_root)
    _git("commit", "-q", "-m", "Record the queued change", cwd=operator_settings.repo_root)

    def open_attributes() -> set[str]:
        with session_factory() as session:
            return {
                a["attribute"]
                for finding in session.query(DriftFinding).filter_by(resolved_at_utc=None)
                if finding.resource_arn.endswith(":demo-orders")
                for a in finding.attributes
            }

    def detect(key: str) -> None:
        operation = service.create("drift_detect", "sandbox", None, {"options": {}}, key, "phil")
        runner.run_once()
        assert service.get(operation.id).state == OperationState.SUCCEEDED

    detect("queued-1")
    assert "backup_retention_period" in open_attributes(), "without a queued change the difference is drift"

    with session_factory() as session:
        row = (
            session.query(InventoryDbInstance)
            .filter_by(snapshot_id=sandbox_snapshot.id, db_instance_identifier="demo-orders")
            .one()
        )
        row.raw_description = {**row.raw_description, "PendingModifiedValues": {"BackupRetentionPeriod": 14}}
        session.commit()
    detect("queued-2")
    assert "backup_retention_period" not in open_attributes(), "AWS holds the recorded value for the window"
