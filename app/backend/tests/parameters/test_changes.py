import boto3
import pytest

from rdscommander.aws.rds_reader import RdsReader
from rdscommander.control_db.models import Operation
from rdscommander.inventory.snapshot_writer import discover_estate, write_snapshot
from rdscommander.operations.executor import StepContext
from rdscommander.parameters.changes import (
    ParameterGroupAttach,
    ParameterGroupCreate,
    ParameterGroupDelete,
    ParameterGroupEdit,
    ParameterGroupInUse,
    ParameterGroupNotAttachable,
    ParameterValidationError,
    attach_problems,
    clone_parameters,
    create_problems,
    group_name_is_taken,
    parameter_problems,
)
from rdscommander.parameters.metadata import load_family_metadata
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_record import _input

TUNED_GROUP_ADDRESS = "aws_db_parameter_group.demo_postgres16_tuned"
ORDERS_ADDRESS = "aws_db_instance.demo_orders"


def _rds():
    return boto3.client("rds", region_name="eu-west-2")


def _record_inventory(session_factory, console_config) -> None:
    """Write a new inventory snapshot of the simulated account as it is now."""
    with session_factory() as session:
        write_snapshot(session, console_config.target(TARGET_KEY), None, discover_estate(RdsReader(_rds())))
        session.commit()


def _create_instance_group(name: str, family: str, parameters: dict[str, str] | None = None) -> str:
    """Create an instance parameter group in the simulator and return its ARN."""
    rds = _rds()
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


@pytest.fixture
def build_context(session_factory, operator_settings, console_config):
    """`build_context(resource_arn)` builds a step context; its session is closed when the test ends."""
    from rdscommander.main import build_services

    sessions = []
    services = build_services(operator_settings, session_factory)

    def build(resource_arn: str) -> StepContext:
        session = session_factory()
        sessions.append(session)
        operation = Operation(
            operation_kind="x",
            state="RUNNING",
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            plan={},
            idempotency_key=f"ctx-{resource_arn}",
            created_by_operator_name="phil",
            resource_arn=resource_arn,
            title="x",
        )
        session.add(operation)
        session.commit()
        return StepContext(
            session=session,
            operation=operation,
            settings=operator_settings,
            console_config=console_config,
            services=services,
        )

    yield build
    for session in sessions:
        session.close()


