from fastapi.testclient import TestClient

from rdscommander.main import create_application


def test_bootstrap_returns_csrf_token_and_identity(
    session_factory, operator_settings, console_config
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    response = client.get("/api/v1/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert len(body["csrf_token"]) >= 32
    assert body["operator_name"]
    assert [e["slug"] for e in body["environments"]] == ["sandbox", "production"]


def test_mutating_request_without_csrf_token_is_rejected(
    session_factory, operator_settings, console_config
) -> None:
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    path = "/api/v1/environments/sandbox/inventory/refresh"
    assert client.post(path, json={"idempotency_key": "csrf-probe"}).status_code == 403
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    with_token = client.post(path, json={"idempotency_key": "csrf-probe"}, headers={"X-CSRF-Token": token})
    assert with_token.status_code == 202


def test_requests_from_a_foreign_origin_are_rejected(
    session_factory, operator_settings, console_config
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    response = client.get("/api/v1/bootstrap", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_bootstrap_describes_each_environment_mode_and_the_operator_defaults(
    session_factory, operator_settings, console_config
) -> None:
    direct_config = console_config.model_copy(deep=True)
    sandbox = direct_config.environment("sandbox")
    sandbox.change_management = "direct"
    sandbox.allow_direct_changes_to_foreign_owned = True
    direct_config.environment("production").is_production = True
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=direct_config, session_factory=session_factory
        )
    )
    body = client.get("/api/v1/bootstrap").json()
    assert body["theme_default"] == "system"
    assert body["operator_name"] == operator_settings.operator_name
    by_slug = {environment["slug"]: environment for environment in body["environments"]}
    assert by_slug["sandbox"]["change_management"] == "direct"
    assert by_slug["sandbox"]["allow_direct_changes_to_foreign_owned"] is True
    assert by_slug["sandbox"]["is_production"] is False
    assert by_slug["production"]["change_management"] == "terraform"
    assert by_slug["production"]["allow_direct_changes_to_foreign_owned"] is False
    assert by_slug["production"]["is_production"] is True
