"""Two changes under one approval: the chained-change executor in both modes."""

from __future__ import annotations

import boto3
from fastapi.testclient import TestClient

from rdscommander.main import create_application
from rdscommander.operations.chained_change import ChainedChangeExecutor
from rdscommander.operations.enums import OperationState
from rdscommander.operations.executor import StepContext
from rdscommander.operations.service import OperationService
from tests.drift.test_detect_executor import _adopt_group
from tests.operations.test_change_strategy import (
    GROUP,
    GROUP_ADDRESS,
    SetWorkMem,
    _direct_store,
    _group_update_plan,
    _runner,
    _workspace_changes,
    _workspace_dir,
)
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _git, _prepare_repo

KIND = "test_set_work_mem_twice"
TITLE = f"Set work_mem twice: {GROUP}"
DIRECT_STEP_SEQUENCE = [
    "check_checkout",
    "compute_change_set",
    "policy_check",
    "await_approval",
    "apply_direct",
    "verify_direct",
    "refresh_resource",
    "record_to_checkout",
    "then_compute_change_set",
    "then_policy_check",
    "then_apply_direct",
    "then_verify_direct",
    "then_refresh_resource",
    "then_record_to_checkout",
]
TERRAFORM_STEP_SEQUENCE = [
    "check_checkout",
    "ensure_state_bucket",
    "render_desired",
    "init",
    "plan",
    "policy_check",
    "await_approval",
    "apply",
    "verify_clean_plan",
    "commit_to_checkout",
    "refresh_resource",
    "then_render_desired",
    "then_init",
    "then_plan",
    "then_policy_check",
    "then_apply",
    "then_verify_clean_plan",
    "then_commit_to_checkout",
    "then_refresh_resource",
]


class SetWorkMemTwiceExecutor(ChainedChangeExecutor):
    """Test-only chained executor: work_mem to `first`, then to `second`, under one approval."""

    operation_kind = KIND

    def first_change(self, context: StepContext) -> SetWorkMem:
        return SetWorkMem(context.operation.resource_arn, context.operation.plan["options"]["first"])

    def second_change(self, context: StepContext) -> SetWorkMem:
        return SetWorkMem(context.operation.resource_arn, context.operation.plan["options"]["second"])


def _chained_runner(session_factory, operator_settings, config_store, terraform_runner=None):  # noqa: ANN202
    runner, services = _runner(session_factory, operator_settings, config_store, terraform_runner)
    runner.executors[KIND] = SetWorkMemTwiceExecutor(config_store)
    return runner, services


def _create(service: OperationService, key: str, group_arn: str):  # noqa: ANN202
    return service.create(
        KIND,
        "sandbox",
        TARGET_KEY,
        {"options": {"first": "30000", "second": "40000"}, "resource_arn": group_arn},
        key,
        "phil",
        resource_arn=group_arn,
        title=TITLE,
    )


def _step_sequence(operation) -> list[str]:  # noqa: ANN001
    """Step names in the order they were first attempted (a resumed step is attempted again)."""
    return list(dict.fromkeys(attempt.step_name for attempt in operation.step_attempts))


def _live_work_mem() -> str:
    parameters = boto3.client("rds", region_name="eu-west-2").describe_db_parameters(
        DBParameterGroupName=GROUP
    )["Parameters"]
    return next(p["ParameterValue"] for p in parameters if p["ParameterName"] == "work_mem")


def _last_two_commit_subjects(operator_settings) -> list[str]:  # noqa: ANN001
    return _git("log", "-2", "--format=%s", cwd=operator_settings.repo_root).splitlines()


