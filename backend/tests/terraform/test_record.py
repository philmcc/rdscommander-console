import json
from pathlib import Path

import pytest

from rdscommander.terraform.record import (
    RECORD_FILE_NAME,
    DriftAttribute,
    diff_records,
    read_record,
    replace_resource,
    write_record,
)
from rdscommander.terraform.render_input import ImportBlock, RenderedParameter, build_render_input
from rdscommander.terraform.renderer import render_workspace_files, write_workspace_files
from tests.terraform.conftest import TARGET_KEY, all_snapshot_arns


def _input(session_factory, console_config, snapshot):
    with session_factory() as session:
        return build_render_input(
            session,
            console_config.target(TARGET_KEY),
            console_config,
            snapshot,
            all_snapshot_arns(session_factory, snapshot),
            set(),
            include_imports=False,
            simulated_endpoint_url=None,
        )


def test_record_round_trips(session_factory, console_config, sandbox_snapshot, tmp_path: Path) -> None:
    render_input = _input(session_factory, console_config, sandbox_snapshot)
    path = write_record(tmp_path, render_input, sandbox_snapshot.id)
    assert (
        path.name == RECORD_FILE_NAME and json.loads(path.read_text())["snapshot_id"] == sandbox_snapshot.id
    )
    recorded = read_record(tmp_path)
    assert recorded is not None and recorded.render_input == render_input


def test_diff_records_reports_attribute_level_differences_and_ignores_auto_minor_engine_version(
    session_factory, console_config, sandbox_snapshot
) -> None:
    recorded = _input(session_factory, console_config, sandbox_snapshot)
    live = recorded.model_copy(deep=True)
    orders = next(i for i in live.instances if i.identifier == "demo-orders")
    orders.instance_class = "db.r6g.xlarge"
    orders.engine_version = "16.5"
    group = next(g for g in live.instance_parameter_groups if g.name == "demo-postgres16-tuned")
    group.parameters[0].value = "999"
    drift = diff_records(recorded, live)
    assert [d.attribute for d in drift["aws_db_instance.demo_orders"]] == ["instance_class"]
    assert drift["aws_db_parameter_group.demo_postgres16_tuned"][0].attribute.startswith("parameters.")
    assert "aws_rds_cluster.demo_aurora" not in drift


