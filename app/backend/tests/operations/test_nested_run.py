"""A step that drives another operation to its end inline, recording it exactly as the runner would."""

from __future__ import annotations

from datetime import timedelta

from rdscommander.control_db.models import Environment, Operation, utc_now
from rdscommander.operations.enums import OperationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepOutcome
from rdscommander.operations.nested_run import (
    NESTED_PAUSE_DIAGNOSTIC_KIND,
    create_nested_operation,
    run_nested_operation,
)
from rdscommander.services import ServiceBundle
from tests.operations.test_runner import RecordingExecutor

PARENT_RUNNER_IDENTITY = "runner-parent"


class PausingExecutor(RecordingExecutor):
    """Pauses for approval at `second` even when the operation is approved."""

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        self.executed.append(step_name)
        if step_name == "second":
            return StepOutcome(status=StepOutcomeStatus.AWAITING_APPROVAL, detail="wants a person")
        return StepOutcome(status=StepOutcomeStatus.COMPLETED)


def _parent_context(session_factory, operator_settings, config_store, session) -> StepContext:
    session.add(Environment(environment_slug="sandbox", display_name="Sandbox", requires_approval=True))
    parent = Operation(
        operation_kind="test_parent",
        state=OperationState.RUNNING.value,
        environment_slug="sandbox",
        target_key=None,
        resource_arn="arn:aws:rds:eu-west-2:000000000000:db:demo-orders",
        title="parent",
        plan={},
        idempotency_key="parent-1",
        created_by_operator_name="phil",
        lease_owner=PARENT_RUNNER_IDENTITY,
        lease_expires_at_utc=utc_now() + timedelta(seconds=30),
    )
    session.add(parent)
    session.commit()
    return StepContext(
        session=session,
        operation=parent,
        settings=operator_settings,
        console_config=config_store.current,
        services=ServiceBundle(session_factory=session_factory, sleep=lambda seconds: None),
        runner_identity=PARENT_RUNNER_IDENTITY,
    )


def _child(session_factory, child_id) -> Operation:
    with session_factory() as session:
        child = session.get(Operation, child_id)
        _ = child.step_attempts, child.diagnostic_reports
        session.expunge_all()
        return child


def _create_child(context: StepContext, key: str = "child-1"):
    return create_nested_operation(
        context,
        operation_kind="test_kind",
        plan={"options": {"value": 1}},
        title="child",
        idempotency_key=key,
    )


def test_create_nested_operation_is_approved_by_the_parent_creator_running_and_leased_by_the_parent_runner(
    session_factory, operator_settings, config_store
) -> None:
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        child_id = _create_child(context)
        assert _create_child(context) == child_id  # the same key returns the same child
        parent_id = context.operation.id
    child = _child(session_factory, child_id)
    assert child.state == OperationState.RUNNING
    assert child.approved_by_operator_name == "phil" and child.approved_at_utc is not None
    assert child.created_by_operator_name == "phil"
    assert child.lease_owner == PARENT_RUNNER_IDENTITY and child.lease_expires_at_utc is not None
    assert child.plan == {"options": {"value": 1}, "approved_with_operation_id": str(parent_id)}
    assert (child.environment_slug, child.resource_arn) == (
        "sandbox",
        "arn:aws:rds:eu-west-2:000000000000:db:demo-orders",
    )


def test_create_nested_operation_carries_the_approver_and_approval_time_of_an_approved_parent(
    session_factory, operator_settings, config_store
) -> None:
    approved_at_utc = utc_now() - timedelta(hours=2)
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        context.operation.approved_by_operator_name = "sam"
        context.operation.approved_at_utc = approved_at_utc
        session.commit()
        child_id = _create_child(context, "child-approved")
    child = _child(session_factory, child_id)
    assert (child.approved_by_operator_name, child.approved_at_utc) == ("sam", approved_at_utc)
    assert child.created_by_operator_name == "phil"


