import pytest
from fastapi.testclient import TestClient

from rdscommander.catalogue import catalogue as catalogue_module
from rdscommander.catalogue.catalogue import always
from rdscommander.catalogue.definitions import OperationDefinition
from rdscommander.catalogue.options import NoOptions
from rdscommander.config.models import EnvironmentTarget
from rdscommander.control_db.models import InventoryDbSnapshot
from rdscommander.main import create_application
from rdscommander.resources.identity import encode_resource_id
from tests.terraform.conftest import arn_for


def test_catalogue_route_lists_definitions_with_schemas(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    body = client.get("/api/v1/catalogue").json()
    kinds = {d["kind"] for d in body["definitions"]}
    assert {"inventory_refresh", "terraform_adoption"} <= kinds
    refresh = next(d for d in body["definitions"] if d["kind"] == "inventory_refresh")
    assert refresh["options_schema"]["type"] == "object" and refresh["group"] == "Discovery and adoption"


def test_generic_create_validates_kind_mode_applicability_and_options(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": token}
    assert (
        client.post(
            "/api/v1/operations",
            headers=headers,
            json={"kind": "nope", "environment_slug": "sandbox", "options": {}, "idempotency_key": "x"},
        ).status_code
        == 404
    )
    bad_options = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "terraform_adoption",
            "environment_slug": "sandbox",
            "options": {"target_key": "sandbox/000000000000/eu-west-2"},
            "idempotency_key": "y",
        },
    )
    assert bad_options.status_code == 422 and any(
        "resource_arns" in p for p in bad_options.json()["problems"]
    )
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    created = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "terraform_adoption",
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id(orders),
            "options": {
                "target_key": "sandbox/000000000000/eu-west-2",
                "resource_arns": [arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")],
            },
            "idempotency_key": "z",
        },
    )
    assert created.status_code == 202
    detail = client.get(f"/api/v1/operations/{created.json()['operation_id']}").json()
    assert detail["title"].startswith("Adopt resources") and detail["resource_arn"] == orders


def test_generic_create_rejects_unknown_targets_foreign_targets_and_unknown_resources(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": token}
    group = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    unknown_target = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "terraform_adoption",
            "environment_slug": "sandbox",
            "options": {"target_key": "sandbox/999999999999/eu-west-2", "resource_arns": [group]},
            "idempotency_key": "t1",
        },
    )
    assert unknown_target.status_code == 404
    foreign_target = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "terraform_adoption",
            "environment_slug": "sandbox",
            "options": {"target_key": "production/123456789012/eu-west-2", "resource_arns": [group]},
            "idempotency_key": "t2",
        },
    )
    assert foreign_target.status_code == 422 and "sandbox" in foreign_target.json()["detail"]
    unknown_resource = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "inventory_refresh",
            "environment_slug": "sandbox",
            "resource_id": "bm90LWFuLWFybg",
            "options": {},
            "idempotency_key": "t3",
        },
    )
    assert unknown_resource.status_code == 404
    missing_environment = client.post(
        "/api/v1/operations",
        headers=headers,
        json={"kind": "inventory_refresh", "options": {}, "idempotency_key": "t4"},
    )
    assert missing_environment.status_code == 422


def test_generic_create_refuses_terraform_only_operations_in_direct_mode(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    direct_config = console_config.model_copy(deep=True)
    direct_config.environments[0].change_management = "direct"
    app = create_application(
        settings=operator_settings, console_config=direct_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    refused = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "terraform_adoption",
            "environment_slug": "sandbox",
            "options": {"target_key": "sandbox/000000000000/eu-west-2", "resource_arns": ["arn:aws:rds:x"]},
            "idempotency_key": "d1",
        },
    )
    assert refused.status_code == 422 and "direct mode" in refused.json()["detail"]


def test_old_refresh_route_still_works_through_the_catalogue(
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
        json={"idempotency_key": "r1"},
    )
    assert created.status_code == 202
    detail = client.get(f"/api/v1/operations/{created.json()['operation_id']}").json()
    assert detail["title"] == "Refresh inventory: sandbox"
    unknown = client.post(
        "/api/v1/environments/nope/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "r2"},
    )
    assert unknown.status_code == 404


def test_generic_create_refuses_a_resource_in_a_different_target_than_the_options(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    two_target_config = console_config.model_copy(deep=True)
    two_target_config.environments[0].targets.append(
        EnvironmentTarget(aws_account_id="000000000000", aws_region="eu-west-1", simulated=True)
    )
    app = create_application(
        settings=operator_settings, console_config=two_target_config, session_factory=session_factory
    )
    client = TestClient(app)
    headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operations_before = client.get("/api/v1/operations").json()["operations"]
    refused = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "terraform_adoption",
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id(orders),
            "options": {
                "target_key": "sandbox/000000000000/eu-west-1",
                "resource_arns": [arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")],
            },
            "idempotency_key": "mismatch-1",
        },
    )
    assert refused.status_code == 422
    problems = " ".join(refused.json()["problems"])
    assert "sandbox/000000000000/eu-west-1" in problems and "sandbox/000000000000/eu-west-2" in problems
    assert client.get("/api/v1/operations").json()["operations"] == operations_before


