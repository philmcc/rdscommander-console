from fastapi.testclient import TestClient

from rdscommander.main import create_application
from tests.terraform.conftest import arn_for


def test_adoption_route_validates_and_creates(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    rejected = client.post(
        "/api/v1/terraform/adoptions",
        headers={"X-CSRF-Token": token},
        json={
            "target_key": "sandbox/000000000000/eu-west-2",
            "resource_arns": [orders],
            "idempotency_key": "a1",
        },
    )
    assert rejected.status_code == 422 and any(
        "demo-postgres16-tuned" in p for p in rejected.json()["problems"]
    )
    group = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    accepted = client.post(
        "/api/v1/terraform/adoptions",
        headers={"X-CSRF-Token": token},
        json={
            "target_key": "sandbox/000000000000/eu-west-2",
            "resource_arns": [group],
            "idempotency_key": "a2",
        },
    )
    assert accepted.status_code == 202
    workspaces = client.get("/api/v1/terraform/workspaces").json()["workspaces"]
    assert workspaces == [] or workspaces[0]["target_key"] == "sandbox/000000000000/eu-west-2"
