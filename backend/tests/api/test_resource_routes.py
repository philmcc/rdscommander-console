import uuid
from datetime import UTC, datetime

import boto3
from fastapi.testclient import TestClient

from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.inventory.snapshot_writer import DiscoveredEstate, write_snapshot
from rdscommander.main import create_application
from rdscommander.resources.identity import encode_resource_id
from tests.terraform.conftest import TARGET_KEY, arn_for


def test_resource_route_returns_record_actions_and_recent_operations(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    body = client.get(f"/api/v1/resources/{encode_resource_id(orders)}").json()
    assert body["record"]["identifier"] == "demo-orders" and body["record"]["kind"] == "instance"
    assert any(a["kind"] == "terraform_adoption" and a["applicable"] for a in body["actions"])
    assert body["recent_operations"] == [] and body["change_events"] == [] and body["drift"] == []
    assert client.get("/api/v1/resources/bm90LWFuLWFybg").status_code == 404
    assert client.get("/api/v1/resources/!!!").status_code == 404
    listing = client.get("/api/v1/environments/sandbox/resources", params={"kind": "instance"}).json()
    assert [r["identifier"] for r in listing["resources"]] == [
        "demo-aurora-reader",
        "demo-aurora-writer",
        "demo-orders",
        "demo-orders-replica",
    ]
    assert all(r["resource_id"] == encode_resource_id(r["arn"]) for r in listing["resources"])
    assert body["record"]["resource_id"] == encode_resource_id(orders)
    assert client.get("/api/v1/environments/nope/resources").status_code == 404


def test_resource_page_lists_operations_created_against_the_resource(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    resource_id = encode_resource_id(orders)
    created = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "terraform_adoption",
            "environment_slug": "sandbox",
            "resource_id": resource_id,
            "options": {
                "target_key": "sandbox/000000000000/eu-west-2",
                "resource_arns": [arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")],
            },
            "idempotency_key": "page-1",
        },
    )
    assert created.status_code == 202
    page = client.get(f"/api/v1/resources/{resource_id}").json()
    assert [o["id"] for o in page["recent_operations"]] == [created.json()["operation_id"]]
    assert page["recent_operations"][0]["resource_arn"] == orders
    assert page["recent_operations"][0]["title"] == "Adopt resources: demo-orders"


def test_resource_page_lists_its_change_events_newest_first(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders",
        DeletionProtection=True,
        BackupRetentionPeriod=7,
        ApplyImmediately=True,
    )
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "page-changes"},
    )
    app.state.runner.run_once()
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    page = client.get(f"/api/v1/resources/{encode_resource_id(orders)}").json()
    assert {e["attribute_path"] for e in page["change_events"]} == {
        "deletion_protection",
        "backup_retention_period",
    }
    assert all(
        e["resource_arn"] == orders and e["explanation"] == "outside_console" for e in page["change_events"]
    )
    event_ids = [e["id"] for e in page["change_events"]]
    assert event_ids == sorted(event_ids, reverse=True)
    unchanged = arn_for(session_factory, sandbox_snapshot, "demo-aurora-writer")
    assert client.get(f"/api/v1/resources/{encode_resource_id(unchanged)}").json()["change_events"] == []


def _application(session_factory, operator_settings, console_config):
    return create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )


