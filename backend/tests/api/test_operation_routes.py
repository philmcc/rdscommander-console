from fastapi.testclient import TestClient

from rdscommander.control_db.models import Operation
from rdscommander.main import create_application


def test_operation_list_detail_and_approval_flow(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    created = client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "ops-1"},
    ).json()
    app.state.runner.run_once()
    listing = client.get("/api/v1/operations", params={"environment_slug": "sandbox"}).json()
    assert listing["operations"][0]["id"] == created["operation_id"]
    assert listing["operations"][0]["state"] == "SUCCEEDED"
    detail = client.get(f"/api/v1/operations/{created['operation_id']}").json()
    assert detail["step_attempts"][0]["step_name"] == "refresh:sandbox/000000000000/eu-west-2"
    assert any(r["record_kind"] == "aws_api" for r in detail["execution_records"])
    approve = client.post(
        f"/api/v1/operations/{created['operation_id']}/approve", headers={"X-CSRF-Token": token}
    )
    assert approve.status_code == 409
    assert client.get("/api/v1/operations/00000000-0000-0000-0000-000000000000").status_code == 404


def test_operation_detail_carries_the_change_set_and_what_the_review_needs(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.operations.service import OperationService
    from tests.operations.test_change_strategy import GROUP, GROUP_ADDRESS, SetWorkMemExecutor
    from tests.terraform.conftest import TARGET_KEY, arn_for
    from tests.terraform.test_adoption_executor import _prepare_repo

    _prepare_repo(operator_settings)
    direct_config = console_config.model_copy(deep=True)
    direct_config.environment("sandbox").change_management = "direct"
    app = create_application(
        settings=operator_settings, console_config=direct_config, session_factory=session_factory
    )
    app.state.runner.executors["test_set_work_mem"] = SetWorkMemExecutor(app.state.config_store)
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    operation = OperationService(session_factory).create(
        "test_set_work_mem",
        "sandbox",
        TARGET_KEY,
        {"options": {"value": "40000"}, "resource_arn": group_arn},
        "detail-1",
        "phil",
        resource_arn=group_arn,
        title="Edit parameter group",
    )
    app.state.runner.run_once()
    waiting = client.get(f"/api/v1/operations/{operation.id}").json()
    assert waiting["state"] == "AWAITING_APPROVAL"
    assert waiting["change_set"] == [
        {
            "address": GROUP_ADDRESS,
            "attribute": "parameters.work_mem",
            "current": "16384",
            "desired": "40000",
            "apply_type": "immediate",
            "requires_reboot": False,
            "downtime": "none",
        }
    ]
    assert waiting["change_set_verification"] is None
    assert waiting["requires_reboot"] is False and waiting["downtime"] == "none"
    assert waiting["pending_reboot_attributes"] == [] and waiting["foreign_owner_banner"] is None
    assert waiting["touched_addresses"] == [GROUP_ADDRESS]
    approved = client.post(f"/api/v1/operations/{operation.id}/approve", headers={"X-CSRF-Token": token})
    assert approved.status_code == 200
    app.state.runner.run_once()
    done = client.get(f"/api/v1/operations/{operation.id}").json()
    assert done["state"] == "SUCCEEDED"
    assert done["change_set_verification"] == {"work_mem": "40000"}


def test_operation_detail_of_an_operation_without_a_change_set_has_neutral_review_fields(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    created = client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "ops-neutral"},
    ).json()
    detail = client.get(f"/api/v1/operations/{created['operation_id']}").json()
    assert detail["change_set"] is None and detail["change_set_verification"] is None
    assert detail["requires_reboot"] is False and detail["downtime"] == "none"
    assert detail["pending_reboot_attributes"] == [] and detail["touched_addresses"] == []
    assert detail["foreign_owner_banner"] is None


