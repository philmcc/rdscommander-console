from __future__ import annotations

import threading

from rdscommander.control_db.models import DiagnosticReport, Environment, Operation, OperationStepAttempt
from rdscommander.operations.enums import ObservationState, OperationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.services import ServiceBundle


class RecordingExecutor:
    operation_kind = "test_kind"

    def __init__(
        self, fail_on: str | None = None, pause_on: str | None = None, raise_on: str | None = None
    ) -> None:
        self.executed: list[str] = []
        self.fail_on = fail_on
        self.pause_on = pause_on
        self.raise_on = raise_on

    def step_names(self, operation: Operation) -> list[str]:
        return ["first", "second", "third"]

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        if context.step_already_completed(step_name):
            return StepObservation(state=ObservationState.SATISFIED, detail="done earlier")
        if step_name == self.pause_on and context.operation.approved_by_operator_name:
            return StepObservation(state=ObservationState.SATISFIED, detail="approved")
        return StepObservation(state=ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        self.executed.append(step_name)
        if step_name == self.raise_on:
            raise RuntimeError("boom")
        if step_name == self.fail_on:
            return StepOutcome(status=StepOutcomeStatus.FAILED, detail="nope", fix_suggestion="try again")
        if step_name == self.pause_on:
            return StepOutcome(status=StepOutcomeStatus.AWAITING_APPROVAL, detail="plan ready")
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"step": step_name})


def _prepare(
    session_factory, executor, operator_settings, config_store, runner_identity="runner-a"
) -> tuple[OperationService, OperationRunner]:
    with session_factory() as session:
        session.add(Environment(environment_slug="sandbox", display_name="Sandbox", requires_approval=True))
        session.commit()
    service = OperationService(session_factory)
    runner = OperationRunner(
        session_factory,
        {executor.operation_kind: executor},
        runner_identity,
        operator_settings,
        config_store,
        ServiceBundle(),
        lease_seconds=30,
    )
    return service, runner


def test_runner_walks_every_step_and_succeeds(session_factory, operator_settings, config_store) -> None:
    executor = RecordingExecutor()
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    operation = service.create("test_kind", "sandbox", None, {}, "k1", "phil")
    assert runner.run_once() == operation.id
    assert executor.executed == ["first", "second", "third"]
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        attempts = session.query(OperationStepAttempt).filter_by(operation_id=operation.id).all()
        assert [(a.step_name, a.outcome_status) for a in attempts] == [
            ("first", "completed"),
            ("second", "completed"),
            ("third", "completed"),
        ]


