"""Shared Terraform test helpers: a seeded, snapshotted simulated estate."""

from __future__ import annotations

import boto3
import pytest
from moto.moto_api._internal import mock_random

from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.control_db.models import (
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryParameterGroup,
    InventorySubnetGroup,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.main import build_executors, build_services
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.simulated.estate import load_simulated_estate
from rdscommander.simulated.seeder import seed_simulated_estate
from rdscommander.terraform.render_input import build_render_input

TARGET_KEY = "sandbox/000000000000/eu-west-2"


@pytest.fixture
def sandbox_snapshot(session_factory, operator_settings, console_config, config_store, moto_aws, tmp_path):
    mock_random.seed(20261010)  # deterministic VPC, subnet and security group ids for the golden files
    seed_simulated_estate(
        boto3.client("rds", region_name="eu-west-2"),
        boto3.client("ec2", region_name="eu-west-2"),
        load_simulated_estate(tmp_path),
    )
    sync_configuration_to_control_db(session_factory, console_config)
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    OperationService(session_factory).create("inventory_refresh", "sandbox", None, {}, "render-seed", "phil")
    runner.run_once()
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        assert snapshot is not None
        session.expunge_all()
        return snapshot


def all_snapshot_arns(session_factory, snapshot) -> set[str]:
    """Every adoptable ARN in the snapshot (default parameter groups excluded)."""
    with session_factory() as session:
        arns = {
            r.db_instance_arn for r in session.query(InventoryDbInstance).filter_by(snapshot_id=snapshot.id)
        }
        arns |= {
            r.db_cluster_arn for r in session.query(InventoryDbCluster).filter_by(snapshot_id=snapshot.id)
        }
        arns |= {
            r.parameter_group_arn
            for r in session.query(InventoryParameterGroup).filter_by(
                snapshot_id=snapshot.id, is_default=False
            )
        }
        arns |= {
            r.db_subnet_group_arn
            for r in session.query(InventorySubnetGroup).filter_by(snapshot_id=snapshot.id)
        }
        return arns


def arn_for(session_factory, snapshot, identifier: str) -> str:
    """The ARN in the snapshot whose identifier is `identifier`."""
    return next(a for a in all_snapshot_arns(session_factory, snapshot) if a.endswith(f":{identifier}"))


@pytest.fixture
def sandbox_render_input(session_factory, console_config, sandbox_snapshot):
    """The sandbox estate as renderer input, with no import blocks."""
    target = console_config.target(TARGET_KEY)
    with session_factory() as session:
        return build_render_input(
            session,
            target,
            console_config,
            sandbox_snapshot,
            all_snapshot_arns(session_factory, sandbox_snapshot),
            set(),
            include_imports=False,
            simulated_endpoint_url="http://simulated-aws:5000",
        )