def test_operation_list_filters_by_resource_and_detail_carries_change_set_fields(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import Operation
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        session.add(
            Operation(
                operation_kind="parameter_group_attach",
                state="AWAITING_APPROVAL",
                environment_slug="sandbox",
                target_key="sandbox/000000000000/eu-west-2",
                plan={
                    "options": {},
                    "change_set": [
                        {
                            "address": "aws_db_instance.demo_orders",
                            "attribute": "parameter_group_name",
                            "current": "a",
                            "desired": "b",
                            "apply_type": "pending-reboot",
                            "requires_reboot": True,
                            "downtime": "a reboot now",
                        }
                    ],
                    "requires_reboot": True,
                    "downtime": "a reboot now",
                },
                idempotency_key="list-1",
                created_by_operator_name="phil",
                resource_arn=orders_arn,
                title="Attach parameter group: demo-orders",
                approval_required_text="demo-orders",
            )
        )
        session.commit()
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    listed = client.get("/api/v1/operations", params={"resource_id": encode_resource_id(orders_arn)}).json()[
        "operations"
    ]
    assert [o["title"] for o in listed] == ["Attach parameter group: demo-orders"]
    assert listed[0]["resource_id"] == encode_resource_id(orders_arn)
    assert listed[0]["progress"] is None, "a plan the executor rejects leaves progress out and lists anyway"
    detail = client.get(f"/api/v1/operations/{listed[0]['id']}").json()
    assert detail["change_set"][0]["attribute"] == "parameter_group_name"
    assert detail["requires_reboot"] is True
    assert detail["approval_required_text"] == "demo-orders"
    assert detail["resource_id"] == encode_resource_id(orders_arn)


def test_operation_list_filters_by_state_and_rejects_a_malformed_resource_id(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import Operation

    with session_factory() as session:
        for state, key in (("RUNNING", "state-running"), ("SUCCEEDED", "state-succeeded")):
            session.add(
                Operation(
                    operation_kind="inventory_refresh",
                    state=state,
                    environment_slug="sandbox",
                    target_key=None,
                    plan={"options": {}},
                    idempotency_key=key,
                    created_by_operator_name="phil",
                    title=f"Refresh inventory ({state})",
                )
            )
        session.commit()
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    running = client.get("/api/v1/operations", params={"state": "RUNNING"}).json()["operations"]
    assert [o["state"] for o in running] == ["RUNNING"]
    assert running[0]["resource_id"] is None
    assert client.get("/api/v1/operations", params={"resource_id": "not-a-resource-id"}).status_code == 422


def _add_operation(session, kind: str, state: str, key: str, **columns) -> Operation:
    operation = Operation(
        operation_kind=kind,
        state=state,
        environment_slug="sandbox",
        target_key=columns.pop("target_key", None),
        plan={"options": {}},
        idempotency_key=key,
        created_by_operator_name="phil",
        title=columns.pop("title", f"{kind} ({state})"),
        **columns,
    )
    session.add(operation)
    session.flush()
    return operation


def test_operation_list_state_filter_accepts_several_states_separated_by_commas(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    with session_factory() as session:
        for state in ("QUEUED", "RUNNING", "AWAITING_APPROVAL", "SUCCEEDED", "FAILED"):
            _add_operation(session, "inventory_refresh", state, f"multi-{state}")
        session.commit()
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )

    def states_for(state: str) -> set[str]:
        response = client.get("/api/v1/operations", params={"state": state})
        assert response.status_code == 200
        return {o["state"] for o in response.json()["operations"]}

    assert states_for("QUEUED,RUNNING,AWAITING_APPROVAL") == {"QUEUED", "RUNNING", "AWAITING_APPROVAL"}
    assert states_for("RUNNING") == {"RUNNING"}
    assert states_for(" FAILED , SUCCEEDED,") == {"FAILED", "SUCCEEDED"}
    assert states_for("NOT_A_STATE") == set()


def test_operation_list_reports_step_progress_for_operations_that_have_not_finished(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import OperationStepAttempt
    from rdscommander.terraform.adoption_executor import STEP_NAMES
    from tests.terraform.conftest import TARGET_KEY

    def attempt(operation, step_name: str, number: int, outcome: str | None) -> OperationStepAttempt:
        return OperationStepAttempt(
            operation_id=operation.id,
            step_name=step_name,
            attempt_number=number,
            observation_state="ABSENT",
            outcome_status=outcome,
        )

    with session_factory() as session:
        running = _add_operation(
            session, "terraform_adoption", "RUNNING", "progress-running", target_key=TARGET_KEY
        )
        # The runner passes over the steps again when it resumes, so a step can complete twice.
        session.add_all(
            [
                attempt(running, STEP_NAMES[0], 1, "completed"),
                attempt(running, STEP_NAMES[1], 1, "completed"),
                attempt(running, STEP_NAMES[0], 2, "completed"),
                attempt(running, STEP_NAMES[1], 2, "completed"),
                attempt(running, STEP_NAMES[2], 1, None),
            ]
        )
        queued = _add_operation(
            session, "terraform_adoption", "QUEUED", "progress-queued", target_key=TARGET_KEY
        )
        waiting = _add_operation(
            session, "terraform_adoption", "AWAITING_APPROVAL", "progress-waiting", target_key=TARGET_KEY
        )
        session.add_all(
            [attempt(waiting, step_name, 1, "completed") for step_name in STEP_NAMES[:7]]
            + [attempt(waiting, STEP_NAMES[7], 1, "awaiting_approval")]
        )
        done = _add_operation(
            session, "terraform_adoption", "SUCCEEDED", "progress-done", target_key=TARGET_KEY
        )
        session.add(attempt(done, STEP_NAMES[0], 1, "completed"))
        unknown_kind = _add_operation(session, "kind_without_an_executor", "RUNNING", "progress-unknown")
        session.commit()
        ids = {
            "running": str(running.id),
            "queued": str(queued.id),
            "waiting": str(waiting.id),
            "done": str(done.id),
            "unknown_kind": str(unknown_kind.id),
        }
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    listed = {o["id"]: o for o in client.get("/api/v1/operations").json()["operations"]}
    total = len(STEP_NAMES)
    assert listed[ids["running"]]["progress"] == {
        "completed_steps": 2,
        "total_steps": total,
        "current_step": STEP_NAMES[2],
    }
    assert listed[ids["queued"]]["progress"] == {
        "completed_steps": 0,
        "total_steps": total,
        "current_step": None,
    }
    assert listed[ids["waiting"]]["progress"] == {
        "completed_steps": 7,
        "total_steps": total,
        "current_step": "await_approval",
    }
    assert listed[ids["done"]]["progress"] is None
    assert listed[ids["unknown_kind"]]["progress"] is None


def test_a_malformed_path_query_or_body_answers_422_with_a_sentence_and_field_problems(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]

    not_a_uuid = client.get("/api/v1/operations/not-a-uuid")
    assert not_a_uuid.status_code == 422
    body = not_a_uuid.json()
    assert isinstance(body["detail"], str) and "operation_id" in body["detail"]
    assert body["problems"] == ["operation_id: must be a UUID such as 3fa85f64-5717-4562-b3fc-2c963f66afa6"]

    zero_limit = client.get("/api/v1/operations", params={"limit": 0}).json()
    assert zero_limit["problems"] == ["limit: must be at least 1"]

    without_key = client.post(
        "/api/v1/operations", headers={"X-CSRF-Token": token}, json={"kind": "inventory_refresh"}
    )
    assert without_key.status_code == 422
    assert without_key.json()["problems"] == ["idempotency_key: is required"]
    assert isinstance(without_key.json()["detail"], str)

    not_json = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token, "content-type": "application/json"},
        content=b"{not json",
    )
    assert not_json.status_code == 422 and isinstance(not_json.json()["detail"], str)
    assert all(isinstance(problem, str) for problem in not_json.json()["problems"])


def test_operation_list_filters_by_kind_across_environments_and_finds_operations_of_no_environment(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    rows = (
        ("inventory_refresh", "sandbox", "Refresh inventory: sandbox"),
        ("inventory_refresh", "production", "Refresh inventory: production"),
        ("environment_import", None, "Import environment: staging"),
    )
    with session_factory() as session:
        for index, (kind, environment_slug, title) in enumerate(rows):
            session.add(
                Operation(
                    operation_kind=kind,
                    state="FAILED",
                    environment_slug=environment_slug,
                    plan={},
                    idempotency_key=f"kind-filter-{index}",
                    created_by_operator_name="phil",
                    title=title,
                )
            )
        session.commit()

    def titles(**params: str) -> set[str]:
        response = client.get("/api/v1/operations", params=params)
        assert response.status_code == 200, response.text
        return {operation["title"] for operation in response.json()["operations"]}

    assert titles() == {title for _, _, title in rows}, "no environment named: every environment and none"
    assert titles(kind="environment_import") == {"Import environment: staging"}
    assert titles(kind="environment_import", environment_slug="sandbox") == {"Import environment: staging"}
    assert titles(kind="inventory_refresh") == {"Refresh inventory: sandbox", "Refresh inventory: production"}
    assert titles(environment_slug="sandbox") == {"Refresh inventory: sandbox"}
    assert titles(operation_kind="inventory_refresh", environment_slug="production") == {
        "Refresh inventory: production"
    }
    conflicting = client.get("/api/v1/operations", params={"kind": "a", "operation_kind": "b"})
    assert conflicting.status_code == 422 and isinstance(conflicting.json()["detail"], str)


def test_approving_an_operation_at_its_second_stage_records_the_second_approval_beside_the_first(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from datetime import UTC, datetime

    first_approved_at_utc = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    with session_factory() as session:
        operation = _add_operation(
            session,
            "major_upgrade",
            "AWAITING_APPROVAL",
            "second-stage",
            approved_by_operator_name="sam",
            approved_at_utc=first_approved_at_utc,
            approval_required_text="demo-orders",
        )
        operation.plan = {"options": {}, "approval_stage": "switchover"}
        session.commit()
        operation_id = operation.id
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    approved = client.post(
        f"/api/v1/operations/{operation_id}/approve",
        headers={"X-CSRF-Token": token},
        json={"approval_stage": "switchover", "confirmation_text": "demo-orders"},
    )
    assert approved.status_code == 200 and approved.json()["state"] == "QUEUED"
    with session_factory() as session:
        stored = session.get(Operation, operation_id)
        assert (stored.approved_by_operator_name, stored.approved_at_utc) == ("sam", first_approved_at_utc)
        assert stored.plan["switchover_approved_by"] == operator_settings.operator_name
        assert stored.plan["switchover_approved_at_utc"]


def _paused_at_the_switchover(session_factory, key: str):
    """A major upgrade paused at its second stage: first approval given, the identifier to type."""
    from datetime import UTC, datetime

    with session_factory() as session:
        operation = _add_operation(
            session,
            "major_upgrade",
            "AWAITING_APPROVAL",
            key,
            approved_by_operator_name="sam",
            approved_at_utc=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
            approval_required_text="demo-orders",
        )
        operation.plan = {"options": {}, "approval_stage": "switchover"}
        session.commit()
        return operation.id


def _approve(client, token: str, operation_id, body: dict | None):
    return client.post(
        f"/api/v1/operations/{operation_id}/approve",
        headers={"X-CSRF-Token": token},
        **({} if body is None else {"json": body}),
    )


def test_a_first_stage_approval_sent_while_the_operation_waits_at_the_switchover_is_refused(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    operation_id = _paused_at_the_switchover(session_factory, "stale-stage")
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    for body in (
        {"approval_stage": "create_green", "confirmation_text": "demo-orders"},
        {"confirmation_text": "demo-orders"},
        None,
    ):
        refused = _approve(client, token, operation_id, body)
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["diagnostic_kind"] == "approval_stage_mismatch"
        assert "switchover" in detail["detail"] and detail["fix_suggestion"]
    stale = _approve(
        client, token, operation_id, {"approval_stage": "create_green", "confirmation_text": "demo-orders"}
    )
    assert "create_green" in stale.json()["detail"]["detail"]
    with session_factory() as session:
        stored = session.get(Operation, operation_id)
        assert stored.state == "AWAITING_APPROVAL"
        assert "switchover_approved_at_utc" not in stored.plan


def test_an_approval_with_the_wrong_typed_identifier_is_refused_without_echoing_it(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    operation_id = _paused_at_the_switchover(session_factory, "wrong-text")
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    for typed in ("demo-order", None):
        refused = _approve(
            client, token, operation_id, {"approval_stage": "switchover", "confirmation_text": typed}
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["diagnostic_kind"] == "confirmation_text_mismatch"
        assert "demo-orders" not in detail["detail"] and "demo-orders" not in detail["fix_suggestion"]
        assert "the identifier shown on the review" in detail["detail"]
    with session_factory() as session:
        assert session.get(Operation, operation_id).state == "AWAITING_APPROVAL"


def test_an_approval_with_the_shown_stage_and_the_typed_identifier_requeues_the_operation(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    operation_id = _paused_at_the_switchover(session_factory, "right-stage")
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    approved = _approve(
        client, token, operation_id, {"approval_stage": "switchover", "confirmation_text": "demo-orders"}
    )
    assert approved.status_code == 200 and approved.json()["state"] == "QUEUED"
    with session_factory() as session:
        assert session.get(Operation, operation_id).plan["switchover_approved_at_utc"]
