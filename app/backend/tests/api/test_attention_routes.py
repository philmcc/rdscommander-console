from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from rdscommander.control_db.models import (
    InventoryDbInstance,
    InventoryPendingMaintenance,
    Operation,
    ResourceChangeEvent,
)
from rdscommander.main import create_application
from rdscommander.resources.identity import encode_resource_id
from tests.terraform.conftest import arn_for


def test_attention_inbox_lists_awaiting_approval_and_unmanaged(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    with session_factory() as session:
        session.add(
            Operation(
                operation_kind="terraform_adoption",
                state="AWAITING_APPROVAL",
                environment_slug="sandbox",
                target_key="sandbox/000000000000/eu-west-2",
                plan={},
                idempotency_key="att-1",
                created_by_operator_name="phil",
                title="Adopt resources: demo-orders",
            )
        )
        session.commit()
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )
    inbox = client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()
    assert inbox["awaiting_approval"][0]["title"] == "Adopt resources: demo-orders"
    assert {i["resource_identifier"] for i in inbox["unmanaged"]} >= {"demo-orders", "demo-aurora"}
    assert inbox["drift"] == [] and inbox["pending_reboot"] == []


def _client(session_factory, operator_settings, console_config) -> TestClient:
    return TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )


def test_attention_inbox_lists_drift_with_the_action_that_resolves_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group, _refresh_and_detect, _set_work_mem

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _set_work_mem("65536")
    _refresh_and_detect(runner, service, "inbox")
    inbox = _client(session_factory, operator_settings, console_config).get("/api/v1/attention").json()
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    [item] = inbox["drift"]
    assert (
        item["title"] == "Drift on demo-postgres16-tuned" and item["detail"] == "record: parameters.work_mem"
    )
    assert item["resource_id"] == encode_resource_id(group_arn)
    assert (item["action_kind"], item["action_options"]) == ("drift_resolve", {"resource_arn": group_arn})
    assert "demo-postgres16-tuned" not in {i["resource_identifier"] for i in inbox["unmanaged"]}, (
        "an adopted group is no longer unmanaged"
    )