def test_resource_page_lists_open_drift_and_offers_to_resolve_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group, _refresh_and_detect, _set_work_mem

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "page")
    client = TestClient(_application(session_factory, operator_settings, console_config))
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    page = client.get(f"/api/v1/resources/{encode_resource_id(group_arn)}").json()
    assert page["record"]["facts"]["has_open_drift"] is True
    [finding] = page["drift"]
    assert finding["finding_kind"] == "record" and finding["detected_at_utc"]
    assert [(a["attribute"], a["recorded"], a["live"]) for a in finding["attributes"]] == [
        ("parameters.work_mem", "16384", "65536")
    ]
    assert all(a["address"] for a in finding["attributes"])
    resolve = next(a for a in page["actions"] if a["kind"] == "drift_resolve")
    assert resolve["applicable"] is True
    orders = client.get(
        f"/api/v1/resources/{encode_resource_id(arn_for(session_factory, sandbox_snapshot, 'demo-orders'))}"
    ).json()
    assert orders["record"]["facts"]["has_open_drift"] is False and orders["drift"] == []
    not_offered = next(a for a in orders["actions"] if a["kind"] == "drift_resolve")
    assert not_offered["applicable"] is False and not_offered["reason"] == "no open drift on this resource"
    listing = client.get("/api/v1/environments/sandbox/resources", params={"kind": "parameter_group"}).json()
    assert {r["identifier"]: r["facts"]["has_open_drift"] for r in listing["resources"]}[
        "demo-postgres16-tuned"
    ] is True


def test_drift_resolution_is_created_through_the_generic_route_only_while_drift_is_open(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group, _refresh_and_detect, _set_work_mem

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    client = TestClient(_application(session_factory, operator_settings, console_config))
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")

    def request(key: str):
        return client.post(
            "/api/v1/operations",
            headers={"X-CSRF-Token": token},
            json={
                "kind": "drift_resolve",
                "resource_id": encode_resource_id(group_arn),
                "options": {"resource_arn": group_arn, "choice": "accept"},
                "idempotency_key": key,
            },
        )

    refused = request("resolve-none")
    assert refused.status_code == 422 and "no open drift" in refused.json()["detail"]
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "generic")
    created = request("resolve-open")
    assert created.status_code == 202
    operation = service.get(uuid.UUID(created.json()["operation_id"]))
    assert operation.operation_kind == "drift_resolve"
    assert (operation.target_key, operation.resource_arn) == (TARGET_KEY, group_arn)
    assert operation.title == "Resolve drift: demo-postgres16-tuned"
    assert operation.plan["options"] == {"resource_arn": group_arn, "choice": "accept"}
    assert operation.plan["address"] == "aws_db_parameter_group.demo_postgres16_tuned"
    mismatched = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "drift_resolve",
            "resource_id": encode_resource_id(group_arn),
            "options": {"resource_arn": "arn:aws:rds:eu-west-2:123456789012:pg:other", "choice": "accept"},
            "idempotency_key": "resolve-mismatch",
        },
    )
    assert mismatched.status_code == 422 and "selected resource" in mismatched.json()["detail"]
    invalid = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "drift_resolve",
            "resource_id": encode_resource_id(group_arn),
            "options": {"resource_arn": group_arn, "choice": "ignore"},
            "idempotency_key": "resolve-bad-choice",
        },
    )
    assert invalid.status_code == 422