def test_runner_pauses_for_approval_and_resumes_without_repeating_steps(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor(pause_on="second")
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    operation = service.create("test_kind", "sandbox", None, {}, "k2", "phil")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    assert runner.run_once() is None
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    assert executor.executed == ["first", "second", "third"]


def test_runner_fails_with_diagnostic_on_failed_step(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor(fail_on="second")
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    operation = service.create("test_kind", "sandbox", None, {}, "k3", "phil")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.FAILED
    with session_factory() as session:
        [report] = session.query(DiagnosticReport).filter_by(operation_id=operation.id).all()
        assert report.fix_suggestion == "try again"
        assert report.step_name == "second"


def test_runner_turns_exceptions_into_failed_operations_with_diagnostics(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor(raise_on="first")
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    operation = service.create("test_kind", "sandbox", None, {}, "k4", "phil")
    runner.run_once()
    row = service.get(operation.id)
    assert row.state == OperationState.FAILED
    assert "boom" in (row.failure_summary or "")
    with session_factory() as session:
        [report] = session.query(DiagnosticReport).filter_by(operation_id=operation.id).all()
        assert report.diagnostic_kind == "unhandled_exception"


def test_two_runners_never_execute_the_same_operation(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor()
    service, runner_a = _prepare(session_factory, executor, operator_settings, config_store, "runner-a")
    runner_b = OperationRunner(
        session_factory,
        {executor.operation_kind: executor},
        "runner-b",
        operator_settings,
        config_store,
        ServiceBundle(),
        lease_seconds=30,
    )
    for index in range(6):
        service.create("test_kind", "sandbox", None, {}, f"race-{index}", "phil")
    processed: list = []
    lock = threading.Lock()

    def drain(runner: OperationRunner) -> None:
        while True:
            result = runner.run_once()
            if result is None:
                return
            with lock:
                processed.append(result)

    threads = [
        threading.Thread(target=drain, args=(runner_a,)),
        threading.Thread(target=drain, args=(runner_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(processed) == 6 and len(set(processed)) == 6
    assert executor.executed.count("first") == 6


class SlowExecutor:
    operation_kind = "slow_kind"

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def step_names(self, operation: Operation) -> list[str]:
        return ["slow"]

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        return StepObservation(state=ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        import time

        time.sleep(self.seconds)
        return StepOutcome(status=StepOutcomeStatus.COMPLETED)


def test_lease_is_renewed_while_a_long_step_runs(session_factory, operator_settings, config_store) -> None:
    """A second runner must not be able to claim an operation whose step outlives one lease period."""
    import time

    with session_factory() as session:
        session.add(Environment(environment_slug="sandbox", display_name="Sandbox", requires_approval=True))
        session.commit()
    service = OperationService(session_factory)
    executor = SlowExecutor(seconds=3.0)
    runner_a = OperationRunner(
        session_factory,
        {executor.operation_kind: executor},
        "runner-a",
        operator_settings,
        config_store,
        ServiceBundle(),
        lease_seconds=1,
    )
    runner_b = OperationRunner(
        session_factory,
        {executor.operation_kind: executor},
        "runner-b",
        operator_settings,
        config_store,
        ServiceBundle(),
        lease_seconds=1,
    )
    operation = service.create("slow_kind", "sandbox", None, {}, "slow-1", "phil")
    worker = threading.Thread(target=runner_a.run_once)
    worker.start()
    time.sleep(1.6)
    assert runner_b.run_once() is None
    worker.join()
    assert service.get(operation.id).state == OperationState.SUCCEEDED


class ClientErrorExecutor:
    operation_kind = "aws_error_kind"

    def __init__(self, code: str) -> None:
        self.code = code

    def step_names(self, operation: Operation) -> list[str]:
        return ["call_aws"]

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        return StepObservation(state=ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": self.code, "Message": "nope"}}, "DescribeDBInstances")


def test_expired_credentials_produce_a_diagnostic_naming_the_sso_login_command(
    session_factory, operator_settings, config_store
) -> None:
    with session_factory() as session:
        session.add(
            Environment(environment_slug="production", display_name="Production", requires_approval=True)
        )
        session.commit()
    service = OperationService(session_factory)
    executor = ClientErrorExecutor("ExpiredToken")
    runner = OperationRunner(
        session_factory,
        {executor.operation_kind: executor},
        "r",
        operator_settings,
        config_store,
        ServiceBundle(),
    )
    operation = service.create(
        "aws_error_kind", "production", "production/123456789012/eu-west-2", {}, "exp-1", "phil"
    )
    runner.run_once()
    row = service.get(operation.id)
    assert row.state == OperationState.FAILED
    [report] = row.diagnostic_reports
    assert report.diagnostic_kind == "aws_credentials_expired"
    assert "aws sso login --profile testco-prod" in report.fix_suggestion


def test_access_denied_produces_a_diagnostic_naming_the_action_and_policy(
    session_factory, operator_settings, config_store
) -> None:
    with session_factory() as session:
        session.add(
            Environment(environment_slug="production", display_name="Production", requires_approval=True)
        )
        session.commit()
    service = OperationService(session_factory)
    executor = ClientErrorExecutor("AccessDenied")
    runner = OperationRunner(
        session_factory,
        {executor.operation_kind: executor},
        "r",
        operator_settings,
        config_store,
        ServiceBundle(),
    )
    operation = service.create(
        "aws_error_kind", "production", "production/123456789012/eu-west-2", {}, "den-1", "phil"
    )
    runner.run_once()
    [report] = service.get(operation.id).diagnostic_reports
    assert report.diagnostic_kind == "aws_access_denied"
    assert (
        "rds:DescribeDBInstances" in report.fix_suggestion
        and "docs/iam/rdscommander-policy.json" in report.fix_suggestion
    )


class CleaningExecutor(RecordingExecutor):
    """Pauses for approval at `second` and cleans up after a rejection."""

    operation_kind = "test_cleaning_kind"

    def __init__(self, hook_result: bool | None = True, hook_error: Exception | None = None) -> None:
        super().__init__(pause_on="second")
        self.cleaned: list[str] = []
        self.hook_result = hook_result
        self.hook_error = hook_error

    def on_cancelled(self, context: StepContext) -> bool | None:
        self.cleaned.append(f"{context.operation.id}@{context.runner_identity}")
        if self.hook_error is not None:
            raise self.hook_error
        return self.hook_result


def _reject_a_waiting_operation(service, runner, key: str):
    operation = service.create(CleaningExecutor.operation_kind, "sandbox", None, {}, key, "phil")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    service.reject(operation.id, "sam")
    return operation


def test_runner_cleans_up_a_rejected_operation_exactly_once(
    session_factory, operator_settings, config_store
) -> None:
    executor = CleaningExecutor()
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    operation = _reject_a_waiting_operation(service, runner, "clean-1")
    assert service.get(operation.id).plan["cleanup_pending"] is True
    assert runner.run_once() is None, "cleaning up is not claiming work"
    assert executor.cleaned == [f"{operation.id}@runner-a"]
    plan = service.get(operation.id).plan
    assert "cleanup_pending" not in plan and plan["cleanup_done_at_utc"]
    runner.run_once()
    runner.run_once()
    assert len(executor.cleaned) == 1


def test_runner_cleans_up_before_it_claims_new_work(session_factory, operator_settings, config_store) -> None:
    executor = CleaningExecutor()
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    rejected = _reject_a_waiting_operation(service, runner, "clean-2")
    queued = service.create(CleaningExecutor.operation_kind, "sandbox", None, {}, "clean-3", "phil")
    assert runner.run_once() == queued.id
    assert executor.cleaned == [f"{rejected.id}@runner-a"]


def test_runner_leaves_the_cleanup_pending_when_the_hook_says_the_files_are_elsewhere(
    session_factory, operator_settings, config_store
) -> None:
    executor = CleaningExecutor(hook_result=False)
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    operation = _reject_a_waiting_operation(service, runner, "clean-4")
    runner.run_once()
    assert len(executor.cleaned) == 1
    assert service.get(operation.id).plan["cleanup_pending"] is True
    executor.hook_result = True
    runner.run_once()
    assert len(executor.cleaned) == 2
    assert "cleanup_pending" not in service.get(operation.id).plan


def test_a_failing_cleanup_is_reported_once_and_does_not_stop_the_runner(
    session_factory, operator_settings, config_store
) -> None:
    executor = CleaningExecutor(hook_error=RuntimeError("git is unavailable"))
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    operation = _reject_a_waiting_operation(service, runner, "clean-5")
    runner.run_once()
    runner.run_once()
    assert len(executor.cleaned) == 1
    finished = service.get(operation.id)
    assert "cleanup_pending" not in finished.plan and finished.plan["cleanup_done_at_utc"]
    [diagnostic] = finished.diagnostic_reports
    assert diagnostic.diagnostic_kind == "cleanup_failed" and diagnostic.severity == "warning"
    assert "git is unavailable" in diagnostic.detail and diagnostic.fix_suggestion


def test_the_flag_of_an_executor_without_a_hook_is_cleared(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor(pause_on="second")
    service, runner = _prepare(session_factory, executor, operator_settings, config_store)
    operation = service.create("test_kind", "sandbox", None, {}, "clean-6", "phil")
    runner.run_once()
    service.reject(operation.id, "sam")
    runner.run_once()
    plan = service.get(operation.id).plan
    assert "cleanup_pending" not in plan and plan["cleanup_done_at_utc"]
