"""The blue/green API wrapper and the deployment rows, against the simulated account."""

from __future__ import annotations

from datetime import datetime

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from rdscommander.control_db.models import BlueGreenDeployment, ExecutionRecord
from rdscommander.lifecycle.direct_api import cluster_describe, instance_describe
from rdscommander.upgrades.blue_green_api import (
    BlueGreenStatus,
    create_deployment,
    delete_deployment,
    describe_deployment,
    is_deployment_not_found,
    mark_deployment_deleted,
    record_deployment,
    switchover,
    update_deployment_status,
)
from tests.terraform.conftest import TARGET_KEY, arn_for

TARGET_ENGINE_VERSION = "17.2"
STEP_NAME = "create_deployment"


def _instance_parameter_group_name(context, identifier: str) -> str:
    instance = instance_describe(context, identifier)
    return instance["DBParameterGroups"][0]["DBParameterGroupName"]


def _recorded_api_names(session, step_name: str) -> list[str]:
    records = session.scalars(
        select(ExecutionRecord).where(ExecutionRecord.step_name == step_name).order_by(ExecutionRecord.id)
    )
    return [record.api_name for record in records]


def _create_deployment_for_orders(session_factory, sandbox_snapshot, make_step_context, session):
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    context = make_step_context(session, orders_arn)
    deployment_id = create_deployment(
        context,
        name="demo-orders-upgrade",
        source_arn=orders_arn,
        target_engine_version=TARGET_ENGINE_VERSION,
        target_parameter_group_name=_instance_parameter_group_name(context, "demo-orders"),
        target_cluster_parameter_group_name=None,
        step_name=STEP_NAME,
    )
    return context, orders_arn, deployment_id