def test_edit_change_set_marks_static_parameters_as_pending_reboot(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    context = build_context(group_arn)
    change = ParameterGroupEdit(
        group_arn,
        {"work_mem": "65536", "shared_buffers": "{DBInstanceClassMemory/16384}"},
        reset=["log_min_duration_statement"],
    )
    entries = {e.attribute: e for e in change.change_set(context)}
    assert entries["parameters.work_mem"].apply_type == "immediate"
    assert entries["parameters.work_mem"].current == "16384"
    assert entries["parameters.shared_buffers"].apply_type == "pending-reboot"
    assert entries["parameters.shared_buffers"].requires_reboot
    assert entries["parameters.log_min_duration_statement"].desired is None
    assert entries["parameters.log_min_duration_statement"].current == "500"
    assert "reboot" in entries["parameters.shared_buffers"].downtime
    rendered = change.apply_to_render_input(_input(session_factory, console_config, sandbox_snapshot))
    group = next(g for g in rendered.instance_parameter_groups if g.name == "demo-postgres16-tuned")
    assert {p.name: p.value for p in group.parameters} == {
        "work_mem": "65536",
        "shared_buffers": "{DBInstanceClassMemory/16384}",
        "rds.logical_replication": "1",
    }


def test_edit_change_set_leaves_out_parameters_that_already_have_the_value(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    context = build_context(group_arn)
    change = ParameterGroupEdit(group_arn, {"work_mem": "16384"}, reset=["random_page_cost"])
    assert change.change_set(context) == []


def test_edit_renders_new_parameters_with_the_apply_method_the_executor_resolved(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    change = ParameterGroupEdit(
        group_arn,
        {"work_mem": "65536", "random_page_cost": "1.1", "max_connections": "200"},
        reset=[],
        apply_methods={"random_page_cost": "immediate", "max_connections": "pending-reboot"},
    )
    rendered = change.apply_to_render_input(_input(session_factory, console_config, sandbox_snapshot))
    group = next(g for g in rendered.instance_parameter_groups if g.name == "demo-postgres16-tuned")
    methods = {p.name: p.apply_method for p in group.parameters}
    assert methods["work_mem"] == "immediate"  # an existing parameter keeps its method
    assert methods["random_page_cost"] == "immediate"
    assert methods["max_connections"] == "pending-reboot"


def test_edit_of_a_cluster_group_uses_the_cluster_group_address(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")
    context = build_context(group_arn)
    change = ParameterGroupEdit(group_arn, {"log_min_duration_statement": "2000"}, reset=[])
    [entry] = change.change_set(context)
    assert entry.address == "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned"
    assert entry.current == "1000" and entry.desired == "2000"
    assert change.touched_addresses(context) == {entry.address}


def test_touched_addresses_come_from_the_loaded_record_without_a_change_set(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    writer_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora-writer")
    context = build_context(group_arn)
    assert ParameterGroupEdit(group_arn, {}, []).touched_addresses(context) == {TUNED_GROUP_ADDRESS}
    assert ParameterGroupDelete(group_arn).touched_addresses(context) == {TUNED_GROUP_ADDRESS}
    assert ParameterGroupAttach(orders_arn, group_arn, "none").touched_addresses(context) == {ORDERS_ADDRESS}
    assert ParameterGroupAttach(aurora_arn, group_arn, "none").touched_addresses(context) == {
        "aws_rds_cluster.demo_aurora"
    }
    assert ParameterGroupAttach(writer_arn, group_arn, "none").touched_addresses(context) == {
        "aws_rds_cluster_instance.demo_aurora_writer"
    }
    created = ParameterGroupCreate(TARGET_KEY, "orders-pg16-v2", "instance", "postgres16", "d", {})
    assert created.touched_addresses(context) == {"aws_db_parameter_group.orders_pg16_v2"}
    cluster_created = ParameterGroupCreate(TARGET_KEY, "aurora-v2", "cluster", "aurora-postgresql16", "d", {})
    assert cluster_created.touched_addresses(context) == {"aws_rds_cluster_parameter_group.aurora_v2"}


def test_create_change_set_lists_every_override_and_renders_a_new_group(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    context = build_context("")
    change = ParameterGroupCreate(
        TARGET_KEY, "orders-pg16-v2", "instance", "postgres16", "orders v2", {"work_mem": "32768"}
    )
    [entry] = change.change_set(context)
    assert entry.address == "aws_db_parameter_group.orders_pg16_v2"
    assert entry.attribute == "parameters.work_mem"
    assert entry.current is None
    rendered = change.apply_to_render_input(_input(session_factory, console_config, sandbox_snapshot))
    new_group = next(g for g in rendered.instance_parameter_groups if g.name == "orders-pg16-v2")
    assert new_group.family == "postgres16"
    assert [(p.name, p.value) for p in new_group.parameters] == [("work_mem", "32768")]


def test_create_without_overrides_still_has_a_change_set_entry(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    context = build_context("")
    change = ParameterGroupCreate(TARGET_KEY, "empty-pg16", "instance", "postgres16", "empty", {})
    [entry] = change.change_set(context)
    assert entry.attribute == "presence" and entry.current is None and entry.desired == "postgres16 group"


def test_create_refuses_a_name_that_already_exists(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    context = build_context("")
    change = ParameterGroupCreate(TARGET_KEY, "demo-postgres16-tuned", "instance", "postgres16", "again", {})
    with pytest.raises(KeyError, match="already exists"):
        change.change_set(context)


def _metadata(session_factory, family: str):
    with session_factory() as session:
        return load_family_metadata(session, family, None, scope="instance", simulated=True)


def test_cloning_a_group_of_the_same_family_carries_every_override(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    source_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        cloned = clone_parameters(
            session,
            console_config,
            source_arn,
            "instance",
            "postgres16",
            _metadata(session_factory, "postgres16"),
        )
    assert cloned.parameters == {
        "log_min_duration_statement": "500",
        "rds.logical_replication": "1",
        "shared_buffers": "{DBInstanceClassMemory/32768}",
        "work_mem": "16384",
    }
    assert cloned.dropped == [] and not cloned.cross_family and cloned.source_name == "demo-postgres16-tuned"


def test_cloning_across_families_drops_the_parameters_the_new_family_lacks_and_lists_them(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    legacy_arn = _create_instance_group(
        "legacy-pg15",
        "postgres15",
        {"work_mem": "8192", "wal_compression": "on", "force_parallel_mode": "off"},
    )
    _record_inventory(session_factory, console_config)
    with session_factory() as session:
        cloned = clone_parameters(
            session,
            console_config,
            legacy_arn,
            "instance",
            "postgres16",
            _metadata(session_factory, "postgres16"),
        )
    assert cloned.cross_family
    assert cloned.parameters == {"work_mem": "8192", "wal_compression": "on"}
    assert cloned.dropped == ["force_parallel_mode"]


def test_cloning_without_family_metadata_drops_nothing(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    legacy_arn = _create_instance_group("legacy-pg15", "postgres15", {"force_parallel_mode": "off"})
    _record_inventory(session_factory, console_config)
    with session_factory() as session:
        cloned = clone_parameters(session, console_config, legacy_arn, "instance", "postgres16", None)
    assert cloned.parameters == {"force_parallel_mode": "off"} and cloned.dropped == []


def test_cross_family_values_are_validated_against_the_new_family_and_the_operator_can_override_them(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    legacy_arn = _create_instance_group("legacy-pg15", "postgres15", {"work_mem": "10"})
    _record_inventory(session_factory, console_config)
    metadata = _metadata(session_factory, "postgres16")
    with session_factory() as session:
        cloned = clone_parameters(session, console_config, legacy_arn, "instance", "postgres16", metadata)
    [problem] = create_problems(metadata, {}, cloned)
    assert problem.message == "work_mem must be between 64 and 2147483647"
    assert create_problems(metadata, {"work_mem": "4096"}, cloned) == []


def test_values_cloned_within_a_family_are_not_validated_again(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    sibling_arn = _create_instance_group("sibling-pg16", "postgres16", {"work_mem": "10"})
    _record_inventory(session_factory, console_config)
    metadata = _metadata(session_factory, "postgres16")
    with session_factory() as session:
        cloned = clone_parameters(session, console_config, sibling_arn, "instance", "postgres16", metadata)
    assert cloned.parameters == {"work_mem": "10"}
    assert create_problems(metadata, {}, cloned) == []  # already in AWS, so valid there by definition


def test_cloning_refuses_a_source_of_the_other_scope_or_kind_or_one_that_is_missing(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    source_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    metadata = _metadata(session_factory, "postgres16")
    with session_factory() as session:
        with pytest.raises(ValueError, match="instance group"):
            clone_parameters(session, console_config, source_arn, "cluster", "postgres16", metadata)
        with pytest.raises(ValueError, match="not a parameter group"):
            clone_parameters(session, console_config, orders_arn, "instance", "postgres16", metadata)
        with pytest.raises(KeyError, match="no-such-group"):
            clone_parameters(
                session,
                console_config,
                source_arn.replace("demo-postgres16-tuned", "no-such-group"),
                "instance",
                "postgres16",
                metadata,
            )


def test_group_name_is_taken_by_scope_in_the_inventory(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    with session_factory() as session:
        assert group_name_is_taken(session, console_config, TARGET_KEY, "instance", "demo-postgres16-tuned")
        assert not group_name_is_taken(
            session, console_config, TARGET_KEY, "cluster", "demo-postgres16-tuned"
        )
        assert not group_name_is_taken(session, console_config, TARGET_KEY, "instance", "orders-pg16-v2")


def test_attach_change_set_targets_the_instance_parameter_group_attribute(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = _create_instance_group("orders-pg16-v2", "postgres16")
    _record_inventory(session_factory, console_config)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    context = build_context(orders_arn)
    [entry] = ParameterGroupAttach(orders_arn, group_arn, reboot="none").change_set(context)
    assert entry.address == "aws_db_instance.demo_orders" and entry.attribute == "parameter_group_name"
    assert entry.current == "demo-postgres16-tuned" and entry.desired == "orders-pg16-v2"
    assert entry.apply_type == "pending-reboot" and not entry.requires_reboot
    assert "until the next reboot" in entry.downtime and "window" not in entry.downtime


def test_attach_with_a_reboot_now_says_so_in_the_change_set(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = _create_instance_group("orders-pg16-v2", "postgres16")
    _record_inventory(session_factory, console_config)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    context = build_context(orders_arn)
    [entry] = ParameterGroupAttach(orders_arn, group_arn, reboot="now").change_set(context)
    assert entry.requires_reboot and "reboot" in entry.downtime


def test_attach_to_the_group_already_attached_changes_nothing(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    context = build_context(orders_arn)
    assert ParameterGroupAttach(orders_arn, group_arn, reboot="none").change_set(context) == []


def test_attach_refuses_a_group_of_the_other_scope(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    cluster_group_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    context = build_context(orders_arn)
    with pytest.raises(ParameterGroupNotAttachable, match="needs an instance group"):
        ParameterGroupAttach(orders_arn, cluster_group_arn, reboot="none").change_set(context)


def test_attach_refuses_a_group_of_another_family_or_one_the_inventory_lacks(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    other_family_arn = _create_instance_group("legacy-pg15", "postgres15")
    unseen_arn = _create_instance_group("unseen-pg16", "postgres16")
    _record_inventory(session_factory, console_config)
    unseen_arn = unseen_arn.replace("unseen-pg16", "never-created")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    context = build_context(orders_arn)
    with pytest.raises(ParameterGroupNotAttachable, match="family postgres15.*uses family postgres16"):
        ParameterGroupAttach(orders_arn, other_family_arn, reboot="none").change_set(context)
    with pytest.raises(ParameterGroupNotAttachable, match="not in the latest inventory"):
        ParameterGroupAttach(orders_arn, unseen_arn, reboot="none").change_set(context)
    with pytest.raises(ParameterGroupNotAttachable, match="family postgres15"):
        ParameterGroupAttach(orders_arn, other_family_arn, reboot="none").ensure_attachable(context)


def test_attach_problems_names_every_reason_and_is_empty_for_a_compatible_group(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.resources.queries import load_resource

    sibling_arn = _create_instance_group("sibling-pg16", "postgres16")
    legacy_arn = _create_instance_group("legacy-pg15", "postgres15")
    _record_inventory(session_factory, console_config)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    cluster_group_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")
    with session_factory() as session:
        orders = load_resource(session, console_config, orders_arn)
        aurora = load_resource(session, console_config, aurora_arn)
        assert attach_problems(session, console_config, orders, sibling_arn) == []
        assert attach_problems(session, console_config, aurora, cluster_group_arn) == []
        [wrong_family] = attach_problems(session, console_config, orders, legacy_arn)
        assert (
            "legacy-pg15 is of family postgres15" in wrong_family and "demo-postgres16-tuned" in wrong_family
        )
        [cluster_needs_cluster_group] = attach_problems(session, console_config, aurora, sibling_arn)
        assert "needs a cluster group" in cluster_needs_cluster_group
        assert attach_problems(session, console_config, orders, "arn:aws:rds:eu-west-2:1:db:demo-orders") == [
            "arn:aws:rds:eu-west-2:1:db:demo-orders is not the ARN of a parameter group"
        ]
        assert attach_problems(
            session, console_config, load_resource(session, console_config, cluster_group_arn), sibling_arn
        ) == ["demo-aurora-pg16-tuned is a parameter_group; only instances and clusters have a group"]


def test_expected_impact_of_an_edit_flags_only_static_parameters_that_would_change(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    context = build_context(group_arn)
    dynamic = ParameterGroupEdit(group_arn, {"work_mem": "65536"}, reset=["log_min_duration_statement"])
    assert dynamic.expected_impact(context).requires_reboot is False
    assert dynamic.expected_impact(context).downtime == "none"
    static = ParameterGroupEdit(group_arn, {"max_connections": "200"}, reset=[])
    impact = static.expected_impact(context)
    assert impact.requires_reboot is True and "demo-orders" in impact.downtime and "reboot" in impact.downtime
    unchanged = ParameterGroupEdit(group_arn, {"shared_buffers": "{DBInstanceClassMemory/32768}"}, reset=[])
    assert unchanged.expected_impact(context).requires_reboot is False  # the recorded value is that already
    static_reset = ParameterGroupEdit(group_arn, {}, reset=["shared_buffers"])
    assert static_reset.expected_impact(context).requires_reboot is True


def test_expected_impact_of_a_create_and_an_attach(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    context = build_context("")
    dynamic = ParameterGroupCreate(
        TARGET_KEY, "dyn-pg16", "instance", "postgres16", "d", {"work_mem": "1024"}
    )
    static = ParameterGroupCreate(
        TARGET_KEY, "static-pg16", "instance", "postgres16", "d", {"max_connections": "200"}
    )
    assert dynamic.expected_impact(context).requires_reboot is False
    assert static.expected_impact(context).requires_reboot is True
    attach_now = ParameterGroupAttach("arn:x", "arn:y", "now").expected_impact(context)
    attach_later = ParameterGroupAttach("arn:x", "arn:y", "none").expected_impact(context)
    assert attach_now.requires_reboot and attach_now.downtime == "a reboot now"
    assert not attach_later.requires_reboot and "next reboot" in attach_later.downtime


def test_parameter_problems_can_leave_unknown_names_to_aws(session_factory) -> None:
    metadata = _metadata(session_factory, "postgres16")
    values = {"aws.only_parameter": "1", "work_mem": "abc"}
    strict = parameter_problems(metadata, values, ["another.aws.only"])
    lenient = parameter_problems(metadata, values, ["another.aws.only"], check_unknown_names=False)
    assert {p.message for p in strict} == {
        "aws.only_parameter is not a parameter of postgres16",
        "work_mem must be an integer",
        "another.aws.only is not a parameter of postgres16",
    }
    assert [p.message for p in lenient] == ["work_mem must be an integer"]


def test_attach_renders_a_reference_to_a_rendered_group_and_a_literal_for_any_other(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    tuned_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    replica_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders-replica")
    base = _input(session_factory, console_config, sandbox_snapshot)
    referenced = ParameterGroupAttach(replica_arn, tuned_arn, "none").apply_to_render_input(base)
    replica = next(i for i in referenced.instances if i.identifier == "demo-orders-replica")
    assert replica.parameter_group_reference == "aws_db_parameter_group.demo_postgres16_tuned.name"
    other_arn = tuned_arn.replace("demo-postgres16-tuned", "somewhere-else")
    literal = ParameterGroupAttach(orders_arn, other_arn, "none").apply_to_render_input(base)
    orders = next(i for i in literal.instances if i.identifier == "demo-orders")
    assert orders.parameter_group_reference == '"somewhere-else"'
    assert next(i for i in base.instances if i.identifier == "demo-orders").parameter_group_reference == (
        "aws_db_parameter_group.demo_postgres16_tuned.name"
    )  # the input that was passed in is not modified


def test_attach_of_a_cluster_sets_the_cluster_parameter_group_reference(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    new_group_arn = aurora_arn.replace(":cluster:demo-aurora", ":cluster-pg:aurora-v2")
    base = _input(session_factory, console_config, sandbox_snapshot)
    rendered = ParameterGroupAttach(aurora_arn, new_group_arn, "none").apply_to_render_input(base)
    cluster = next(c for c in rendered.clusters if c.identifier == "demo-aurora")
    assert cluster.cluster_parameter_group_reference == '"aurora-v2"'


def test_delete_refuses_a_group_in_use(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    context = build_context(group_arn)
    with pytest.raises(ParameterGroupInUse, match="demo-orders"):
        ParameterGroupDelete(group_arn).change_set(context)


def test_delete_of_an_unused_group_names_the_group_and_leaves_it_out_of_the_render(
    session_factory, console_config, sandbox_snapshot, moto_aws, build_context
) -> None:
    from rdscommander.aws.rds_reader import RdsReader
    from rdscommander.inventory.snapshot_writer import discover_estate, write_snapshot

    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="unused-pg16", DBParameterGroupFamily="postgres16", Description="unused"
    )
    with session_factory() as session:
        write_snapshot(session, console_config.target(TARGET_KEY), None, discover_estate(RdsReader(rds)))
        session.commit()
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="unused-pg16")["DBParameterGroups"][0][
        "DBParameterGroupArn"
    ]
    context = build_context(group_arn)
    change = ParameterGroupDelete(group_arn)
    [entry] = change.change_set(context)
    assert entry.address == "aws_db_parameter_group.unused_pg16" and entry.desired is None
    assert entry.current == "postgres16 group"
    base = _input(session_factory, console_config, sandbox_snapshot)
    base.instance_parameter_groups.append(
        base.instance_parameter_groups[0].model_copy(
            update={"terraform_name": "unused_pg16", "name": "unused-pg16"}
        )
    )
    rendered = change.apply_to_render_input(base)
    assert "unused-pg16" not in [g.name for g in rendered.instance_parameter_groups]
    assert "unused-pg16" in [g.name for g in base.instance_parameter_groups]


def test_parameter_problems_names_each_parameter_and_catches_conflicting_requests(
    session_factory,
) -> None:
    with session_factory() as session:
        metadata = load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
    problems = parameter_problems(
        metadata,
        {"work_mem": "abc", "rds.rds_superuser_reserved_connections": "5", "log_statement": "ddl"},
        reset=["log_statement", "no_such_parameter"],
    )
    assert {problem.message for problem in problems} == {
        "work_mem must be an integer",
        "rds.rds_superuser_reserved_connections cannot be modified",
        "log_statement cannot be both set and reset",
        "no_such_parameter is not a parameter of postgres16",
    }
    assert parameter_problems(metadata, {"work_mem": "65536"}, reset=["log_statement"]) == []
    [reset_problem] = parameter_problems(metadata, {}, reset=["rds.rds_superuser_reserved_connections"])
    assert reset_problem.message == "rds.rds_superuser_reserved_connections cannot be modified"


def test_parameter_validation_error_carries_the_problems_and_a_diagnostic_kind(session_factory) -> None:
    with session_factory() as session:
        metadata = load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
    from rdscommander.parameters.metadata import validate_parameters

    problems = validate_parameters(metadata, {"work_mem": "abc"})
    error = ParameterValidationError(problems)
    assert error.diagnostic_kind == "parameter_validation_failed"
    assert error.problems == problems
    assert "work_mem must be an integer" in str(error)
