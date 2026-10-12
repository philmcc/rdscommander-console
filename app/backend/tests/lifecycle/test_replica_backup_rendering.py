"""A read replica's automated backups are rendered, so a Terraform-mode backup change on it changes AWS."""

from __future__ import annotations

import re

from rdscommander.operations.enums import OperationState
from rdscommander.terraform.record import read_record
from tests.drift.test_detect_executor import REPLICA_ADDRESS, _adopt_estate
from tests.operations.test_change_strategy import _workspace_dir
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _git

REPLICA = "demo-orders-replica"
INSTANCES_FILE = "terraform/sandbox/000000000000/eu-west-2/instances.tf"


def _replica_block(instances_tf: str) -> str:
    return instances_tf[instances_tf.index('"demo_orders_replica"') :].split("\n}\n")[0]


def test_a_terraform_backup_change_on_a_read_replica_renders_a_change_to_the_replica(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update = {
        "resource_changes": [
            {
                "address": REPLICA_ADDRESS,
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"backup_retention_period": 1, "backup_window": None},
                    "after": {"backup_retention_period": 14, "backup_window": "02:00-03:00"},
                },
            }
        ]
    }
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    runner.services.terraform_runner = fake
    replica_arn = arn_for(session_factory, sandbox_snapshot, REPLICA)
    operation = service.create(
        "backup_settings_change",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"backup_retention_period_days": 14, "preferred_backup_window": "02:00-03:00"},
            "resource_arn": replica_arn,
        },
        "replica-backups-tf",
        "phil",
        resource_arn=replica_arn,
        title=f"Backup settings: {REPLICA}",
    )
    committed_block = _replica_block(_git("show", f"HEAD:{INSTANCES_FILE}", cwd=operator_settings.repo_root))
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    rendered_block = _replica_block((_workspace_dir(operator_settings) / "instances.tf").read_text())
    assert re.search(r"backup_retention_period\s+=\s+14", rendered_block)
    assert re.search(r'backup_window\s+=\s+"02:00-03:00"', rendered_block)
    assert rendered_block != committed_block, "the files the plan reads change the replica's backups"
    assert "replicate_source_db" in rendered_block
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert fake.calls.count("apply") == 1
    recorded = next(
        instance
        for instance in read_record(_workspace_dir(operator_settings)).render_input.instances
        if instance.identifier == REPLICA
    )
    assert (recorded.backup_retention_period, recorded.preferred_backup_window) == (14, "02:00-03:00")
    head_block = _replica_block(_git("show", f"HEAD:{INSTANCES_FILE}", cwd=operator_settings.repo_root))
    assert re.search(r"backup_retention_period\s+=\s+14", head_block), "the record and AWS agree"
