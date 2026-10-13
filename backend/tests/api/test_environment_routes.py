import boto3
from fastapi.testclient import TestClient

from rdscommander.main import create_application


def test_inventory_routes_round_trip(session_factory, operator_settings, console_config, moto_aws) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_instance(
        DBInstanceIdentifier="pg-one",
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.t4g.micro",
        MasterUsername="u",
        MasterUserPassword="password1",
        AllocatedStorage=20,
    )
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]

    empty = client.get("/api/v1/environments/sandbox/inventory").json()
    assert empty["targets"][0]["snapshot"] is None

    created = client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "ui-1"},
    )
    assert created.status_code == 202
    operation_id = created.json()["operation_id"]
    app.state.runner.run_once()

    inventory = client.get("/api/v1/environments/sandbox/inventory").json()
    target = inventory["targets"][0]
    assert target["target_key"] == "sandbox/000000000000/eu-west-2" and target["simulated"] is True
    assert target["snapshot"]["operation_id"] == operation_id
    assert [i["db_instance_identifier"] for i in target["instances"]] == ["pg-one"]
    assert target["instances"][0]["ownership"] == "unmanaged"
    group_id = next(g["id"] for g in target["parameter_groups"] if g["is_default"])
    parameters = client.get(f"/api/v1/inventory/parameter-groups/{group_id}/parameters").json()
    assert isinstance(parameters["parameters"], list)
    assert client.get("/api/v1/environments/nope/inventory").status_code == 404


def test_environment_pages_and_profiles(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws, tmp_path
) -> None:
    (tmp_path / "aws-config").write_text(
        "[profile example-prod]\nsso_session = example\n[profile example-dev]\nregion = eu-west-2\n"
    )
    operator_settings.aws_config_path = tmp_path / "aws-config"
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    environments = client.get("/api/v1/environments").json()["environments"]
    sandbox = next(e for e in environments if e["slug"] == "sandbox")
    assert (
        sandbox["change_management"] == "terraform"
        and sandbox["counts"]["instance"] == 4
        and sandbox["last_refresh_at_utc"]
    )
    page = client.get("/api/v1/environments/sandbox").json()
    assert page["targets"][0]["target_key"] == "sandbox/000000000000/eu-west-2" and page["actions"]
    assert client.get("/api/v1/environments/import/profiles").json()["profiles"] == [
        "example-dev",
        "example-prod",
    ]
    compare = client.get(
        "/api/v1/environments/compare", params={"left": "sandbox", "right": "production"}
    ).json()
    assert compare["pairs"] == [] and {u["identifier"] for u in compare["unmatched_left"]} >= {"demo-orders"}


def _client_for(session_factory, operator_settings, console_config) -> TestClient:
    return TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )


def test_environment_overview_counts_resources_versions_ownership_and_running_operations(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client = _client_for(session_factory, operator_settings, console_config)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    assert client.get("/api/v1/environments/sandbox").json()["overview"]["running_operation_count"] == 0
    client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "overview-1"},
    )
    page = client.get("/api/v1/environments/sandbox").json()
    overview = page["overview"]
    assert overview["running_operation_count"] == 1
    assert overview["counts"]["cluster"] == 1 and overview["counts"]["snapshot"] >= 0
    assert (
        sum(overview["engine_versions"].values())
        == overview["counts"]["instance"] + overview["counts"]["cluster"]
    )
    assert sum(overview["ownership_counts"].values()) == (
        overview["counts"]["instance"] + overview["counts"]["cluster"] + overview["counts"]["parameter_group"]
    )
    assert overview["attention_count"] > 0 and overview["is_production"] is False
    assert [o["operation_kind"] for o in page["recent_operations"]] == [
        "inventory_refresh",
        "inventory_refresh",
    ]
    assert page["first_steps"] == {
        "imported": True,
        "refreshed": True,
        "adopted_or_direct": False,
        "mode_set": True,
    }
    production = next(
        e for e in client.get("/api/v1/environments").json()["environments"] if e["slug"] == "production"
    )
    assert production["last_refresh_at_utc"] is None and production["counts"]["instance"] == 0


def test_unknown_environments_are_not_found_on_the_page_and_in_comparisons(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = _client_for(session_factory, operator_settings, console_config)
    assert client.get("/api/v1/environments/nope").status_code == 404
    assert (
        client.get("/api/v1/environments/compare", params={"left": "sandbox", "right": "nope"}).status_code
        == 404
    )
    assert (
        client.get("/api/v1/environments/compare", params={"left": "nope", "right": "sandbox"}).status_code
        == 404
    )


def test_import_route_queues_a_titled_operation_without_an_environment(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = _client_for(session_factory, operator_settings, console_config)
    headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}
    body = {
        "idempotency_key": "import-ui-1",
        "options": {
            "slug": "staging",
            "display_name": "Staging",
            "simulated": True,
            "regions": ["eu-west-2"],
        },
    }
    created = client.post("/api/v1/environments/import", headers=headers, json=body)
    assert created.status_code == 202
    assert client.post("/api/v1/environments/import", headers=headers, json=body).json() == created.json()
    operations = client.get("/api/v1/operations").json()["operations"]
    [imported] = [o for o in operations if o["operation_kind"] == "environment_import"]
    assert imported["id"] == created.json()["operation_id"]
    assert imported["title"] == "Import environment: staging" and imported["environment_slug"] is None
    detail = client.get(f"/api/v1/operations/{imported['id']}").json()
    assert detail["plan"]["options"]["slug"] == "staging"
    assert detail["plan"]["options"]["change_management"] == "terraform"


def test_generic_operation_route_titles_an_import_with_the_slug(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = _client_for(session_factory, operator_settings, console_config)
    headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}
    created = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": "environment_import",
            "options": {"slug": "staging", "display_name": "Staging", "aws_profile_name": "example-dev"},
            "idempotency_key": "import-generic-1",
        },
    )
    assert created.status_code == 202
    operation = client.get(f"/api/v1/operations/{created.json()['operation_id']}").json()
    assert operation["title"] == "Import environment: staging"


