"""The lifecycle routes: orderable classes and a disruptive change created through the catalogue."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from rdscommander.api import lifecycle_routes
from rdscommander.main import create_application
from rdscommander.resources.identity import encode_resource_id
from tests.terraform.conftest import arn_for


def _client(session_factory, operator_settings, console_config) -> TestClient:
    return TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )


def test_instance_classes_route_asks_aws_for_the_engine_and_version_of_the_resource(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    asked: list[tuple[str, str]] = []

    def fake_orderable_instance_classes(rds, engine: str, engine_version: str) -> list[str]:
        asked.append((engine, engine_version))
        return ["db.m6g.large", "db.m6g.xlarge"]

    monkeypatch.setattr(lifecycle_routes, "orderable_instance_classes", fake_orderable_instance_classes)
    client = _client(session_factory, operator_settings, console_config)
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    response = client.get(
        "/api/v1/lifecycle/instance-classes",
        params={"environment_slug": "sandbox", "resource_id": encode_resource_id(orders)},
    )
    assert response.status_code == 200
    assert response.json() == {"classes": ["db.m6g.large", "db.m6g.xlarge"]}
    assert asked == [("postgres", "16.4")]


def test_instance_classes_route_answers_an_empty_list_when_the_simulator_cannot_say(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    """moto implements describe_orderable_db_instance_options for Aurora only, and has no 16.x options."""
    client = _client(session_factory, operator_settings, console_config)
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    response = client.get(
        "/api/v1/lifecycle/instance-classes",
        params={"environment_slug": "sandbox", "resource_id": encode_resource_id(orders)},
    )
    assert response.status_code == 200
    assert response.json()["classes"] == []


def test_instance_classes_route_answers_404_for_an_unknown_resource(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client = _client(session_factory, operator_settings, console_config)
    response = client.get(
        "/api/v1/lifecycle/instance-classes",
        params={
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id("arn:aws:rds:eu-west-2:000000000000:db:nope"),
        },
    )
    assert response.status_code == 404


def test_creating_an_instance_class_change_requires_typing_the_identifier(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    direct_config = console_config.model_copy(deep=True)
    direct_config.environment("sandbox").change_management = "direct"
    client = _client(session_factory, operator_settings, direct_config)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    created = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "instance_class_change",
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id(orders),
            "options": {"instance_class": "db.m6g.xlarge"},
            "idempotency_key": "class-1",
        },
    )
    assert created.status_code == 202, created.text
    detail = client.get(f"/api/v1/operations/{created.json()['operation_id']}").json()
    assert detail["approval_required_text"] == "demo-orders"
    assert detail["title"] == "Change instance class: demo-orders"


def _iso_utc(value: str) -> datetime:
    assert value.endswith("Z"), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_restore_window_route_answers_the_earliest_and_latest_restorable_times(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client = _client(session_factory, operator_settings, console_config)
    for identifier in ("demo-orders", "demo-aurora"):
        arn = arn_for(session_factory, sandbox_snapshot, identifier)
        response = client.get(f"/api/v1/lifecycle/restore-window/{encode_resource_id(arn)}")
        assert response.status_code == 200, response.text
        window = response.json()
        earliest, latest = _iso_utc(window["earliest_utc"]), _iso_utc(window["latest_restorable_utc"])
        assert earliest <= latest and window["unknown_reason"] is None, identifier


def test_restore_window_route_says_why_when_the_inventory_has_no_restorable_time(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import InventoryDbInstance

    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        row = session.query(InventoryDbInstance).filter_by(db_instance_arn=orders).one()
        row.raw_description = {k: v for k, v in row.raw_description.items() if k != "LatestRestorableTime"}
        session.commit()
    client = _client(session_factory, operator_settings, console_config)
    window = client.get(f"/api/v1/lifecycle/restore-window/{encode_resource_id(orders)}").json()
    assert (window["earliest_utc"], window["latest_restorable_utc"]) == (None, None)
    assert "latest restorable time" in window["unknown_reason"]


def test_restore_window_route_refuses_a_parameter_group_and_an_unknown_resource(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client = _client(session_factory, operator_settings, console_config)
    group = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    assert client.get(f"/api/v1/lifecycle/restore-window/{encode_resource_id(group)}").status_code == 422
    unknown = encode_resource_id("arn:aws:rds:eu-west-2:000000000000:db:nope")
    assert client.get(f"/api/v1/lifecycle/restore-window/{unknown}").status_code == 404


def test_instance_classes_route_asks_for_a_snapshot_s_engine_and_version(
    session_factory, operator_settings, console_config, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    import boto3

    from tests.lifecycle.test_creates import _refresh

    snapshot_arn = boto3.client("rds", region_name="eu-west-2").create_db_snapshot(
        DBSnapshotIdentifier="demo-orders-classes", DBInstanceIdentifier="demo-orders"
    )["DBSnapshot"]["DBSnapshotArn"]
    _refresh(session_factory, operator_settings, config_store, "classes")
    asked: list[tuple[str, str]] = []

    def fake_orderable_instance_classes(rds, engine: str, engine_version: str) -> list[str]:
        asked.append((engine, engine_version))
        return ["db.m6g.large"]

    monkeypatch.setattr(lifecycle_routes, "orderable_instance_classes", fake_orderable_instance_classes)
    client = _client(session_factory, operator_settings, console_config)
    response = client.get(
        "/api/v1/lifecycle/instance-classes",
        params={"environment_slug": "sandbox", "resource_id": encode_resource_id(snapshot_arn)},
    )
    assert response.status_code == 200, response.text
    assert asked == [("postgres", "16.4")]


def test_instance_classes_route_answers_for_an_engine_and_version_a_create_chose(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    asked: list[tuple[str, str]] = []

    def fake_orderable_instance_classes(rds, engine: str, engine_version: str) -> list[str]:
        asked.append((engine, engine_version))
        return ["db.r6g.large"]

    monkeypatch.setattr(lifecycle_routes, "orderable_instance_classes", fake_orderable_instance_classes)
    client = _client(session_factory, operator_settings, console_config)
    response = client.get(
        "/api/v1/lifecycle/instance-classes",
        params={
            "environment_slug": "sandbox",
            "engine": "aurora-postgresql",
            "engine_version": "16.4",
            "target_key": "sandbox/000000000000/eu-west-2",
        },
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"classes": ["db.r6g.large"]}
    assert asked == [("aurora-postgresql", "16.4")]
    without_either = client.get("/api/v1/lifecycle/instance-classes", params={"environment_slug": "sandbox"})
    assert without_either.status_code == 422
    other_target = client.get(
        "/api/v1/lifecycle/instance-classes",
        params={
            "environment_slug": "sandbox",
            "engine": "postgres",
            "engine_version": "16.4",
            "target_key": "production/123456789012/eu-west-2",
        },
    )
    assert other_target.status_code == 404


def test_the_engine_version_picker_answers_without_a_resource(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client = _client(session_factory, operator_settings, console_config)
    for engine in ("postgres", "aurora-postgresql"):
        response = client.get(
            "/api/v1/parameters/engine-versions", params={"environment_slug": "sandbox", "engine": engine}
        )
        assert response.status_code == 200, response.json()
        assert "16.4" in [version["engine_version"] for version in response.json()["versions"]], engine
