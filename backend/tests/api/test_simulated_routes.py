from fastapi.testclient import TestClient

from rdscommander.main import create_application


def test_reset_route_seeds_every_simulated_target(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    response = client.post("/api/v1/environments/sandbox/simulated/reset", headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    report = response.json()["reports"]["sandbox/000000000000/eu-west-2"]
    assert "instance:demo-orders" in report["created"]
    assert (
        client.post(
            "/api/v1/environments/production/simulated/reset", headers={"X-CSRF-Token": token}
        ).status_code
        == 422
    )