def test_run_nested_operation_records_every_step_attempt_and_succeeds_the_child(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor()
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        child_id = _create_child(context)
        result = run_nested_operation(context, child_id, executor)
    assert result.state == OperationState.SUCCEEDED and result.operation_id == child_id
    assert executor.executed == ["first", "second", "third"]
    child = _child(session_factory, child_id)
    assert child.state == OperationState.SUCCEEDED and child.finished_at_utc is not None
    assert child.lease_owner is None
    assert [(a.step_name, a.outcome_status) for a in child.step_attempts] == [
        ("first", "completed"),
        ("second", "completed"),
        ("third", "completed"),
    ]


def test_run_nested_operation_fails_the_child_with_its_diagnostic_when_a_step_fails(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor(fail_on="second")
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        child_id = _create_child(context)
        result = run_nested_operation(context, child_id, executor)
    assert result.state == OperationState.FAILED
    assert result.failure_summary == "nope" and result.fix_suggestion == "try again"
    child = _child(session_factory, child_id)
    assert child.state == OperationState.FAILED
    [diagnostic] = child.diagnostic_reports
    assert (diagnostic.step_name, diagnostic.detail) == ("second", "nope")


def test_run_nested_operation_turns_an_exception_into_a_diagnostic_on_the_child(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor(raise_on="first")
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        child_id = _create_child(context)
        result = run_nested_operation(context, child_id, executor)
    assert result.state == OperationState.FAILED
    child = _child(session_factory, child_id)
    # As in the runner: the interrupted attempt stays open and a failed attempt records the exception.
    assert [(a.step_name, a.outcome_status) for a in child.step_attempts] == [
        ("first", None),
        ("first", "failed"),
    ]
    assert "boom" in child.diagnostic_reports[0].detail


def test_run_nested_operation_fails_a_child_that_pauses_because_no_one_can_approve_it_inline(
    session_factory, operator_settings, config_store
) -> None:
    executor = PausingExecutor()
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        child_id = _create_child(context)
        result = run_nested_operation(context, child_id, executor)
    assert result.state == OperationState.FAILED
    child = _child(session_factory, child_id)
    assert child.state == OperationState.FAILED and child.lease_owner is None
    assert NESTED_PAUSE_DIAGNOSTIC_KIND in [d.diagnostic_kind for d in child.diagnostic_reports]


def test_run_nested_operation_resumes_a_child_left_running_after_the_completed_steps(
    session_factory, operator_settings, config_store
) -> None:
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        child_id = _create_child(context)
        first_pass = RecordingExecutor(raise_on="second")
        run_nested_operation(context, child_id, first_pass)
        # A crash after `first` leaves the child RUNNING with an expired lease and `first` complete.
        child = session.get(Operation, child_id)
        child.state = OperationState.RUNNING.value
        child.failure_summary = None
        child.finished_at_utc = None
        child.lease_owner = "crashed-runner"
        child.lease_expires_at_utc = utc_now() - timedelta(seconds=5)
        session.commit()
        resumed = RecordingExecutor()
        result = run_nested_operation(context, child_id, resumed)
    assert result.state == OperationState.SUCCEEDED
    assert resumed.executed == ["second", "third"]


def test_run_nested_operation_leaves_a_child_whose_lease_another_runner_holds(
    session_factory, operator_settings, config_store
) -> None:
    executor = RecordingExecutor()
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        child_id = _create_child(context)
        child = session.get(Operation, child_id)
        child.lease_owner = "another-runner"
        child.lease_expires_at_utc = utc_now() + timedelta(seconds=60)
        session.commit()
        result = run_nested_operation(context, child_id, executor)
    assert result.state == OperationState.RUNNING and executor.executed == []
    assert "another-runner" in (result.failure_summary or "")


def test_run_nested_operation_reports_a_finished_child_without_running_it_again(
    session_factory, operator_settings, config_store
) -> None:
    with session_factory() as session:
        context = _parent_context(session_factory, operator_settings, config_store, session)
        child_id = _create_child(context)
        run_nested_operation(context, child_id, RecordingExecutor())
        again = RecordingExecutor()
        result = run_nested_operation(context, child_id, again)
    assert result.state == OperationState.SUCCEEDED and again.executed == []