def test_create_deployment_answers_the_identifier_and_records_the_call_under_the_step(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        context, _, deployment_id = _create_deployment_for_orders(
            session_factory, sandbox_snapshot, make_step_context, session
        )
        assert deployment_id.startswith("bgd-")
        assert "rds:CreateBlueGreenDeployment" in _recorded_api_names(session, STEP_NAME)
        request = session.scalars(
            select(ExecutionRecord).where(ExecutionRecord.api_name == "rds:CreateBlueGreenDeployment")
        ).one()
        assert request.request_parameters["TargetEngineVersion"] == TARGET_ENGINE_VERSION
        assert request.request_parameters["BlueGreenDeploymentName"] == "demo-orders-upgrade"
        assert request.operation_id == context.operation.id


def test_describe_deployment_reports_status_green_side_switchover_details_and_tasks(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        context, orders_arn, deployment_id = _create_deployment_for_orders(
            session_factory, sandbox_snapshot, make_step_context, session
        )
        status = describe_deployment(context, deployment_id, step_name="await_green_available")
        assert isinstance(status, BlueGreenStatus)
        assert status.status in {"PROVISIONING", "AVAILABLE"}
        assert status.green_arn is not None and "-green-" in status.green_arn
        assert status.switchover_details[0]["SourceMember"] == orders_arn
        assert status.switchover_details[0]["TargetMember"] == status.green_arn
        assert {task["Name"] for task in status.tasks} >= {"CREATING_READ_REPLICA_OF_SOURCE"}
        assert "rds:DescribeBlueGreenDeployments" in _recorded_api_names(session, "await_green_available")


def test_describe_deployment_reads_the_green_instance_endpoint_when_aws_reports_one(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        context, _, deployment_id = _create_deployment_for_orders(
            session_factory, sandbox_snapshot, make_step_context, session
        )
        status = describe_deployment(context, deployment_id, step_name="await_green_available")
        green_identifier = status.green_arn.rsplit(":", 1)[1]
        green_instance = instance_describe(context, green_identifier)
        assert green_instance["EngineVersion"] == TARGET_ENGINE_VERSION
        assert status.green_endpoint == green_instance["Endpoint"]["Address"]


def test_switchover_completes_the_deployment_and_renames_the_blue_instance(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        context, _, deployment_id = _create_deployment_for_orders(
            session_factory, sandbox_snapshot, make_step_context, session
        )
        switchover(context, deployment_id, timeout_seconds=600, step_name="switchover")
        status = describe_deployment(context, deployment_id, step_name="verify")
        assert status.status == "SWITCHOVER_COMPLETED"
        assert instance_describe(context, "demo-orders-old1") is not None
        assert instance_describe(context, "demo-orders")["EngineVersion"] == TARGET_ENGINE_VERSION
        switchover_request = session.scalars(
            select(ExecutionRecord).where(ExecutionRecord.api_name == "rds:SwitchoverBlueGreenDeployment")
        ).one()
        assert switchover_request.step_name == "switchover"
        assert switchover_request.request_parameters["SwitchoverTimeout"] == 600


def test_delete_deployment_removes_it_and_a_later_describe_is_not_found(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        context, _, deployment_id = _create_deployment_for_orders(
            session_factory, sandbox_snapshot, make_step_context, session
        )
        delete_deployment(context, deployment_id, delete_target=False, step_name="cleanup")
        with pytest.raises(ClientError) as not_found:
            describe_deployment(context, deployment_id, step_name="cleanup")
        assert is_deployment_not_found(not_found.value)
        assert "rds:DeleteBlueGreenDeployment" in _recorded_api_names(session, "cleanup")


def test_delete_deployment_with_delete_target_also_removes_the_green_instance(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        context, _, deployment_id = _create_deployment_for_orders(
            session_factory, sandbox_snapshot, make_step_context, session
        )
        green_identifier = describe_deployment(context, deployment_id, step_name="cleanup").green_arn.rsplit(
            ":", 1
        )[1]
        delete_deployment(context, deployment_id, delete_target=True, step_name="cleanup")
        assert instance_describe(context, green_identifier) is None


def test_describe_of_an_unknown_deployment_is_recognised_as_not_found(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
        with pytest.raises(ClientError) as not_found:
            describe_deployment(context, "bgd-doesnotexist00000", step_name="cleanup")
        assert is_deployment_not_found(not_found.value)


def test_a_cluster_deployment_passes_both_parameter_groups_and_builds_a_green_cluster(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
        context = make_step_context(session, aurora_arn)
        cluster = cluster_describe(context, "demo-aurora")
        deployment_id = create_deployment(
            context,
            name="demo-aurora-upgrade",
            source_arn=aurora_arn,
            target_engine_version=cluster["EngineVersion"],
            target_parameter_group_name="default.aurora-postgresql16",
            target_cluster_parameter_group_name=cluster["DBClusterParameterGroup"],
            step_name=STEP_NAME,
        )
        request = session.scalars(
            select(ExecutionRecord).where(ExecutionRecord.api_name == "rds:CreateBlueGreenDeployment")
        ).one()
        assert (
            request.request_parameters["TargetDBClusterParameterGroupName"]
            == cluster["DBClusterParameterGroup"]
        )
        status = describe_deployment(context, deployment_id, step_name="await_green_available")
        assert ":cluster:" in status.green_arn


def test_an_instance_deployment_does_not_send_a_cluster_parameter_group(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        _create_deployment_for_orders(session_factory, sandbox_snapshot, make_step_context, session)
        request = session.scalars(
            select(ExecutionRecord).where(ExecutionRecord.api_name == "rds:CreateBlueGreenDeployment")
        ).one()
        assert "TargetDBClusterParameterGroupName" not in request.request_parameters


def test_record_deployment_stores_the_row_with_creation_and_update_times(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
        context = make_step_context(session, orders_arn)
        stored = record_deployment(
            session,
            deployment_id="bgd-recorded0000000001",
            operation_id=context.operation.id,
            resource_arn=orders_arn,
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            status="PROVISIONING",
            target_engine_version=TARGET_ENGINE_VERSION,
        )
        session.commit()
        assert stored.created_at_utc == stored.updated_at_utc
        reloaded = session.get(BlueGreenDeployment, "bgd-recorded0000000001")
        assert reloaded.status == "PROVISIONING"
        assert reloaded.green_endpoint is None
        assert reloaded.target_key == TARGET_KEY


def test_update_deployment_status_sets_the_time_the_endpoint_and_the_switchover_moment(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
        context = make_step_context(session, orders_arn)
        stored = record_deployment(
            session,
            deployment_id="bgd-updated00000000001",
            operation_id=context.operation.id,
            resource_arn=orders_arn,
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            status="PROVISIONING",
            target_engine_version=TARGET_ENGINE_VERSION,
        )
        session.commit()
        created_at = stored.created_at_utc

        update_deployment_status(
            session, "bgd-updated00000000001", "AVAILABLE", green_endpoint="green.example"
        )
        session.commit()
        session.refresh(stored)
        assert stored.status == "AVAILABLE"
        assert stored.green_endpoint == "green.example"
        assert stored.updated_at_utc > created_at
        assert stored.switched_over_at_utc is None

        update_deployment_status(session, "bgd-updated00000000001", "SWITCHOVER_IN_PROGRESS")
        session.commit()
        session.refresh(stored)
        assert stored.green_endpoint == "green.example"  # not overwritten by a call without one
        assert stored.switched_over_at_utc is None

        update_deployment_status(session, "bgd-updated00000000001", "SWITCHOVER_COMPLETED")
        session.commit()
        session.refresh(stored)
        assert isinstance(stored.switched_over_at_utc, datetime)
        first_switched_over_at = stored.switched_over_at_utc

        update_deployment_status(session, "bgd-updated00000000001", "SWITCHOVER_COMPLETED")
        session.commit()
        session.refresh(stored)
        assert stored.switched_over_at_utc == first_switched_over_at


def test_mark_deployment_deleted_sets_the_deletion_time_and_status(
    session_factory, sandbox_snapshot, make_step_context, moto_aws
):
    with session_factory() as session:
        orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
        context = make_step_context(session, orders_arn)
        stored = record_deployment(
            session,
            deployment_id="bgd-deleted00000000001",
            operation_id=context.operation.id,
            resource_arn=orders_arn,
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            status="AVAILABLE",
            target_engine_version=TARGET_ENGINE_VERSION,
        )
        session.commit()
        mark_deployment_deleted(session, "bgd-deleted00000000001")
        session.commit()
        session.refresh(stored)
        assert stored.status == "DELETING"
        assert isinstance(stored.deleted_at_utc, datetime)


def test_updating_a_deployment_the_console_never_recorded_raises_a_named_error(session_factory, moto_aws):
    with session_factory() as session, pytest.raises(LookupError, match="bgd-unknown"):
        update_deployment_status(session, "bgd-unknown", "AVAILABLE")
