import boto3
from fastapi.testclient import TestClient

from rdscommander.main import create_application
from rdscommander.resources.identity import encode_resource_id
from tests.terraform.conftest import arn_for


def test_history_search_and_export(session_factory, operator_settings, console_config, moto_aws) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "h1"},
    )
    app.state.runner.run_once()
    found = client.get(
        "/api/v1/history", params={"record_kind": "aws_api", "text": "DescribeDBInstances"}
    ).json()
    assert found["records"] and all(r["api_name"] == "rds:DescribeDBInstances" for r in found["records"])
    exported = client.get("/api/v1/history/export", params={"record_kind": "aws_api"}).json()
    assert isinstance(exported, list) and len(exported) >= len(found["records"])


def test_changes_view_lists_and_reviews_events(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", DeletionProtection=True, ApplyImmediately=True
    )
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "cv-1"},
    )
    app.state.runner.run_once()
    page = client.get(
        "/api/v1/history/changes", params={"environment_slug": "sandbox", "explanation": "outside_console"}
    ).json()
    [event] = [e for e in page["events"] if e["attribute_path"] == "deletion_protection"]
    assert event["value_before"] is False and event["value_after"] is True
    reviewed = client.post(
        f"/api/v1/history/changes/{event['id']}/review", headers={"X-CSRF-Token": token}
    ).json()
    assert reviewed["reviewed_by_operator_name"] == "test-operator"


def test_changes_view_filters_by_resource_review_state_and_environment(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders",
        DeletionProtection=True,
        BackupRetentionPeriod=7,
        ApplyImmediately=True,
    )
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-aurora-writer", AutoMinorVersionUpgrade=False, ApplyImmediately=True
    )
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "cv-2"},
    )
    app.state.runner.run_once()
    orders_resource_id = encode_resource_id(arn_for(session_factory, sandbox_snapshot, "demo-orders"))
    orders_events = client.get("/api/v1/history/changes", params={"resource_id": orders_resource_id}).json()
    assert {e["attribute_path"] for e in orders_events["events"]} == {
        "deletion_protection",
        "backup_retention_period",
    }
    assert all(e["resource_id"] == orders_resource_id for e in orders_events["events"])
    everything = client.get("/api/v1/history/changes").json()["events"]
    assert len(everything) == 3
    client.post(
        f"/api/v1/history/changes/{orders_events['events'][0]['id']}/review", headers={"X-CSRF-Token": token}
    )
    unreviewed = client.get("/api/v1/history/changes", params={"unreviewed_only": True}).json()["events"]
    assert len(unreviewed) == 2
    assert client.get("/api/v1/history/changes", params={"environment_slug": "production"}).json() == {
        "events": []
    }
    assert client.get("/api/v1/history/changes", params={"limit": 1}).json()["events"][0]["id"] == max(
        e["id"] for e in everything
    )


def test_changes_view_rejects_a_malformed_resource_id_and_review_of_unknown_event_is_not_found(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    assert client.get("/api/v1/history/changes", params={"resource_id": "!!!"}).status_code == 422
    missing = client.post("/api/v1/history/changes/424242/review", headers={"X-CSRF-Token": token})
    assert missing.status_code == 404
