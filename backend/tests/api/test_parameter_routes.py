from fastapi.testclient import TestClient

from rdscommander.main import create_application


def _client(session_factory, operator_settings, console_config) -> TestClient:
    return TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )


def test_family_metadata_and_engine_versions_routes(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    family = client.get(
        "/api/v1/parameters/families/postgres16", params={"environment_slug": "sandbox"}
    ).json()
    assert family["source"] == "bundled" and family["parameters"]["work_mem"]["data_type"] == "integer"
    versions = client.get(
        "/api/v1/parameters/engine-versions",
        params={"environment_slug": "sandbox", "engine": "aurora-postgresql"},
    ).json()["versions"]
    assert any(v["parameter_group_family"] == "aurora-postgresql16" for v in versions)
    assert (
        client.get(
            "/api/v1/parameters/families/postgres9", params={"environment_slug": "sandbox"}
        ).status_code
        == 404
    )


def test_routes_answer_404_for_an_unknown_environment(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = _client(session_factory, operator_settings, console_config)
    family_response = client.get(
        "/api/v1/parameters/families/postgres16", params={"environment_slug": "nope"}
    )
    versions_response = client.get(
        "/api/v1/parameters/engine-versions", params={"environment_slug": "nope", "engine": "postgres"}
    )
    assert family_response.status_code == 404 and "nope" in family_response.json()["detail"]
    assert versions_response.status_code == 404


def test_routes_answer_502_with_the_diagnosis_when_a_real_account_cannot_be_reached(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = _client(session_factory, operator_settings, console_config)
    family_response = client.get(
        "/api/v1/parameters/families/postgres16", params={"environment_slug": "production"}
    )
    versions_response = client.get(
        "/api/v1/parameters/engine-versions", params={"environment_slug": "production", "engine": "postgres"}
    )
    for response in (family_response, versions_response):
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail["kind"] == "aws_profile_not_found" and "example-prod" in detail["fix_suggestion"]


def test_compare_route_and_parameter_group_page(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    comparison = client.get(
        "/api/v1/parameters/compare", params={"left": encode_resource_id(tuned), "against_defaults": "true"}
    ).json()
    assert any(r["name"] == "work_mem" and r["differs"] for r in comparison["rows"])
    page = client.get(f"/api/v1/resources/{encode_resource_id(tuned)}").json()
    assert page["record"]["facts"]["used_by"] == ["demo-orders", "demo-orders-replica"]
    assert page["record"]["facts"]["parameters"]["work_mem"]["value"] == "16384"
    kinds = {a["kind"]: a for a in page["actions"]}
    assert kinds["parameter_group_edit"]["applicable"] is False
    assert "adopt" in kinds["parameter_group_edit"]["reason"]
    assert kinds["parameter_group_delete"]["applicable"] is False
    assert "in use" in kinds["parameter_group_delete"]["reason"]


def test_compare_route_answers_404_for_unknown_resources_and_422_for_other_kinds(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    client = _client(session_factory, operator_settings, console_config)
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    missing = encode_resource_id(tuned.replace("demo-postgres16-tuned", "no-such-group"))
    assert client.get("/api/v1/parameters/compare", params={"left": "not-an-id"}).status_code == 404
    assert client.get("/api/v1/parameters/compare", params={"left": missing}).status_code == 404
    assert (
        client.get(
            "/api/v1/parameters/compare", params={"left": encode_resource_id(tuned), "right": missing}
        ).status_code
        == 404
    )
    not_a_group = client.get("/api/v1/parameters/compare", params={"left": encode_resource_id(orders)})
    assert not_a_group.status_code == 422 and "not a parameter group" in not_a_group.json()["detail"]


def test_parameter_group_actions_in_a_direct_environment_include_unused_deletion_and_reboot_rules(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    config = console_config.model_copy(deep=True)
    config.environment("sandbox").change_management = "direct"
    client = _client(session_factory, operator_settings, config)
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    group_page = client.get(f"/api/v1/resources/{encode_resource_id(tuned)}").json()
    kinds = {a["kind"]: a for a in group_page["actions"]}
    assert kinds["parameter_group_edit"]["applicable"] is True
    assert kinds["parameter_group_delete"]["applicable"] is False
    assert "in use by demo-orders, demo-orders-replica" in kinds["parameter_group_delete"]["reason"]
    assert kinds["parameter_group_delete"]["destructive"] is True
    assert kinds["reboot_instance"]["applicable"] is False
    assert "pending a reboot" in kinds["reboot_instance"]["reason"]
    instance_kinds = {
        a["kind"]: a for a in client.get(f"/api/v1/resources/{encode_resource_id(orders)}").json()["actions"]
    }
    assert instance_kinds["parameter_group_attach"]["applicable"] is True
    assert instance_kinds["parameter_group_attach"]["disruptive"] is True
    assert instance_kinds["reboot_instance"]["applicable"] is True


def test_create_operation_rejects_invalid_parameter_values_before_anything_runs(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    config = console_config.model_copy(deep=True)
    config.environment("sandbox").change_management = "direct"
    app = create_application(
        settings=operator_settings, console_config=config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    response = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_edit",
            "resource_id": encode_resource_id(tuned),
            "options": {
                "parameters": {"work_mem": "abc", "rds.rds_superuser_reserved_connections": "5"},
                "reset": [],
            },
            "idempotency_key": "bad-values",
        },
    )
    assert response.status_code == 422
    assert set(response.json()["problems"]) == {
        "work_mem must be an integer",
        "rds.rds_superuser_reserved_connections cannot be modified",
    }
    created = client.get("/api/v1/operations", params={"operation_kind": "parameter_group_edit"}).json()
    assert created["operations"] == []  # the fixture's own inventory refresh is the only operation


def test_create_a_parameter_group_operation_validates_values_and_names_the_group_in_the_title(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    config = console_config.model_copy(deep=True)
    config.environment("sandbox").change_management = "direct"
    client = TestClient(
        create_application(settings=operator_settings, console_config=config, session_factory=session_factory)
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    options = {
        "target_key": "sandbox/000000000000/eu-west-2",
        "name": "orders-pg16-v2",
        "scope": "instance",
        "family": "postgres16",
        "description": "orders v2",
        "source": "defaults",
        "parameters": {"work_mem": "abc"},
    }
    body = {
        "kind": "parameter_group_create",
        "environment_slug": "sandbox",
        "options": options,
        "idempotency_key": "create-1",
    }
    rejected = client.post("/api/v1/operations", headers={"X-CSRF-Token": token}, json=body)
    assert rejected.status_code == 422 and rejected.json()["problems"] == ["work_mem must be an integer"]
    listing = {"operation_kind": "parameter_group_create"}
    assert client.get("/api/v1/operations", params=listing).json()["operations"] == []
    accepted = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            **body,
            "options": {**options, "parameters": {"work_mem": "32768"}},
            "idempotency_key": "create-2",
        },
    )
    assert accepted.status_code == 202
    [operation] = client.get("/api/v1/operations", params=listing).json()["operations"]
    assert operation["title"] == "Create parameter group: orders-pg16-v2"
    assert operation["target_key"] == "sandbox/000000000000/eu-west-2"
    assert operation["approval_required_text"] is None


def test_clone_without_a_source_group_is_rejected_at_request_time(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    config = console_config.model_copy(deep=True)
    config.environment("sandbox").change_management = "direct"
    client = TestClient(
        create_application(settings=operator_settings, console_config=config, session_factory=session_factory)
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    response = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_create",
            "environment_slug": "sandbox",
            "options": {
                "target_key": "sandbox/000000000000/eu-west-2",
                "name": "orders-copy",
                "scope": "instance",
                "family": "postgres16",
                "description": "copy",
                "source": "clone",
            },
            "idempotency_key": "clone-without-source",
        },
    )
    assert response.status_code == 422
    assert any("clone_from_arn" in problem for problem in response.json()["problems"])


def test_attach_and_delete_require_the_identifier_to_be_typed(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    config = console_config.model_copy(deep=True)
    config.environment("sandbox").change_management = "direct"
    client = TestClient(
        create_application(settings=operator_settings, console_config=config, session_factory=session_factory)
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    for kind, resource, options, key in (
        ("parameter_group_attach", orders, {"parameter_group_arn": tuned, "reboot": "none"}, "attach"),
        ("reboot_instance", orders, {"force_failover": False}, "reboot"),
    ):
        response = client.post(
            "/api/v1/operations",
            headers={"X-CSRF-Token": token},
            json={
                "kind": kind,
                "resource_id": encode_resource_id(resource),
                "options": options,
                "idempotency_key": key,
            },
        )
        assert response.status_code == 202, response.json()
    operations = {o["operation_kind"]: o for o in client.get("/api/v1/operations").json()["operations"]}
    assert operations["parameter_group_attach"]["approval_required_text"] == "demo-orders"
    assert operations["reboot_instance"]["approval_required_text"] == "demo-orders"


def _direct_client(session_factory, operator_settings, console_config):
    config = console_config.model_copy(deep=True)
    config.environment("sandbox").change_management = "direct"
    client = TestClient(
        create_application(settings=operator_settings, console_config=config, session_factory=session_factory)
    )
    return client, config, client.get("/api/v1/bootstrap").json()["csrf_token"]


def _record_inventory(session_factory, config) -> None:
    import boto3

    from rdscommander.aws.rds_reader import RdsReader
    from rdscommander.inventory.snapshot_writer import discover_estate, write_snapshot

    rds = boto3.client("rds", region_name="eu-west-2")
    with session_factory() as session:
        write_snapshot(
            session, config.target("sandbox/000000000000/eu-west-2"), None, discover_estate(RdsReader(rds))
        )
        session.commit()


def _cluster_group_arn(session_factory, sandbox_snapshot) -> str:
    from tests.terraform.conftest import arn_for

    return arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")


def _new_group(name: str, family: str, parameters: dict[str, str] | None = None) -> str:
    import boto3

    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(DBParameterGroupName=name, DBParameterGroupFamily=family, Description=name)
    if parameters:
        rds.modify_db_parameter_group(
            DBParameterGroupName=name,
            Parameters=[
                {"ParameterName": key, "ParameterValue": value, "ApplyMethod": "immediate"}
                for key, value in parameters.items()
            ],
        )
    return rds.describe_db_parameter_groups(DBParameterGroupName=name)["DBParameterGroups"][0][
        "DBParameterGroupArn"
    ]


def test_attach_is_checked_before_approval_for_inventory_scope_and_family(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    client, config, token = _direct_client(session_factory, operator_settings, console_config)
    legacy_arn = _new_group("legacy-pg15", "postgres15")
    sibling_arn = _new_group("sibling-pg16", "postgres16")
    _record_inventory(session_factory, config)
    orders = encode_resource_id(arn_for(session_factory, sandbox_snapshot, "demo-orders"))
    cluster_group_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")

    def attach(group_arn: str, key: str, reboot: str = "none"):
        return client.post(
            "/api/v1/operations",
            headers={"X-CSRF-Token": token},
            json={
                "kind": "parameter_group_attach",
                "resource_id": orders,
                "options": {"parameter_group_arn": group_arn, "reboot": reboot},
                "idempotency_key": key,
            },
        )

    wrong_family = attach(legacy_arn, "family")
    assert wrong_family.status_code == 422
    [family_problem] = wrong_family.json()["problems"]
    assert (
        "legacy-pg15 is of family postgres15" in family_problem and "demo-postgres16-tuned" in family_problem
    )
    wrong_scope = attach(cluster_group_arn, "scope")
    assert wrong_scope.status_code == 422
    assert wrong_scope.json()["problems"] == [
        "demo-aurora-pg16-tuned is a cluster group; demo-orders needs an instance group"
    ]
    not_in_inventory = attach(sibling_arn.replace("sibling-pg16", "never-created"), "inventory")
    assert not_in_inventory.status_code == 422
    assert "not in the latest inventory" in not_in_inventory.json()["problems"][0]
    assert attach(sibling_arn, "fine").status_code == 202
    created = client.get("/api/v1/operations", params={"operation_kind": "parameter_group_attach"}).json()
    assert len(created["operations"]) == 1  # only the compatible request became an operation


def test_attach_offers_no_reboot_at_the_maintenance_window(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    client, config, token = _direct_client(session_factory, operator_settings, console_config)
    sibling_arn = _new_group("sibling-pg16", "postgres16")
    _record_inventory(session_factory, config)
    response = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_attach",
            "resource_id": encode_resource_id(arn_for(session_factory, sandbox_snapshot, "demo-orders")),
            "options": {"parameter_group_arn": sibling_arn, "reboot": "window"},
            "idempotency_key": "window",
        },
    )
    assert response.status_code == 422 and any("reboot" in problem for problem in response.json()["problems"])
    schema = client.get("/api/v1/catalogue").json()["definitions"]
    attach = next(d for d in schema if d["kind"] == "parameter_group_attach")
    assert attach["options_schema"]["properties"]["reboot"]["enum"] == ["none", "now"]


def test_create_refuses_a_name_that_exists_in_the_inventory_of_the_target(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client, _, token = _direct_client(session_factory, operator_settings, console_config)
    options = {
        "target_key": "sandbox/000000000000/eu-west-2",
        "name": "demo-postgres16-tuned",
        "scope": "instance",
        "family": "postgres16",
        "description": "again",
    }
    response = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_create",
            "environment_slug": "sandbox",
            "options": options,
            "idempotency_key": "exists",
        },
    )
    assert response.status_code == 422 and "already exists" in response.json()["problems"][0]
    same_name_other_scope = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_create",
            "environment_slug": "sandbox",
            "options": {**options, "scope": "cluster", "family": "aurora-postgresql16"},
            "idempotency_key": "other-scope",
        },
    )
    assert same_name_other_scope.status_code == 202  # instance and cluster groups have separate namespaces


def test_create_by_cloning_across_families_is_checked_at_request_time(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client, config, token = _direct_client(session_factory, operator_settings, console_config)
    legacy_arn = _new_group("legacy-pg15", "postgres15", {"work_mem": "8192", "force_parallel_mode": "off"})
    broken_arn = _new_group("broken-pg15", "postgres15", {"work_mem": "10"})
    _record_inventory(session_factory, config)

    def create(source_arn: str, key: str, parameters: dict[str, str] | None = None):
        return client.post(
            "/api/v1/operations",
            headers={"X-CSRF-Token": token},
            json={
                "kind": "parameter_group_create",
                "environment_slug": "sandbox",
                "options": {
                    "target_key": "sandbox/000000000000/eu-west-2",
                    "name": f"modern-{key}",
                    "scope": "instance",
                    "family": "postgres16",
                    "description": "modern",
                    "source": "clone",
                    "clone_from_arn": source_arn,
                    "parameters": parameters or {},
                },
                "idempotency_key": key,
            },
        )

    assert create(legacy_arn, "carried").status_code == 202  # force_parallel_mode is dropped, not refused
    invalid = create(broken_arn, "invalid")
    assert invalid.status_code == 422
    assert invalid.json()["problems"] == ["work_mem must be between 64 and 2147483647"]
    assert create(broken_arn, "overridden", {"work_mem": "4096"}).status_code == 202
    other_scope = create(_cluster_group_arn(session_factory, sandbox_snapshot), "scope")
    assert other_scope.status_code == 422
    assert "clone_from_arn" in other_scope.json()["problems"][0]


def _preview(client, token, arn: str, version: str = "17.2"):
    from rdscommander.resources.identity import encode_resource_id

    return client.post(
        "/api/v1/parameters/upgrade-preview",
        headers={"X-CSRF-Token": token},
        json={"resource_id": encode_resource_id(arn), "target_engine_version": version},
    )


def test_upgrade_preview_of_an_instance_classifies_the_overrides_of_its_tuned_group(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.terraform.conftest import arn_for

    client, _, token = _direct_client(session_factory, operator_settings, console_config)
    response = _preview(client, token, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
    assert response.status_code == 200
    preview = response.json()
    assert preview["target_family_instance"] == "postgres17" and preview["target_family_cluster"] is None
    [plan] = preview["plans"]
    assert plan["scope"] == "instance" and plan["source_family"] == "postgres16"
    assert plan["source_group_name"] == "demo-postgres16-tuned"
    assert plan["new_group_name"] == "demo-postgres16-tuned-pg17"
    assert {c["name"]: c["outcome"] for c in plan["classifications"]} == {
        "log_min_duration_statement": "carried",
        "rds.logical_replication": "carried",
        "shared_buffers": "carried",
        "work_mem": "carried",
    }
    assert plan["parameters_to_set"]["work_mem"] == "16384"


def test_upgrade_preview_of_an_aurora_instance_on_the_default_group_starts_from_defaults(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.terraform.conftest import arn_for

    client, _, token = _direct_client(session_factory, operator_settings, console_config)
    [plan] = _preview(client, token, arn_for(session_factory, sandbox_snapshot, "demo-aurora-writer")).json()[
        "plans"
    ]
    assert plan["source_group_name"] == "default.aurora-postgresql16"
    assert (
        plan["new_group_name"] == "demo-aurora-writer-pg17"
    )  # named after the instance, not the AWS default
    assert plan["classifications"] == [] and plan["parameters_to_set"] == {}


def test_upgrade_preview_answers_404_for_a_resource_that_is_not_in_the_inventory_and_422_for_a_bad_version(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.terraform.conftest import arn_for

    client, _, token = _direct_client(session_factory, operator_settings, console_config)
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    assert _preview(client, token, orders.replace("demo-orders", "no-such-db")).status_code == 404
    assert _preview(client, token, orders, version="seventeen").status_code == 422


def test_upgrade_preview_refuses_a_cluster_whose_members_use_different_instance_groups(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    import boto3

    from tests.terraform.conftest import arn_for

    client, config, token = _direct_client(session_factory, operator_settings, console_config)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="reader-pg16", DBParameterGroupFamily="aurora-postgresql16", Description="reader"
    )
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-aurora-reader", DBParameterGroupName="reader-pg16", ApplyImmediately=True
    )
    _record_inventory(session_factory, config)
    aurora = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    response = _preview(client, token, aurora)
    assert response.status_code == 422
    assert (
        "reader-pg16" in response.json()["detail"]
        and "default.aurora-postgresql16" in response.json()["detail"]
    )


def test_upgrade_build_request_is_refused_while_a_new_group_name_exists_and_accepted_with_a_suffix(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    client, config, token = _direct_client(session_factory, operator_settings, console_config)
    _new_group("demo-postgres16-tuned-pg17", "postgres17")
    _record_inventory(session_factory, config)
    orders = encode_resource_id(arn_for(session_factory, sandbox_snapshot, "demo-orders"))

    def request(key: str, name_suffix: str):
        return client.post(
            "/api/v1/operations",
            headers={"X-CSRF-Token": token},
            json={
                "kind": "parameter_group_build_for_upgrade",
                "resource_id": orders,
                "options": {"target_engine_version": "17.2", "name_suffix": name_suffix},
                "idempotency_key": key,
            },
        )

    refused = request("taken", "")
    assert refused.status_code == 422
    assert "demo-postgres16-tuned-pg17" in refused.json()["problems"][0]
    accepted = request("suffixed", "-v2")
    assert accepted.status_code == 202
    operation = client.get(f"/api/v1/operations/{accepted.json()['operation_id']}").json()
    assert operation["plan"]["resource_kind"] == "instance"
    assert operation["title"] == "Build upgrade parameter groups: demo-orders"


def test_upgrade_build_request_rejects_edits_for_a_group_it_will_not_build(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    client, _, token = _direct_client(session_factory, operator_settings, console_config)
    response = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_build_for_upgrade",
            "resource_id": encode_resource_id(arn_for(session_factory, sandbox_snapshot, "demo-orders")),
            "options": {"target_engine_version": "17.2", "overrides": {"cluster": {"work_mem": "8192"}}},
            "idempotency_key": "stray-edit",
        },
    )
    assert response.status_code == 422
    assert "cluster" in response.json()["problems"][0]
    unknown_scope = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_build_for_upgrade",
            "resource_id": encode_resource_id(arn_for(session_factory, sandbox_snapshot, "demo-orders")),
            "options": {"target_engine_version": "17.2", "overrides": {"shard": {"work_mem": "8192"}}},
            "idempotency_key": "unknown-scope",
        },
    )
    assert unknown_scope.status_code == 422


def test_upgrade_build_is_offered_on_instances_and_clusters_in_both_modes(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import arn_for

    client = _client(session_factory, operator_settings, console_config)  # Terraform mode
    for identifier in ("demo-orders", "demo-aurora"):
        page = client.get(
            f"/api/v1/resources/{encode_resource_id(arn_for(session_factory, sandbox_snapshot, identifier))}"
        ).json()
        action = next(a for a in page["actions"] if a["kind"] == "parameter_group_build_for_upgrade")
        assert action["applicable"] is True and action["group"] == "Parameter groups"


def _record_earlier_build(
    session_factory, group_name: str, scope: str, family: str, source_group_name: str
) -> None:
    from rdscommander.control_db.models import ParameterGroupProvenance

    with session_factory() as session:
        session.add(
            ParameterGroupProvenance(
                operation_id=None,
                target_key="sandbox/000000000000/eu-west-2",
                group_name=group_name,
                group_scope=scope,
                target_family=family,
                source_group_names=[source_group_name],
                classification=[],
            )
        )
        session.commit()


def _build_request(client, token, arn: str, key: str, name_suffix: str = ""):
    from rdscommander.resources.identity import encode_resource_id

    return client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_build_for_upgrade",
            "resource_id": encode_resource_id(arn),
            "options": {"target_engine_version": "17.2", "name_suffix": name_suffix},
            "idempotency_key": key,
        },
    )


def test_upgrade_build_request_completes_a_set_that_an_earlier_build_left_half_built(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.terraform.conftest import arn_for

    client, config, token = _direct_client(session_factory, operator_settings, console_config)
    aurora = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    _new_group("demo-aurora-pg17", "aurora-postgresql17")  # the instance group an earlier build created
    _record_inventory(session_factory, config)
    refused = _build_request(client, token, aurora, "no-provenance")
    assert refused.status_code == 422 and "demo-aurora-pg17" in refused.json()["problems"][0]
    _record_earlier_build(
        session_factory, "demo-aurora-pg17", "instance", "aurora-postgresql17", "default.aurora-postgresql16"
    )
    assert _build_request(client, token, aurora, "same-source").status_code == 202
    preview = _preview(client, token, aurora).json()
    assert {p["new_group_name"]: p["already_built"] for p in preview["plans"]} == {
        "demo-aurora-pg17": True,
        "demo-aurora-pg16-tuned-pg17": False,
    }


def test_upgrade_build_request_refuses_an_existing_group_another_source_or_family_built(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.terraform.conftest import arn_for

    client, config, token = _direct_client(session_factory, operator_settings, console_config)
    aurora = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    _new_group("demo-aurora-pg17", "aurora-postgresql17")
    _record_inventory(session_factory, config)
    _record_earlier_build(
        session_factory, "demo-aurora-pg17", "instance", "aurora-postgresql17", "some-other-group"
    )
    refused = _build_request(client, token, aurora, "other-source")
    assert refused.status_code == 422 and "demo-aurora-pg17" in refused.json()["problems"][0]
    _record_earlier_build(
        session_factory, "demo-aurora-pg17", "instance", "aurora-postgresql16", "default.aurora-postgresql16"
    )
    assert _build_request(client, token, aurora, "other-family").status_code == 422


def test_upgrade_build_request_is_refused_when_every_group_exists_from_an_earlier_build(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.terraform.conftest import arn_for

    client, config, token = _direct_client(session_factory, operator_settings, console_config)
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _new_group("demo-postgres16-tuned-pg17", "postgres17")
    _record_inventory(session_factory, config)
    _record_earlier_build(
        session_factory, "demo-postgres16-tuned-pg17", "instance", "postgres17", "demo-postgres16-tuned"
    )
    refused = _build_request(client, token, orders, "all-built")
    assert refused.status_code == 422 and "exists already" in refused.json()["detail"]
    assert _build_request(client, token, orders, "all-built-suffixed", "-v2").status_code == 202


def test_family_metadata_route_answers_for_the_scope_of_group_asked_for(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = _client(session_factory, operator_settings, console_config)
    path = "/api/v1/parameters/families/aurora-postgresql16"
    instance = client.get(path, params={"environment_slug": "sandbox"}).json()["parameters"]
    cluster = client.get(path, params={"environment_slug": "sandbox", "scope": "cluster"}).json()[
        "parameters"
    ]
    assert "max_connections" in instance and "rds.logical_replication" not in instance
    assert "rds.logical_replication" in cluster and "max_connections" not in cluster
    no_cluster_groups = client.get(
        "/api/v1/parameters/families/postgres16", params={"environment_slug": "sandbox", "scope": "cluster"}
    )
    assert (
        no_cluster_groups.status_code == 404
        and "no cluster parameter groups" in no_cluster_groups.json()["detail"]
    )


def test_a_change_to_a_resource_with_open_record_drift_is_refused_before_anything_is_created(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    """The change would record the drifted value as approved; the operator resolves the drift first."""
    from rdscommander.control_db.models import DriftFinding
    from rdscommander.resources.identity import encode_resource_id
    from tests.terraform.conftest import TARGET_KEY, arn_for

    config = console_config.model_copy(deep=True)
    config.environment("sandbox").change_management = "direct"
    client = _client(session_factory, operator_settings, config)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        session.add(
            DriftFinding(
                resource_arn=tuned,
                environment_slug="sandbox",
                target_key=TARGET_KEY,
                finding_kind="record",
                attributes=[
                    {
                        "address": "aws_db_parameter_group.demo_postgres16_tuned",
                        "attribute": "parameters.work_mem",
                        "recorded": "16384",
                        "live": "65536",
                    }
                ],
            )
        )
        session.commit()
    edit = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_edit",
            "resource_id": encode_resource_id(tuned),
            "options": {"parameters": {"work_mem": "40000"}, "reset": []},
            "idempotency_key": "edit-drifted",
        },
    )
    assert edit.status_code == 422, edit.json()
    assert edit.json()["detail"] == "demo-postgres16-tuned has drift that is not resolved"
    [problem] = edit.json()["problems"]
    assert "parameters.work_mem" in problem
    assert problem.endswith("Resolve drift on demo-postgres16-tuned first")
    created = client.get("/api/v1/operations", params={"operation_kind": "parameter_group_edit"}).json()
    assert created["operations"] == []
    aurora_group = arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")
    other = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "parameter_group_edit",
            "resource_id": encode_resource_id(aurora_group),
            "options": {"parameters": {"log_min_duration_statement": "2000"}, "reset": []},
            "idempotency_key": "edit-undrifted",
        },
    )
    assert other.status_code == 202, "a resource without drift is not held up by another resource's drift"