def test_attention_inbox_lists_failed_operations_of_the_last_seven_days_only(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        for key, age in (("recent", timedelta(days=1)), ("old", timedelta(days=8))):
            session.add(
                Operation(
                    operation_kind="inventory_refresh",
                    state="FAILED",
                    environment_slug="sandbox",
                    plan={},
                    idempotency_key=f"failed-{key}",
                    created_by_operator_name="phil",
                    title=f"Refresh inventory: {key}",
                    failure_summary="credentials expired",
                    finished_at_utc=now - age,
                )
            )
        session.commit()
    inbox = (
        _client(session_factory, operator_settings, console_config)
        .get("/api/v1/attention", params={"environment_slug": "sandbox"})
        .json()
    )
    assert [(i["title"], i["detail"]) for i in inbox["failed_recent"]] == [
        ("Refresh inventory: recent", "credentials expired")
    ]


def test_attention_inbox_lists_pending_reboots_pending_maintenance_and_foreign_owned_resources(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    replica_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders-replica")
    with session_factory() as session:
        orders = session.query(InventoryDbInstance).filter_by(db_instance_arn=orders_arn).one()
        orders.parameter_apply_status = "pending-reboot"
        session.query(InventoryDbInstance).filter_by(
            db_instance_arn=replica_arn
        ).one().ownership = "foreign_terraform"
        session.add(
            InventoryPendingMaintenance(
                snapshot_id=sandbox_snapshot.id,
                resource_arn=orders_arn,
                action="system-update",
                description="New minor version available",
            )
        )
        session.commit()
    inbox = (
        _client(session_factory, operator_settings, console_config)
        .get("/api/v1/attention", params={"environment_slug": "sandbox"})
        .json()
    )
    [reboot] = inbox["pending_reboot"]
    assert reboot["title"] == "demo-orders needs a reboot" and reboot["action_kind"] == "reboot_instance"
    assert reboot["resource_id"] == encode_resource_id(orders_arn)
    [maintenance] = inbox["pending_maintenance"]
    assert (maintenance["title"], maintenance["detail"]) == (
        "demo-orders: system-update",
        "New minor version available",
    )
    [foreign] = inbox["foreign_owned"]
    assert foreign["title"] == "demo-orders-replica is foreign terraform" and foreign["action_kind"] is None
    assert "demo-orders-replica" not in {i["resource_identifier"] for i in inbox["unmanaged"]}


def test_attention_inbox_lists_unreviewed_outside_changes_only(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        for attribute_path, reviewed_at in (("db_instance_class", None), ("multi_az", datetime.now(UTC))):
            session.add(
                ResourceChangeEvent(
                    resource_arn=orders_arn,
                    environment_slug="sandbox",
                    target_key="sandbox/000000000000/eu-west-2",
                    snapshot_after_id=sandbox_snapshot.id,
                    attribute_path=attribute_path,
                    value_before="before",
                    value_after="after",
                    explanation="outside_console",
                    reviewed_at_utc=reviewed_at,
                )
            )
        session.commit()
    inbox = (
        _client(session_factory, operator_settings, console_config)
        .get("/api/v1/attention", params={"environment_slug": "sandbox"})
        .json()
    )
    [change] = inbox["outside_changes"]
    assert change["title"] == "demo-orders: db_instance_class"
    assert change["detail"] == "'before' became 'after'"


def test_attention_inbox_offers_no_adoption_in_a_direct_mode_environment(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    direct_config = console_config.model_copy(deep=True)
    direct_config.environments[0].change_management = "direct"
    inbox = (
        _client(session_factory, operator_settings, direct_config)
        .get("/api/v1/attention", params={"environment_slug": "sandbox"})
        .json()
    )
    assert inbox["unmanaged"] == []


def test_attention_inbox_covers_every_environment_unless_one_is_named(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client = _client(session_factory, operator_settings, console_config)
    everywhere = client.get("/api/v1/attention").json()
    assert {i["environment_slug"] for i in everywhere["unmanaged"]} == {"sandbox"}
    production = client.get("/api/v1/attention", params={"environment_slug": "production"}).json()
    assert all(section == [] for section in production.values())
    assert client.get("/api/v1/attention", params={"environment_slug": "nope"}).status_code == 404


def test_every_drift_row_can_be_resolved_including_those_without_a_resource_page(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import DriftFinding

    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    subnet_group_arn = arn_for(session_factory, sandbox_snapshot, "demo-private-subnets")
    vanished_address = "aws_db_instance.demo_orders_replica"
    with session_factory() as session:
        for resource_key in (orders_arn, subnet_group_arn, vanished_address):
            session.add(
                DriftFinding(
                    resource_arn=resource_key,
                    environment_slug="sandbox",
                    target_key="sandbox/000000000000/eu-west-2",
                    finding_kind="record",
                    attributes=[{"address": "x", "attribute": "presence", "recorded": True, "live": False}],
                )
            )
        session.commit()
    inbox = (
        _client(session_factory, operator_settings, console_config)
        .get("/api/v1/attention", params={"environment_slug": "sandbox"})
        .json()
    )
    rows = {row["resource_identifier"]: row for row in inbox["drift"]}
    assert set(rows) == {"demo-orders", "demo-private-subnets", vanished_address}
    assert all(row["action_kind"] == "drift_resolve" for row in rows.values())
    assert rows["demo-orders"]["resource_id"] == encode_resource_id(orders_arn)
    assert rows["demo-orders"]["action_options"] == {"resource_arn": orders_arn}
    for without_a_page, resource_key in (
        (rows["demo-private-subnets"], subnet_group_arn),
        (rows[vanished_address], vanished_address),
    ):
        assert without_a_page["resource_id"] is None, "a link here would 404"
        assert without_a_page["action_options"] == {"resource_arn": resource_key, "choice": "accept"}


def test_the_pending_reboot_row_of_a_foreign_owned_instance_offers_no_reboot(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        orders = session.query(InventoryDbInstance).filter_by(db_instance_arn=orders_arn).one()
        orders.parameter_apply_status = "pending-reboot"
        orders.ownership = "foreign_cloudformation"
        session.commit()
    inbox = (
        _client(session_factory, operator_settings, console_config)
        .get("/api/v1/attention", params={"environment_slug": "sandbox"})
        .json()
    )
    [reboot] = inbox["pending_reboot"]
    assert reboot["title"] == "demo-orders needs a reboot" and reboot["action_kind"] is None
    assert "owned by other tooling" in reboot["detail"]


def test_a_failed_operation_of_no_environment_is_in_every_environments_inbox_once(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    with session_factory() as session:
        session.add(
            Operation(
                operation_kind="environment_import",
                state="FAILED",
                environment_slug=None,
                plan={},
                idempotency_key="failed-import",
                created_by_operator_name="phil",
                title="Import environment: staging",
                failure_summary="the SSO session has expired; run aws sso login --profile acme-dev",
                finished_at_utc=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.commit()
    client = _client(session_factory, operator_settings, console_config)
    for params in ({"environment_slug": "sandbox"}, {"environment_slug": "production"}, {}):
        failed = client.get("/api/v1/attention", params=params).json()["failed_recent"]
        assert [(i["title"], i["environment_slug"]) for i in failed] == [
            ("Import environment: staging", "")
        ], params
        assert failed[0]["operation_id"] and "aws sso login" in failed[0]["detail"]


def test_the_blue_side_of_a_major_upgrade_is_listed_with_decommission_only_when_it_carries_the_tag(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    import uuid

    upgrade_operation_id = uuid.uuid4()
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    replica_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders-replica")
    with session_factory() as session:
        blue_side = session.query(InventoryDbInstance).filter_by(db_instance_arn=orders_arn).one()
        blue_side.db_instance_identifier = "demo-orders-old2"
        blue_side.db_instance_arn = f"{orders_arn}-old2"
        blue_side.tags = {**blue_side.tags, "rdscommander:blue-side": str(upgrade_operation_id)}
        # an "-old1" instance the console did not leave behind is not a blue-side row
        untagged = session.query(InventoryDbInstance).filter_by(db_instance_arn=replica_arn).one()
        untagged.db_instance_identifier = "someone-elses-old1"
        session.commit()
    direct_config = console_config.model_copy(deep=True)
    direct_config.environment("sandbox").change_management = "direct"
    inbox = (
        _client(session_factory, operator_settings, direct_config)
        .get("/api/v1/attention", params={"environment_slug": "sandbox"})
        .json()
    )
    [row] = inbox["blue_side"]
    # AWS appends the next free -old<n>: a second upgrade's blue side is -old2
    assert row["kind"] == "blue_side" and row["resource_identifier"] == "demo-orders-old2"
    assert row["title"] == "demo-orders-old2 is the blue side of a major upgrade"
    assert row["operation_id"] == str(upgrade_operation_id)
    assert row["action_kind"] == "decommission"
    assert row["resource_id"] == encode_resource_id(f"{orders_arn}-old2")
    # in Terraform mode an instance that is not adopted cannot be decommissioned here: the row says why
    terraform_inbox = (
        _client(session_factory, operator_settings, console_config)
        .get("/api/v1/attention", params={"environment_slug": "sandbox"})
        .json()
    )
    [terraform_row] = terraform_inbox["blue_side"]
    assert terraform_row["action_kind"] is None and "adopt it first" in terraform_row["detail"]
