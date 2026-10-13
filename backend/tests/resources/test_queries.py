from datetime import UTC, datetime, timedelta

from rdscommander.control_db.models import Operation, ResourceAdoption, TerraformWorkspace
from rdscommander.resources.identity import decode_resource_id, encode_resource_id
from rdscommander.resources.model import ResourceKind
from rdscommander.resources.queries import list_resources, load_resource, recent_operations_for
from tests.terraform.conftest import TARGET_KEY, arn_for


def test_load_resource_builds_a_record_with_facts_and_relationships(
    session_factory, console_config, sandbox_snapshot
) -> None:
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        record = load_resource(session, console_config, orders)
    assert record is not None and record.kind == ResourceKind.INSTANCE
    assert record.identifier == "demo-orders" and record.target_key == TARGET_KEY
    assert record.facts["db_instance_class"] == "db.m6g.large" and record.facts["engine_version"] == "16.4"
    assert record.relationships["parameter_group_name"] == "demo-postgres16-tuned"
    assert "demo-orders-replica" in record.relationships["replica_identifiers"]
    assert record.terraform_address is None and record.ownership == "unmanaged"


def test_list_resources_by_kind(session_factory, console_config, sandbox_snapshot) -> None:
    with session_factory() as session:
        clusters = list_resources(session, console_config, "sandbox", ResourceKind.CLUSTER)
        groups = list_resources(session, console_config, "sandbox", ResourceKind.PARAMETER_GROUP)
    assert [c.identifier for c in clusters] == ["demo-aurora"]
    assert {g.identifier for g in groups} >= {"demo-postgres16-tuned", "demo-aurora-pg16-tuned"}
    assert all(g.facts["group_scope"] in ("instance", "cluster") for g in groups)


def test_every_listed_record_carries_the_resource_id_of_its_arn(
    session_factory, console_config, sandbox_snapshot
) -> None:
    with session_factory() as session:
        records = list_resources(session, console_config, "sandbox")
    assert {record.kind for record in records} == set(ResourceKind) - {ResourceKind.ENVIRONMENT}
    assert all(record.resource_id == encode_resource_id(record.arn) for record in records)
    assert all(decode_resource_id(record.resource_id) == record.arn for record in records)


def test_unknown_arn_returns_none(session_factory, console_config, sandbox_snapshot) -> None:
    with session_factory() as session:
        assert load_resource(session, console_config, "arn:aws:rds:eu-west-2:000000000000:db:nope") is None


def test_malformed_arn_returns_none(session_factory, console_config, sandbox_snapshot) -> None:
    with session_factory() as session:
        assert load_resource(session, console_config, "demo-orders") is None


def test_cluster_record_lists_members_and_writer(session_factory, console_config, sandbox_snapshot) -> None:
    aurora = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    with session_factory() as session:
        record = load_resource(session, console_config, aurora)
    assert record is not None and record.kind == ResourceKind.CLUSTER
    assert set(record.relationships["member_identifiers"]) == {"demo-aurora-writer", "demo-aurora-reader"}
    assert record.relationships["writer_identifier"] in record.relationships["member_identifiers"]
    assert record.relationships["cluster_parameter_group_name"] == "demo-aurora-pg16-tuned"


def test_parameter_group_record_lists_the_resources_using_it(
    session_factory, console_config, sandbox_snapshot
) -> None:
    with session_factory() as session:
        groups = {
            group.identifier: group
            for group in list_resources(session, console_config, "sandbox", ResourceKind.PARAMETER_GROUP)
        }
    assert "demo-orders" in groups["demo-postgres16-tuned"].relationships["used_by_identifiers"]
    assert groups["demo-aurora-pg16-tuned"].relationships["used_by_identifiers"] == ["demo-aurora"]


def test_adopted_resource_carries_its_terraform_address(
    session_factory, console_config, sandbox_snapshot
) -> None:
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        workspace = TerraformWorkspace(
            target_key=TARGET_KEY,
            workspace_relative_dir="terraform/sandbox",
            state_bucket_name="state-bucket",
            state_key="terraform.tfstate",
        )
        session.add(workspace)
        session.flush()
        session.add(
            ResourceAdoption(
                workspace_id=workspace.id,
                resource_arn=orders,
                resource_identifier="demo-orders",
                resource_type="aws_db_instance",
                terraform_address="aws_db_instance.demo_orders",
                adoption_status="adopted",
            )
        )
        session.commit()
        record = load_resource(session, console_config, orders)
    assert record is not None and record.terraform_address == "aws_db_instance.demo_orders"


def test_recent_operations_for_returns_newest_first_and_respects_the_limit(
    session_factory, sandbox_snapshot
) -> None:
    orders_arn = "arn:aws:rds:eu-west-2:123456789012:db:demo-orders"
    base_time_utc = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        for minutes_offset in range(3):
            session.add(
                Operation(
                    operation_kind="inventory_refresh",
                    state="SUCCEEDED",
                    environment_slug="sandbox",
                    resource_arn=orders_arn,
                    plan={},
                    idempotency_key=f"recent-{minutes_offset}",
                    created_by_operator_name="phil",
                    created_at_utc=base_time_utc + timedelta(minutes=minutes_offset),
                )
            )
        session.add(
            Operation(
                operation_kind="inventory_refresh",
                state="SUCCEEDED",
                environment_slug="sandbox",
                resource_arn="arn:aws:rds:eu-west-2:123456789012:db:other",
                plan={},
                idempotency_key="other-resource",
                created_by_operator_name="phil",
            )
        )
        session.commit()
        newest_two = recent_operations_for(session, orders_arn, limit=2)
    assert [operation.idempotency_key for operation in newest_two] == ["recent-2", "recent-1"]


def test_open_drift_is_reported_in_facts_and_resolved_drift_is_not(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.control_db.models import DriftFinding
    from rdscommander.resources.queries import open_drift_findings_for

    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    aurora = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    with session_factory() as session:
        for arn, resolved_at in ((orders, None), (aurora, datetime.now(UTC))):
            session.add(
                DriftFinding(
                    resource_arn=arn,
                    environment_slug="sandbox",
                    target_key=TARGET_KEY,
                    finding_kind="record",
                    attributes=[
                        {"address": "x", "attribute": "instance_class", "recorded": "a", "live": "b"}
                    ],
                    resolved_at_utc=resolved_at,
                )
            )
        session.commit()
        listed = {
            r.identifier: r.facts["has_open_drift"]
            for r in list_resources(session, console_config, "sandbox")
        }
        loaded = load_resource(session, console_config, orders)
        open_findings = open_drift_findings_for(session, orders)
        none_open = open_drift_findings_for(session, aurora)
    assert listed["demo-orders"] is True and listed["demo-aurora"] is False
    assert all(value is False for name, value in listed.items() if name != "demo-orders")
    assert loaded is not None and loaded.facts["has_open_drift"] is True
    assert [f.resource_arn for f in open_findings] == [orders] and none_open == []
