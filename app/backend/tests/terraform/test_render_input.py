import pytest

from rdscommander.terraform.render_input import RenderedInstance, RestorePointInTime

_MINIMAL_INSTANCE_DICT = {
    "terraform_name": "demo_orders",
    "identifier": "demo-orders",
    "replicate_source_reference": None,
    "engine": "postgres",
    "engine_version": "16.4",
    "instance_class": "db.m6g.large",
    "allocated_storage": 100,
    "max_allocated_storage": None,
    "storage_type": "gp2",
    "iops": None,
    "storage_throughput": None,
    "master_username": "demo_admin",
    "db_name": None,
    "port": 5432,
    "db_subnet_group_reference": None,
    "vpc_security_group_ids": [],
    "parameter_group_reference": None,
    "multi_az": True,
    "publicly_accessible": False,
    "backup_retention_period": 1,
    "preferred_backup_window": None,
    "preferred_maintenance_window": None,
    "deletion_protection": False,
    "storage_encrypted": False,
    "kms_key_id": None,
    "performance_insights_enabled": False,
    "performance_insights_retention_period": None,
    "monitoring_interval": 0,
    "monitoring_role_arn": None,
    "auto_minor_version_upgrade": True,
    "copy_tags_to_snapshot": False,
    "ca_cert_identifier": None,
    "tags": {},
}


def test_rendered_instance_defaults_keep_old_records_loading_and_apply_immediately():
    instance = RenderedInstance.model_validate(_MINIMAL_INSTANCE_DICT)
    assert instance.apply_immediately is True
    assert instance.final_snapshot_identifier is None
    assert instance.snapshot_identifier is None
    assert instance.restore_to_point_in_time is None
    assert instance.manage_master_user_password is False


def test_restore_point_in_time_needs_a_time_or_latest():
    with pytest.raises(ValueError, match="restore_time_utc or use_latest_restorable_time"):
        RestorePointInTime(
            source_identifier="demo-orders", restore_time_utc=None, use_latest_restorable_time=False
        )