def test_resource_list_narrows_to_the_resources_a_task_applies_to(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group, _refresh_and_detect, _set_work_mem

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    client = TestClient(_application(session_factory, operator_settings, console_config))

    def listed(**params: str) -> list[dict]:
        response = client.get("/api/v1/environments/sandbox/resources", params=params)
        assert response.status_code == 200
        return response.json()["resources"]

    # Drift resolution: only what has an open finding.
    assert listed(applicable_to="drift_resolve") == []
    _set_work_mem("65536")
    # Adoption tags the group, which the next inventory refresh reads as ownership `adopted`.
    boto3.client("rds", region_name="eu-west-2").add_tags_to_resource(
        ResourceName=arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned"),
        Tags=[{"Key": "rdscommander:managed", "Value": "true"}],
    )
    _refresh_and_detect(runner, service, "applicable")
    assert [r["identifier"] for r in listed(applicable_to="drift_resolve")] == ["demo-postgres16-tuned"]
    assert listed(kind="instance", applicable_to="drift_resolve") == []

    # Adoption: not the AWS default groups, and not the group adopted above; snapshots are not adoptable.
    all_groups = listed(kind="parameter_group")
    default_groups = {r["identifier"] for r in all_groups if r["facts"].get("is_default")}
    adopted_groups = {r["identifier"] for r in all_groups if r["ownership"] == "adopted"}
    assert default_groups and adopted_groups == {"demo-postgres16-tuned"}
    adoptable = listed(applicable_to="terraform_adoption")
    adoptable_groups = {r["identifier"] for r in adoptable if r["kind"] == "parameter_group"}
    assert adoptable_groups and adoptable_groups.isdisjoint(default_groups | adopted_groups)
    assert adoptable_groups == {
        r["identifier"] for r in listed(kind="parameter_group", applicable_to="terraform_adoption")
    }
    assert {r["kind"] for r in adoptable} <= {"instance", "cluster", "parameter_group"}
    assert "demo-orders" in {r["identifier"] for r in adoptable}

    # A task applies to nothing it was not defined for, and an unknown task is refused.
    assert listed(kind="snapshot", applicable_to="terraform_adoption") == []
    unknown = client.get("/api/v1/environments/sandbox/resources", params={"applicable_to": "no_such_task"})
    assert unknown.status_code == 404 and "no_such_task" in unknown.json()["detail"]


def test_resource_page_lists_the_pending_maintenance_of_the_resource_in_its_latest_snapshot(
    session_factory, operator_settings, console_config
) -> None:
    sync_configuration_to_control_db(session_factory, console_config)
    target = console_config.target(TARGET_KEY)
    orders_arn = "arn:aws:rds:eu-west-2:000000000000:db:demo-orders"
    billing_arn = "arn:aws:rds:eu-west-2:000000000000:db:demo-billing"

    def instance(identifier: str, arn: str) -> dict:
        return {"DBInstanceIdentifier": identifier, "DBInstanceArn": arn, "Engine": "postgres"}

    stale_estate = DiscoveredEstate(
        instances=[instance("demo-orders", orders_arn)],
        pending_maintenance=[
            {
                "ResourceIdentifier": orders_arn,
                "PendingMaintenanceActionDetails": [
                    {"Action": "ca-certificate-rotation", "Description": "old"}
                ],
            }
        ],
    )
    current_estate = DiscoveredEstate(
        instances=[instance("demo-orders", orders_arn), instance("demo-billing", billing_arn)],
        pending_maintenance=[
            {
                "ResourceIdentifier": orders_arn,
                "PendingMaintenanceActionDetails": [
                    {
                        "Action": "system-update",
                        "Description": "New OS patch",
                        "AutoAppliedAfterDate": datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
                        "CurrentApplyDate": datetime(2026, 10, 20, 2, 0, tzinfo=UTC),
                        "OptInStatus": "next-maintenance",
                    },
                    {"Action": "db-upgrade", "Description": "Minor version upgrade"},
                ],
            },
            {
                "ResourceIdentifier": billing_arn,
                "PendingMaintenanceActionDetails": [
                    {"Action": "system-update", "Description": "Billing patch"}
                ],
            },
        ],
    )
    with session_factory() as session:
        write_snapshot(session, target, None, stale_estate)
        write_snapshot(session, target, None, current_estate)
        session.commit()
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    page = client.get(f"/api/v1/resources/{encode_resource_id(orders_arn)}").json()
    by_action = {entry["action"]: entry for entry in page["pending_maintenance"]}
    assert list(by_action) == ["db-upgrade", "system-update"]
    assert by_action["system-update"] == {
        "action": "system-update",
        "description": "New OS patch",
        "auto_applied_after_utc": "2026-11-01T03:00:00Z",
        "current_apply_utc": "2026-10-20T02:00:00Z",
        "opt_in_status": "next-maintenance",
    }
    assert by_action["db-upgrade"]["auto_applied_after_utc"] is None
    assert by_action["db-upgrade"]["current_apply_utc"] is None
    assert by_action["db-upgrade"]["opt_in_status"] is None
    billing = client.get(f"/api/v1/resources/{encode_resource_id(billing_arn)}").json()
    assert [entry["description"] for entry in billing["pending_maintenance"]] == ["Billing patch"]