def test_generic_create_refuses_a_resource_kind_the_definition_does_not_apply_to(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    with session_factory() as session:
        session.add(
            InventoryDbSnapshot(
                snapshot_id=sandbox_snapshot.id,
                snapshot_scope="instance",
                db_snapshot_identifier="demo-orders-manual",
                db_snapshot_arn="arn:aws:rds:eu-west-2:000000000000:snapshot:demo-orders-manual",
                source_identifier="demo-orders",
                snapshot_type="manual",
                status="available",
                engine_version="16.3",
                allocated_storage_gb=20,
                created_at_source_utc=None,
                tags={},
                raw_description={},
            )
        )
        session.commit()
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operations_before = client.get("/api/v1/operations").json()["operations"]
    refresh_on_instance = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "inventory_refresh",
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id(orders),
            "options": {},
            "idempotency_key": "kind-1",
        },
    )
    assert refresh_on_instance.status_code == 422
    assert "does not apply to instance resources" in refresh_on_instance.json()["detail"]
    adoption_on_snapshot = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "terraform_adoption",
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id(
                "arn:aws:rds:eu-west-2:000000000000:snapshot:demo-orders-manual"
            ),
            "options": {
                "target_key": "sandbox/000000000000/eu-west-2",
                "resource_arns": [arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")],
            },
            "idempotency_key": "kind-2",
        },
    )
    assert adoption_on_snapshot.status_code == 422
    assert "does not apply to snapshot resources" in adoption_on_snapshot.json()["detail"]
    assert client.get("/api/v1/operations").json()["operations"] == operations_before


def test_generic_create_requires_a_resource_for_a_definition_that_does_not_apply_to_environments(
    session_factory,
    operator_settings,
    console_config,
    sandbox_snapshot,
    moto_aws,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    instance_only_definition = OperationDefinition(
        kind="instance_only_probe",
        title="Probe instance",
        group="Probes",
        summary="Exists only to prove applies_to is enforced.",
        applies_to=frozenset({catalogue_module.ResourceKind.INSTANCE}),
        options_model=NoOptions,
        applicability=always,
    )
    monkeypatch.setattr(
        catalogue_module,
        "OPERATION_DEFINITIONS",
        (*catalogue_module.OPERATION_DEFINITIONS, instance_only_definition),
    )
    client = TestClient(app)
    headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}
    without_resource = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "instance_only_probe",
            "environment_slug": "sandbox",
            "options": {},
            "idempotency_key": "env-1",
        },
    )
    assert (
        without_resource.status_code == 422 and "resource_id: required" in without_resource.json()["problems"]
    )
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with_instance = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "instance_only_probe",
            "resource_id": encode_resource_id(orders),
            "options": {},
            "idempotency_key": "env-2",
        },
    )
    assert with_instance.status_code == 202


def test_the_idempotency_key_is_namespaced_by_environment_and_a_key_of_any_allowed_length_is_stored(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    from rdscommander.control_db.models import Operation

    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]

    def refresh(environment_slug: str, idempotency_key: str):  # noqa: ANN202
        return client.post(
            "/api/v1/operations",
            headers={"X-CSRF-Token": token},
            json={
                "kind": "inventory_refresh",
                "environment_slug": environment_slug,
                "options": {},
                "idempotency_key": idempotency_key,
            },
        )

    sandbox = refresh("sandbox", "same-key")
    production = refresh("production", "same-key")
    assert sandbox.status_code == production.status_code == 202
    assert sandbox.json()["operation_id"] != production.json()["operation_id"]
    assert refresh("sandbox", "same-key").json()["operation_id"] == sandbox.json()["operation_id"]
    longest_allowed_key = "k" * 200
    first = refresh("sandbox", longest_allowed_key)
    assert first.status_code == 202, first.text
    assert refresh("sandbox", longest_allowed_key).json()["operation_id"] == first.json()["operation_id"]
    assert refresh("production", longest_allowed_key).json()["operation_id"] != first.json()["operation_id"]
    with session_factory() as session:
        stored_keys = [operation.idempotency_key for operation in session.query(Operation)]
    assert len(stored_keys) == 4 and all(len(key) <= 200 for key in stored_keys)


def test_catalogue_says_which_definitions_pause_for_approval_and_which_option_decides_it(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    definitions = {d["kind"]: d for d in client.get("/api/v1/catalogue").json()["definitions"]}
    refresh = definitions["inventory_refresh"]
    environment_import = definitions["environment_import"]
    drift_resolve = definitions["drift_resolve"]
    parameter_edit = definitions["parameter_group_edit"]
    assert refresh["pauses_for_approval"] is False and refresh["pauses_for_approval_when"] is None
    assert environment_import["pauses_for_approval"] is False
    assert environment_import["pauses_for_approval_when"] is None
    assert drift_resolve["pauses_for_approval"] is True
    assert drift_resolve["pauses_for_approval_when"] == {"accept": False, "restore": True}
    assert (
        parameter_edit["pauses_for_approval"] is True and parameter_edit["pauses_for_approval_when"] is None
    )