def test_chained_direct_change_runs_both_phases_under_one_approval_and_commits_twice(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _chained_runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create(service, "chained-direct", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["second_phase"] == {"summary": "set work_mem", "what": [GROUP_ADDRESS]}
    assert [entry["desired"] for entry in paused.plan["change_set"]] == ["30000"]
    assert "change_set_phase_2" not in paused.plan
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert _step_sequence(final) == DIRECT_STEP_SEQUENCE
    assert "then_await_approval" not in _step_sequence(final)
    assert final.approved_by_operator_name == "sam"
    assert _live_work_mem() == "40000"
    [phase_one_entry] = final.plan["change_set"]
    [phase_two_entry] = final.plan["change_set_phase_2"]
    assert (phase_one_entry["current"], phase_one_entry["desired"]) == ("16384", "30000")
    assert (phase_two_entry["current"], phase_two_entry["desired"]) == ("30000", "40000")
    assert final.plan["touched_addresses_phase_2"] == [GROUP_ADDRESS]
    assert _last_two_commit_subjects(operator_settings) == [
        f"{TITLE} (phil, approved by sam) (phase 2)",
        f"{TITLE} (phil, approved by sam)",
    ]
    assert "40000" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()


def test_the_phase_two_aws_calls_are_recorded_under_the_phase_two_step_names(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import ExecutionRecord

    _prepare_repo(operator_settings)
    runner, _ = _chained_runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create(service, "chained-records", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    with session_factory() as session:
        modify_steps = [
            record.step_name
            for record in session.query(ExecutionRecord)
            .filter_by(operation_id=operation.id, api_name="rds:ModifyDBParameterGroup")
            .order_by(ExecutionRecord.id)
        ]
    assert modify_steps == ["apply_direct", "then_apply_direct"]


def test_the_operation_detail_shows_both_change_sets_once_the_second_is_computed(
    session_factory, operator_settings, console_config, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _chained_runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create(service, "chained-detail", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    waiting = client.get(f"/api/v1/operations/{operation.id}").json()
    assert [entry["desired"] for entry in waiting["change_set"]] == ["30000"]
    service.approve(operation.id, "sam")
    runner.run_once()
    finished = client.get(f"/api/v1/operations/{operation.id}").json()
    assert [entry["desired"] for entry in finished["change_set"]] == ["30000", "40000"]


def test_chained_terraform_change_plans_and_applies_each_phase_and_commits_twice(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    first = _group_update_plan("16384", "30000")
    second = _group_update_plan("30000", "40000")
    clean = {"resource_changes": []}
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[first, first, clean, second, second, clean]
    )
    runner.services.terraform_runner = fake
    runner.executors[KIND] = SetWorkMemTwiceExecutor(config_store)
    operation = _create(service, "chained-tf", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["second_phase"]["what"] == [GROUP_ADDRESS]
    assert "30000" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert _step_sequence(final) == TERRAFORM_STEP_SEQUENCE
    assert fake.calls.count("apply") == 2
    assert "plan:then_plan" in fake.calls and "plan:then_verify_clean_plan" in fake.calls
    assert final.plan["plan_summary_phase_2"] != final.plan["plan_summary"]
    assert "40000" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    assert _last_two_commit_subjects(operator_settings) == [
        f"{TITLE} (phil, approved by sam) (phase 2)",
        f"{TITLE} (phil, approved by sam)",
    ]
    assert _workspace_changes(operator_settings) == ""


def test_rejecting_a_chained_change_at_its_one_approval_cleans_up_once_and_runs_no_second_phase(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[_group_update_plan("16384", "30000")]
    )
    executor = SetWorkMemTwiceExecutor(config_store)
    cleanups: list[str] = []
    real_on_cancelled = executor.on_cancelled

    def counting_on_cancelled(context: StepContext) -> bool:
        cleanups.append(str(context.operation.id))
        return real_on_cancelled(context)

    executor.on_cancelled = counting_on_cancelled  # type: ignore[method-assign]
    runner.executors[KIND] = executor
    operation = _create(service, "chained-reject", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    service.reject(operation.id, "sam")
    runner.run_once()
    runner.run_once()
    rejected = service.get(operation.id)
    assert rejected.state == OperationState.CANCELLED
    assert cleanups == [str(operation.id)]
    assert _workspace_changes(operator_settings) == ""
    assert not [a for a in rejected.step_attempts if a.step_name.startswith("then_")]
    assert _live_work_mem() == "16384"


def test_the_chained_step_lists_pause_once_in_both_modes(config_store) -> None:
    from rdscommander.control_db.models import Operation

    operation = Operation(operation_kind=KIND, target_key=TARGET_KEY, plan={"options": {}})
    terraform_steps = SetWorkMemTwiceExecutor(config_store).step_names(operation)
    direct_steps = SetWorkMemTwiceExecutor(_direct_store(config_store)).step_names(operation)
    assert terraform_steps == TERRAFORM_STEP_SEQUENCE
    assert direct_steps == DIRECT_STEP_SEQUENCE
    assert terraform_steps.count("await_approval") == direct_steps.count("await_approval") == 1


def test_the_change_each_phase_makes_is_explained_by_the_chained_operation(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """The second phase's closing refresh runs after the first's, so its window must still be open then."""
    from rdscommander.control_db.models import ResourceChangeEvent

    _prepare_repo(operator_settings)
    runner, _ = _chained_runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    operation = _create(service, "chained-history", group_arn)
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    with session_factory() as session:
        events = [
            (event.value_before, event.value_after, event.explanation, event.operation_id)
            for event in session.query(ResourceChangeEvent)
            .filter_by(resource_arn=group_arn)
            .filter(ResourceChangeEvent.attribute_path.like("%work_mem%"))
            .order_by(ResourceChangeEvent.id)
        ]
    assert events == [
        ("16384", "30000", "operation", operation.id),
        ("30000", "40000", "operation", operation.id),
    ]


# ----- final fix wave: a second phase that fails says the first change is already applied -----------------


class _SetWorkMemThatAwsRefuses(SetWorkMem):
    """The second change: AWS refuses the modify, as a throttled or invalid request would be."""

    def apply_direct(self, context: StepContext, entries):  # noqa: ANN001, ANN201
        from botocore.exceptions import ClientError

        raise ClientError(
            {"Error": {"Code": "InvalidDBParameterGroupState", "Message": "the group is being modified"}},
            "ModifyDBParameterGroup",
        )


class _SetWorkMemThatNeverReadsBack(SetWorkMem):
    """The second change: AWS accepts the modify but never reports the value (a terminal verification)."""

    def verify_direct(self, context: StepContext, entries):  # noqa: ANN001, ANN201
        from rdscommander.operations.change_models import VerificationResult

        return VerificationResult(ok=False, live={}, detail="work_mem never read back", terminal=True)


def _run_with_second_change(  # noqa: ANN202
    session_factory, operator_settings, config_store, sandbox_snapshot, second_change_class, key: str
):
    """Run a direct chained change whose second change is `second_change_class`; returns the operation."""

    class FailingSecondPhaseExecutor(SetWorkMemTwiceExecutor):
        def second_change(self, context: StepContext) -> SetWorkMem:
            return second_change_class(
                context.operation.resource_arn, context.operation.plan["options"]["second"]
            )

    _prepare_repo(operator_settings)
    runner, _ = _chained_runner(session_factory, operator_settings, _direct_store(config_store))
    runner.executors[KIND] = FailingSecondPhaseExecutor(_direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create(service, key, arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    return service.get(operation.id)


def _first_phase_commit_sha(operation) -> str:  # noqa: ANN001
    attempt = next(a for a in operation.step_attempts if a.step_name == "record_to_checkout" and a.output)
    return attempt.output["commit_sha"]


def test_a_second_phase_exception_says_the_first_change_is_applied_and_committed(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final = _run_with_second_change(
        session_factory, operator_settings, config_store, sandbox_snapshot, _SetWorkMemThatAwsRefuses, "c-exc"
    )
    assert final.state == OperationState.FAILED
    assert _live_work_mem() == "30000", "the first change is in AWS"
    sha = _first_phase_commit_sha(final)
    [diagnostic] = [d for d in final.diagnostic_reports if d.severity == "error"]
    assert diagnostic.step_name == "then_apply_direct"
    assert "InvalidDBParameterGroupState" in diagnostic.detail
    assert f"The first change (set work_mem) is already applied and committed as {sha}" in diagnostic.detail
    assert "run the operation again" in diagnostic.fix_suggestion
    assert sha in diagnostic.fix_suggestion


def test_a_second_phase_failure_says_the_first_change_is_applied_and_committed(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final = _run_with_second_change(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        _SetWorkMemThatNeverReadsBack,
        "c-fail",
    )
    assert final.state == OperationState.FAILED
    sha = _first_phase_commit_sha(final)
    [diagnostic] = [d for d in final.diagnostic_reports if d.severity == "error"]
    assert (diagnostic.step_name, diagnostic.diagnostic_kind) == (
        "then_verify_direct",
        "direct_change_not_verified",
    )
    assert "work_mem never read back" in diagnostic.detail
    assert f"The first change (set work_mem) is already applied and committed as {sha}" in diagnostic.detail
    assert sha in diagnostic.fix_suggestion
    assert final.failure_summary


def test_a_first_phase_failure_says_nothing_about_a_first_change_being_applied(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    monkeypatch.setattr(SetWorkMem, "apply_direct", _SetWorkMemThatAwsRefuses.apply_direct)
    final = _run_with_second_change(
        session_factory, operator_settings, config_store, sandbox_snapshot, SetWorkMem, "c-first"
    )
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.severity == "error"]
    assert diagnostic.step_name == "apply_direct"
    assert "already applied" not in diagnostic.detail