def test_diff_records_ignores_the_source_a_restored_resource_was_created_from(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.terraform.render_input import RestorePointInTime

    live = _input(session_factory, console_config, sandbox_snapshot)
    recorded = live.model_copy(deep=True)
    next(i for i in recorded.instances if i.identifier == "demo-orders").snapshot_identifier = "before-change"
    next(
        c for c in recorded.clusters if c.identifier == "demo-aurora"
    ).restore_to_point_in_time = RestorePointInTime(
        source_identifier="demo-aurora-old", use_latest_restorable_time=True
    )
    assert diff_records(recorded, live) == {}
    replica = next(i for i in recorded.instances if i.identifier == "demo-orders-replica")
    replica.instance_class = "db.r6g.xlarge"
    assert [d.attribute for d in diff_records(recorded, live)["aws_db_instance.demo_orders_replica"]] == [
        "instance_class"
    ]


def test_read_record_returns_none_when_the_workspace_was_never_recorded(tmp_path: Path) -> None:
    assert read_record(tmp_path) is None


def test_write_record_never_keeps_import_blocks(
    session_factory, console_config, sandbox_snapshot, tmp_path: Path
) -> None:
    render_input = _input(session_factory, console_config, sandbox_snapshot)
    render_input.import_blocks.append(
        ImportBlock(to_address="aws_db_instance.demo_orders", import_id="demo-orders")
    )
    write_record(tmp_path, render_input, sandbox_snapshot.id)
    recorded = read_record(tmp_path)
    assert recorded is not None and recorded.render_input.import_blocks == []


def test_write_record_leaves_an_identical_record_untouched(
    session_factory, console_config, sandbox_snapshot, tmp_path: Path
) -> None:
    render_input = _input(session_factory, console_config, sandbox_snapshot)
    path = write_record(tmp_path, render_input, sandbox_snapshot.id)
    first_content = path.read_text()
    write_record(tmp_path, render_input, sandbox_snapshot.id)
    assert path.read_text() == first_content
    write_record(tmp_path, render_input, sandbox_snapshot.id + 1)
    assert json.loads(path.read_text())["snapshot_id"] == sandbox_snapshot.id + 1


def test_write_record_replaces_a_record_that_cannot_be_read(
    session_factory, console_config, sandbox_snapshot, tmp_path: Path
) -> None:
    (tmp_path / RECORD_FILE_NAME).write_text("<<<<<<< merge conflict\n")
    render_input = _input(session_factory, console_config, sandbox_snapshot)
    write_record(tmp_path, render_input, sandbox_snapshot.id)
    recorded = read_record(tmp_path)
    assert recorded is not None and recorded.render_input == render_input


def test_write_workspace_files_writes_the_record_only_when_given_one(
    session_factory, console_config, sandbox_snapshot, tmp_path: Path
) -> None:
    render_input = _input(session_factory, console_config, sandbox_snapshot)
    files = render_workspace_files(render_input, format_binary=None)
    written = write_workspace_files(tmp_path, files)
    assert RECORD_FILE_NAME not in {path.name for path in written}
    assert not (tmp_path / RECORD_FILE_NAME).exists()
    written = write_workspace_files(tmp_path, files, record=render_input, snapshot_id=sandbox_snapshot.id)
    assert written[-1] == tmp_path / RECORD_FILE_NAME
    assert read_record(tmp_path).snapshot_id == sandbox_snapshot.id
    write_workspace_files(tmp_path, files)
    assert (tmp_path / RECORD_FILE_NAME).exists(), "rendering without a record must not delete the record"


def test_diff_records_reports_a_resource_that_exists_on_one_side_only(
    session_factory, console_config, sandbox_snapshot
) -> None:
    recorded = _input(session_factory, console_config, sandbox_snapshot)
    live = recorded.model_copy(deep=True)
    live.instance_parameter_groups = [
        g for g in live.instance_parameter_groups if g.name != "demo-postgres16-tuned"
    ]
    live.instances = [i for i in live.instances if i.identifier != "demo-orders"]
    drift = diff_records(recorded, live)
    assert drift["aws_db_parameter_group.demo_postgres16_tuned"] == [
        DriftAttribute(
            address="aws_db_parameter_group.demo_postgres16_tuned",
            attribute="presence",
            recorded=True,
            live=False,
        )
    ]
    assert drift["aws_db_instance.demo_orders"][0].attribute == "presence"
    assert diff_records(live, recorded)["aws_db_instance.demo_orders"][0].live is True


def test_diff_records_compares_parameters_by_value_and_not_by_apply_method(
    session_factory, console_config, sandbox_snapshot
) -> None:
    recorded = _input(session_factory, console_config, sandbox_snapshot)
    live = recorded.model_copy(deep=True)
    group = next(g for g in live.instance_parameter_groups if g.name == "demo-postgres16-tuned")
    group.parameters[0].apply_method = "pending-reboot"
    assert diff_records(recorded, live) == {}
    group.parameters[0].value = "999"
    [drifted] = diff_records(recorded, live)["aws_db_parameter_group.demo_postgres16_tuned"]
    assert drifted.attribute == f"parameters.{group.parameters[0].name}" and drifted.live == "999"


def test_diff_records_reports_added_and_removed_tags_and_parameters(
    session_factory, console_config, sandbox_snapshot
) -> None:
    recorded = _input(session_factory, console_config, sandbox_snapshot)
    live = recorded.model_copy(deep=True)
    orders = next(i for i in live.instances if i.identifier == "demo-orders")
    orders.tags = {**orders.tags, "owner": "payments"}
    group = next(g for g in live.instance_parameter_groups if g.name == "demo-postgres16-tuned")
    removed = group.parameters.pop()
    group.parameters.append(RenderedParameter(name="log_statement", value="ddl", apply_method="immediate"))
    drift = diff_records(recorded, live)
    assert [(d.attribute, d.recorded, d.live) for d in drift["aws_db_instance.demo_orders"]] == [
        ("tags.owner", None, "payments")
    ]
    assert {
        (d.attribute, d.recorded, d.live) for d in drift["aws_db_parameter_group.demo_postgres16_tuned"]
    } == {
        (f"parameters.{removed.name}", removed.value, None),
        ("parameters.log_statement", None, "ddl"),
    }


def test_engine_version_is_drift_only_when_the_record_does_not_leave_it_to_aws(
    session_factory, console_config, sandbox_snapshot
) -> None:
    recorded = _input(session_factory, console_config, sandbox_snapshot)
    orders_recorded = next(i for i in recorded.instances if i.identifier == "demo-orders")
    assert orders_recorded.auto_minor_version_upgrade
    live = recorded.model_copy(deep=True)
    orders_live = next(i for i in live.instances if i.identifier == "demo-orders")
    orders_live.engine_version = "16.99"
    orders_live.auto_minor_version_upgrade = False
    assert [d.attribute for d in diff_records(recorded, live)["aws_db_instance.demo_orders"]] == [
        "auto_minor_version_upgrade"
    ]
    orders_recorded.auto_minor_version_upgrade = False
    assert {d.attribute for d in diff_records(recorded, live)["aws_db_instance.demo_orders"]} == {
        "engine_version"
    }
    orders_live.auto_minor_version_upgrade = True
    assert {d.attribute for d in diff_records(recorded, live)["aws_db_instance.demo_orders"]} == {
        "engine_version",
        "auto_minor_version_upgrade",
    }


def test_build_render_input_include_all_renders_every_non_default_resource(
    session_factory, console_config, sandbox_snapshot
) -> None:
    arns = all_snapshot_arns(session_factory, sandbox_snapshot)
    with session_factory() as session:
        everything = build_render_input(
            session,
            console_config.target(TARGET_KEY),
            console_config,
            sandbox_snapshot,
            set(),
            set(),
            include_imports=False,
            simulated_endpoint_url=None,
            include_all=True,
        )
        nothing = build_render_input(
            session,
            console_config.target(TARGET_KEY),
            console_config,
            sandbox_snapshot,
            set(),
            set(),
            include_imports=False,
            simulated_endpoint_url=None,
        )
    assert everything == _input(session_factory, console_config, sandbox_snapshot)
    assert not (
        nothing.instances or nothing.clusters or nothing.instance_parameter_groups or nothing.subnet_groups
    )
    rendered_count = (
        len(everything.instances)
        + len(everything.cluster_instances)
        + len(everything.clusters)
        + len(everything.instance_parameter_groups)
        + len(everything.cluster_parameter_groups)
        + len(everything.subnet_groups)
    )
    assert rendered_count == len(arns)


def test_build_render_input_applies_the_overlay_last(
    session_factory, console_config, sandbox_snapshot
) -> None:
    def overlay(render_input):
        render_input.instances[0].instance_class = "db.overlaid"
        return render_input

    with session_factory() as session:
        overlaid = build_render_input(
            session,
            console_config.target(TARGET_KEY),
            console_config,
            sandbox_snapshot,
            all_snapshot_arns(session_factory, sandbox_snapshot),
            set(),
            include_imports=False,
            simulated_endpoint_url=None,
            overlay=overlay,
        )
    assert overlaid.instances[0].instance_class == "db.overlaid"


def test_replace_resource_takes_only_the_chosen_resource_from_the_source(
    session_factory, console_config, sandbox_snapshot
) -> None:
    recorded = _input(session_factory, console_config, sandbox_snapshot)
    live = recorded.model_copy(deep=True)
    next(i for i in live.instances if i.identifier == "demo-orders").instance_class = "db.r6g.xlarge"
    next(g for g in live.instance_parameter_groups if g.name == "demo-postgres16-tuned").parameters[
        0
    ].value = "1"
    replaced = replace_resource(recorded, "aws_db_instance.demo_orders", live)
    assert (
        next(i for i in replaced.instances if i.identifier == "demo-orders").instance_class == "db.r6g.xlarge"
    )
    assert replaced.instance_parameter_groups == recorded.instance_parameter_groups, (
        "other resources keep theirs"
    )
    assert [i.identifier for i in replaced.instances] == [i.identifier for i in recorded.instances]
    assert (
        next(i for i in recorded.instances if i.identifier == "demo-orders").instance_class != "db.r6g.xlarge"
    )
    assert replaced.instances[0] is not live.instances[0], "the result shares nothing with its inputs"
    assert set(diff_records(recorded, replaced)) == {"aws_db_instance.demo_orders"}


@pytest.mark.parametrize(
    ("address", "section"),
    [
        ("aws_db_parameter_group.demo_postgres16_tuned", "instance_parameter_groups"),
        ("aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned", "cluster_parameter_groups"),
        ("aws_db_subnet_group.demo_private_subnets", "subnet_groups"),
        ("aws_rds_cluster.demo_aurora", "clusters"),
        ("aws_rds_cluster_instance.demo_aurora_writer", "cluster_instances"),
        ("aws_db_instance.demo_orders", "instances"),
    ],
)
def test_replace_resource_removes_a_resource_the_source_lacks_and_adds_it_back(
    session_factory, console_config, sandbox_snapshot, address: str, section: str
) -> None:
    full = _input(session_factory, console_config, sandbox_snapshot)
    terraform_name = address.split(".")[1]
    without = full.model_copy(deep=True)
    getattr(without, section)[:] = [
        r for r in getattr(without, section) if r.terraform_name != terraform_name
    ]
    assert len(getattr(without, section)) == len(getattr(full, section)) - 1
    assert replace_resource(full, address, without) == without, "vanished: removed from the record"
    assert replace_resource(without, address, full) == full, "only in the source: added, in order"


def test_replace_resource_without_the_resource_on_either_side_changes_nothing_and_rejects_bad_addresses(
    session_factory, console_config, sandbox_snapshot
) -> None:
    recorded = _input(session_factory, console_config, sandbox_snapshot)
    assert replace_resource(recorded, "aws_db_instance.not_there", recorded) == recorded
    with pytest.raises(ValueError, match="not the address of a resource the renderer produces"):
        replace_resource(recorded, "aws_s3_bucket.logs", recorded)
    with pytest.raises(ValueError, match="not the address"):
        replace_resource(recorded, "demo_orders", recorded)


def test_accepting_live_drift_on_a_restored_resource_keeps_the_source_it_was_restored_from(
    session_factory, console_config, sandbox_snapshot
) -> None:
    """AWS never reports a restore source; dropping it from the files would make Terraform plan a replace."""
    from rdscommander.terraform.render_input import RestorePointInTime

    live = _input(session_factory, console_config, sandbox_snapshot)
    recorded = live.model_copy(deep=True)
    point_in_time = RestorePointInTime(
        source_identifier="demo-orders-old", restore_time_utc="2026-10-01T03:00:00Z"
    )
    next(
        i for i in recorded.instances if i.identifier == "demo-orders"
    ).restore_to_point_in_time = point_in_time
    next(
        c for c in recorded.clusters if c.identifier == "demo-aurora"
    ).snapshot_identifier = "demo-aurora-before"
    next(i for i in live.instances if i.identifier == "demo-orders").instance_class = "db.r6g.xlarge"
    next(c for c in live.clusters if c.identifier == "demo-aurora").deletion_protection = False

    accepted = replace_resource(recorded, "aws_db_instance.demo_orders", live)
    accepted = replace_resource(accepted, "aws_rds_cluster.demo_aurora", live)

    orders = next(i for i in accepted.instances if i.identifier == "demo-orders")
    assert orders.instance_class == "db.r6g.xlarge" and orders.restore_to_point_in_time == point_in_time
    aurora = next(c for c in accepted.clusters if c.identifier == "demo-aurora")
    assert aurora.deletion_protection is False and aurora.snapshot_identifier == "demo-aurora-before"
    files = render_workspace_files(accepted, format_binary=None)
    assert 'source_db_instance_identifier = "demo-orders-old"' in files["instances.tf"]
    assert 'snapshot_identifier          = "demo-aurora-before"' in files["clusters.tf"]
    assert diff_records(accepted, live) == {}, "the kept source is not drift"
