import pytest

from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService, OperationStateError


def _sync_environment(session_factory) -> None:
    from rdscommander.control_db.models import Environment, EnvironmentTargetRecord

    with session_factory() as session:
        session.add(Environment(environment_slug="sandbox", display_name="Sandbox", requires_approval=True))
        session.add(
            EnvironmentTargetRecord(
                target_key="sandbox/000000000000/eu-west-2",
                environment_slug="sandbox",
                aws_account_id="000000000000",
                aws_region="eu-west-2",
                aws_profile_name=None,
                is_simulated=True,
            )
        )
        session.commit()


def test_create_is_idempotent_on_key(session_factory) -> None:
    _sync_environment(session_factory)
    service = OperationService(session_factory)
    first = service.create("inventory_refresh", "sandbox", None, {"x": 1}, "key-1", "phil")
    second = service.create("inventory_refresh", "sandbox", None, {"x": 2}, "key-1", "phil")
    assert first.id == second.id
    assert second.plan == {"x": 1}
    assert first.state == OperationState.QUEUED


def test_approve_requires_awaiting_state_and_records_operator(session_factory) -> None:
    _sync_environment(session_factory)
    service = OperationService(session_factory)
    operation = service.create(
        "terraform_adoption", "sandbox", "sandbox/000000000000/eu-west-2", {}, "key-2", "phil"
    )
    with pytest.raises(OperationStateError, match="AWAITING_APPROVAL"):
        service.approve(operation.id, "sam")
    with session_factory() as session:
        row = session.get(type(operation), operation.id)
        row.state = OperationState.AWAITING_APPROVAL
        session.commit()
    approved = service.approve(operation.id, "sam")
    assert approved.state == OperationState.QUEUED
    assert approved.approved_by_operator_name == "sam"


def test_reject_cancels_with_summary(session_factory) -> None:
    _sync_environment(session_factory)
    service = OperationService(session_factory)
    operation = service.create("terraform_adoption", "sandbox", None, {}, "key-3", "phil")
    with session_factory() as session:
        row = session.get(type(operation), operation.id)
        row.state = OperationState.AWAITING_APPROVAL
        session.commit()
    rejected = service.reject(operation.id, "sam")
    assert rejected.state == OperationState.CANCELLED
    assert "sam" in (rejected.failure_summary or "")
    assert rejected.plan == {"cleanup_pending": True}, "the runner is told to let the executor clean up"


def test_create_records_resource_title_and_approval_text(session_factory) -> None:
    _sync_environment(session_factory)
    service = OperationService(session_factory)
    operation = service.create(
        "terraform_adoption",
        None,
        None,
        {},
        "key-resource",
        "phil",
        resource_arn="arn:aws:rds:eu-west-2:000000000000:db:demo-orders",
        title="Adopt resources: demo-orders",
        approval_required_text="demo-orders",
    )
    assert operation.environment_slug is None
    assert operation.resource_arn == "arn:aws:rds:eu-west-2:000000000000:db:demo-orders"
    assert operation.title == "Adopt resources: demo-orders"
    assert operation.approval_required_text == "demo-orders"


def test_list_keeps_operations_in_any_of_the_given_states(session_factory) -> None:
    _sync_environment(session_factory)
    service = OperationService(session_factory)
    created_ids = {}
    for index, state in enumerate(("QUEUED", "RUNNING", "AWAITING_APPROVAL", "SUCCEEDED", "FAILED")):
        operation = service.create("inventory_refresh", "sandbox", None, {}, f"states-{index}", "phil")
        with session_factory() as session:
            session.get(type(operation), operation.id).state = state
            session.commit()
        created_ids[state] = operation.id
    in_progress = service.list(states=["QUEUED", "RUNNING", "AWAITING_APPROVAL"])
    assert {operation.id for operation in in_progress} == {
        created_ids["QUEUED"],
        created_ids["RUNNING"],
        created_ids["AWAITING_APPROVAL"],
    }
    assert [operation.id for operation in service.list(states=["FAILED"])] == [created_ids["FAILED"]]
    assert len(service.list(states=[])) == 5, "an empty list of states is no filter"
    assert len(service.list()) == 5


def test_approve_called_directly_checks_the_stage_and_the_typed_identifier(session_factory) -> None:
    from rdscommander.control_db.models import Operation
    from rdscommander.operations.service import ApprovalMismatchError

    _sync_environment(session_factory)
    service = OperationService(session_factory)
    operation = service.create(
        "major_upgrade",
        "sandbox",
        None,
        {"approval_stage": "create_green"},
        "direct-approve",
        "phil",
        approval_required_text="demo-orders",
    )
    with session_factory() as session:
        session.get(Operation, operation.id).state = OperationState.AWAITING_APPROVAL.value
        session.commit()
    with pytest.raises(ApprovalMismatchError) as stage_error:
        service.approve(operation.id, "sam", approval_stage="switchover", confirmation_text="demo-orders")
    assert stage_error.value.diagnostic_kind == "approval_stage_mismatch"
    with pytest.raises(ApprovalMismatchError) as text_error:
        service.approve(operation.id, "sam", approval_stage="create_green", confirmation_text="nope")
    assert text_error.value.diagnostic_kind == "confirmation_text_mismatch"
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    approved = service.approve(
        operation.id, "sam", approval_stage="create_green", confirmation_text="demo-orders"
    )
    assert approved.state == OperationState.QUEUED and approved.approved_by_operator_name == "sam"