def test_import_route_rejects_options_that_cannot_work_with_the_problems_listed(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = _client_for(session_factory, operator_settings, console_config)
    headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}

    def post(options: dict) -> dict:
        response = client.post(
            "/api/v1/environments/import",
            headers=headers,
            json={"idempotency_key": "bad", "options": options},
        )
        assert response.status_code == 422
        return response.json()

    both = post(
        {"slug": "staging", "display_name": "S", "simulated": True, "aws_profile_name": "example-dev"}
    )
    assert any("choose either a profile or the simulated account" in problem for problem in both["problems"])
    neither = post({"slug": "staging", "display_name": "S"})
    assert any(
        "choose either a profile or the simulated account" in problem for problem in neither["problems"]
    )
    bad_slug = post({"slug": "Not A Slug", "display_name": "S", "simulated": True})
    assert any(problem.startswith("slug") for problem in bad_slug["problems"])
    reserved = post({"slug": "compare", "display_name": "S", "simulated": True})
    assert any(
        "'compare' is reserved for a page of the console" in problem for problem in reserved["problems"]
    )
    bad_region = post({"slug": "staging", "display_name": "S", "simulated": True, "regions": ["mars-1"]})
    assert any("not AWS region names: mars-1" in problem for problem in bad_region["problems"])


def test_option_problems_read_as_sentences_not_as_pydantic_messages(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    client = _client_for(session_factory, operator_settings, console_config)
    headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}

    def problems_for(options: dict) -> list[str]:
        response = client.post(
            "/api/v1/operations",
            headers=headers,
            json={"kind": "environment_import", "options": options, "idempotency_key": "sentences"},
        )
        assert response.status_code == 422
        return response.json()["problems"]

    assert problems_for({"slug": "Not A Slug", "display_name": "S", "simulated": True}) == [
        "slug: must match 2 to 40 lowercase letters, digits and hyphens, starting with a letter, "
        "for example staging"
    ]
    assert problems_for({"slug": "staging", "display_name": "S"}) == [
        "choose either a profile or the simulated account"
    ]
    assert problems_for({"slug": "staging", "display_name": "", "simulated": True}) == [
        "display_name: must not be empty"
    ]
    assert problems_for({"display_name": "S", "simulated": True}) == ["slug: is required"]
    assert problems_for(
        {"slug": "staging", "display_name": "S", "simulated": True, "change_management": "manual"}
    ) == ["change_management: must be 'terraform' or 'direct'"]
    assert problems_for({"slug": "staging", "display_name": "S", "simulated": "maybe"}) == [
        "simulated: must be true or false"
    ]
    assert problems_for({"slug": "staging", "display_name": "S", "simulated": True, "colour": "red"}) == [
        "colour: is not an option of this task"
    ]


def test_a_tagged_subnet_group_that_is_not_under_terraform_is_offered_by_the_picker_and_demanded_by_the_check(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    # A failed adoption apply can leave the console's tags on a subnet group without adopting it. The
    # inventory the adoption picker reads called it adopted (tags) while the prerequisite check of an
    # instance using it still demanded it (adoption rows), so the operator had no way to adopt it.
    from rdscommander.terraform.adoption_plan import AdoptionPlanError, build_adoption_plan

    ec2 = boto3.client("ec2", region_name="eu-west-2")
    vpc = ec2.create_vpc(CidrBlock="10.9.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.9.{n}.0/24", AvailabilityZone=f"eu-west-2{zone}")[
            "Subnet"
        ]["SubnetId"]
        for n, zone in ((1, "a"), (2, "b"))
    ]
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_subnet_group(
        DBSubnetGroupName="tagged-subnets",
        DBSubnetGroupDescription="left tagged by a failed adoption",
        SubnetIds=subnets,
        Tags=[{"Key": "rdscommander:managed", "Value": "true"}],
    )
    rds.create_db_instance(
        DBInstanceIdentifier="pg-two",
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.t4g.micro",
        MasterUsername="u",
        MasterUserPassword="password1",
        AllocatedStorage=20,
        DBSubnetGroupName="tagged-subnets",
    )
    app = create_application(
        settings=operator_settings, console_config=console_config, session_factory=session_factory
    )
    client = TestClient(app)
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    client.post(
        "/api/v1/environments/sandbox/inventory/refresh",
        headers={"X-CSRF-Token": token},
        json={"idempotency_key": "ui-tagged"},
    )
    app.state.runner.run_once()

    target = client.get("/api/v1/environments/sandbox/inventory").json()["targets"][0]
    [subnet_group] = [s for s in target["subnet_groups"] if s["db_subnet_group_name"] == "tagged-subnets"]
    assert subnet_group["ownership"] == "unmanaged", "the picker offers what is not under Terraform"

    instance_arn = next(
        i["db_instance_arn"] for i in target["instances"] if i["db_instance_identifier"] == "pg-two"
    )
    with session_factory() as session:
        try:
            build_adoption_plan(session, console_config, target["target_key"], [instance_arn])
            raise AssertionError("the check must demand the subnet group the picker offers")
        except AdoptionPlanError as error:
            assert any("tagged-subnets" in problem for problem in error.problems)
        plan = build_adoption_plan(
            session, console_config, target["target_key"], [instance_arn, subnet_group["db_subnet_group_arn"]]
        )
    assert {r.resource_identifier for r in plan.selected_resources} == {"pg-two", "tagged-subnets"}
