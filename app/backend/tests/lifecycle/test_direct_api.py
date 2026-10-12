"""Direct RDS calls of the lifecycle operations."""
# ruff: noqa: F811

from __future__ import annotations

import boto3

from rdscommander.lifecycle.direct_api import (
    cluster_status,
    create_snapshot,
    delete_snapshot,
    failover_cluster,
    orderable_instance_classes,
    snapshot_status,
)
from tests.drift.conftest import direct_mode_runner  # noqa: F401
from tests.parameters.conftest import make_step_context  # noqa: F401
from tests.terraform.conftest import arn_for


def test_orderable_instance_classes_are_sorted_unique_and_start_with_db(moto_aws):
    rds = boto3.client("rds", region_name="eu-west-2")
    classes = orderable_instance_classes(rds, "aurora-postgresql", "11.9")
    assert len(classes) > 0
    assert classes == sorted(set(classes))
    assert all(name.startswith("db.") for name in classes)


def test_orderable_instance_classes_are_empty_for_an_engine_the_simulator_lacks(moto_aws):
    rds = boto3.client("rds", region_name="eu-west-2")
    assert orderable_instance_classes(rds, "postgres", "16.4") == []


def test_instance_snapshots_are_created_tagged_polled_and_deleted(
    session_factory,
    sandbox_snapshot,
    make_step_context,
    moto_aws,
):
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
        arn = create_snapshot(context, "instance", "demo-orders", "direct-api-one", {"reason": "test"})
        assert arn.endswith(":snapshot:direct-api-one")
        assert snapshot_status(context, "instance", "direct-api-one") == "available"
        delete_snapshot(context, "instance", "direct-api-one")
        assert snapshot_status(context, "instance", "direct-api-one") is None


def test_cluster_snapshots_use_the_cluster_calls_and_report_none_when_gone(
    session_factory,
    sandbox_snapshot,
    make_step_context,
    moto_aws,
):
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-aurora"))
        arn = create_snapshot(context, "cluster", "demo-aurora", "direct-api-cluster", {})
        assert arn.endswith(":cluster-snapshot:direct-api-cluster")
        assert snapshot_status(context, "cluster", "direct-api-cluster") == "available"
        delete_snapshot(context, "cluster", "direct-api-cluster")
        assert snapshot_status(context, "cluster", "direct-api-cluster") is None


def test_cluster_status_names_the_writer_and_failover_moves_it(
    session_factory,
    sandbox_snapshot,
    make_step_context,
    moto_aws,
):
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-aurora"))
        assert cluster_status(context, "demo-aurora") == ("available", "demo-aurora-writer")
        failover_cluster(context, "demo-aurora", "demo-aurora-reader")
        assert cluster_status(context, "demo-aurora") == ("available", "demo-aurora-reader")
