import boto3
import pytest

from rdscommander.operations.change_models import ApplyType
from rdscommander.parameters import direct_api
from rdscommander.parameters.direct_api import (
    ATTRIBUTE_APPLY_PROFILES,
    CLUSTER_ATTRIBUTES,
    CLUSTER_INSTANCE_ATTRIBUTES,
    GROUP_REFERENCE_ATTRIBUTE_BY_TYPE,
    INSTANCE_ATTRIBUTES,
    TERMINAL_RESOURCE_STATUSES,
    UnsupportedRestoreError,
    apply_attribute_values,
    apply_profile_for,
    ensure_attributes_restorable,
    read_attribute_values,
    read_pending_modifications,
    read_resource_status,
    resource_modify_request,
)
from tests.terraform.conftest import arn_for

GROUP = "demo-postgres16-tuned"
GROUP_ADDRESS = "aws_db_parameter_group.demo_postgres16_tuned"
ORDERS_ADDRESS = "aws_db_instance.demo_orders"
CLUSTER_ADDRESS = "aws_rds_cluster.demo_aurora"
CLUSTER_GROUP_ADDRESS = "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned"


def _rds():
    return boto3.client("rds", region_name="eu-west-2")


def test_parameter_values_are_written_pending_reboot_and_read_back(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, GROUP))
        submitted = apply_attribute_values(context, GROUP_ADDRESS, {"parameters.work_mem": "50000"})
        assert submitted == {"parameters.work_mem": "50000"}
        stored = {
            p["ParameterName"]: p
            for p in _rds().describe_db_parameters(DBParameterGroupName=GROUP)["Parameters"]
        }
        assert stored["work_mem"]["ParameterValue"] == "50000"
        assert stored["work_mem"]["ApplyMethod"] == "pending-reboot"
        live = read_attribute_values(
            context,
            GROUP_ADDRESS,
            ["parameters.work_mem", "parameters.shared_buffers", "parameters.never_set"],
        )
        assert live == {
            "parameters.work_mem": "50000",
            "parameters.shared_buffers": "{DBInstanceClassMemory/32768}",
            "parameters.never_set": None,
        }


def test_more_than_twenty_parameters_are_written_in_batches(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    values = {f"parameters.test_parameter_{index:02d}": str(index) for index in range(45)}
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, GROUP))
        apply_attribute_values(context, GROUP_ADDRESS, values)
        assert read_attribute_values(context, GROUP_ADDRESS, list(values)) == values


def test_cluster_parameter_group_values_use_the_cluster_api(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    with session_factory() as session:
        context = make_step_context(
            session, arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")
        )
        apply_attribute_values(
            context, CLUSTER_GROUP_ADDRESS, {"parameters.log_min_duration_statement": "2000"}
        )
        live = read_attribute_values(
            context,
            CLUSTER_GROUP_ADDRESS,
            ["parameters.log_min_duration_statement", "parameters.rds.force_ssl"],
        )
        assert live == {"parameters.log_min_duration_statement": "2000", "parameters.rds.force_ssl": "1"}


def test_resetting_a_parameter_to_its_default_is_not_supported_yet(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, GROUP))
        with pytest.raises(UnsupportedRestoreError) as raised:
            apply_attribute_values(context, GROUP_ADDRESS, {"parameters.work_mem": None})
        assert raised.value.attribute == "parameters.work_mem"
        assert read_attribute_values(context, GROUP_ADDRESS, ["parameters.work_mem"]) == {
            "parameters.work_mem": "16384"
        }


def test_instance_attributes_are_modified_immediately_and_read_back(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
        values = {
            "backup_retention_period": 9,
            "deletion_protection": True,
            "preferred_backup_window": "03:00-04:00",
            "auto_minor_version_upgrade": False,
            "monitoring_interval": 0,
        }
        assert apply_attribute_values(context, ORDERS_ADDRESS, values) == values
        assert read_attribute_values(context, ORDERS_ADDRESS, list(values)) == values
        description = _rds().describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"][0]
        assert description["BackupRetentionPeriod"] == 9 and description["DeletionProtection"] is True


def test_instance_tags_are_added_changed_and_removed(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        context = make_step_context(session, orders_arn)
        apply_attribute_values(context, ORDERS_ADDRESS, {"tags.team": "data", "tags.cost-centre": "42"})
        assert read_attribute_values(
            context, ORDERS_ADDRESS, ["tags.team", "tags.cost-centre", "tags.gone"]
        ) == {
            "tags.team": "data",
            "tags.cost-centre": "42",
            "tags.gone": None,
        }
        apply_attribute_values(context, ORDERS_ADDRESS, {"tags.team": None})
        assert read_attribute_values(context, ORDERS_ADDRESS, ["tags.team"]) == {"tags.team": None}
        tags = {
            t["Key"]: t["Value"] for t in _rds().list_tags_for_resource(ResourceName=orders_arn)["TagList"]
        }
        assert "team" not in tags and tags["cost-centre"] == "42"


def test_a_parameter_group_reference_is_resolved_through_the_record_and_read_back_the_same_way(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    rds = _rds()
    rds.create_db_parameter_group(
        DBParameterGroupName="other-group", DBParameterGroupFamily="postgres16", Description="another group"
    )
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders", DBParameterGroupName="other-group", ApplyImmediately=True
    )
    reference = f"{GROUP_ADDRESS}.name"
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
        assert read_attribute_values(context, ORDERS_ADDRESS, ["parameter_group_reference"]) == {
            "parameter_group_reference": '"other-group"'
        }
        apply_attribute_values(context, ORDERS_ADDRESS, {"parameter_group_reference": reference})
        assert read_attribute_values(context, ORDERS_ADDRESS, ["parameter_group_reference"]) == {
            "parameter_group_reference": reference
        }
        apply_attribute_values(context, ORDERS_ADDRESS, {"parameter_group_reference": '"other-group"'})
    description = rds.describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"][0]
    assert description["DBParameterGroups"][0]["DBParameterGroupName"] == "other-group"


def test_cluster_attributes_use_the_cluster_api(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-aurora"))
        values = {"backup_retention_period": 12, "copy_tags_to_snapshot": True}
        apply_attribute_values(context, CLUSTER_ADDRESS, values)
        assert read_attribute_values(context, CLUSTER_ADDRESS, list(values)) == values


@pytest.mark.parametrize(
    ("address", "attribute"),
    [
        (ORDERS_ADDRESS, "publicly_accessible"),
        (ORDERS_ADDRESS, "presence"),
        (ORDERS_ADDRESS, "engine"),
        (GROUP_ADDRESS, "description"),
        (GROUP_ADDRESS, "tags."),
        ("aws_db_subnet_group.demo_private_subnets", "subnet_ids"),
    ],
)
def test_attributes_the_console_cannot_write_are_refused_before_any_call(
    address: str, attribute: str
) -> None:
    with pytest.raises(UnsupportedRestoreError) as raised:
        ensure_attributes_restorable(address, [attribute])
    assert raised.value.attribute == attribute
    assert attribute in str(raised.value)


def test_a_restorable_attribute_list_passes_the_check() -> None:
    ensure_attributes_restorable(ORDERS_ADDRESS, ["instance_class", "tags.team", "parameter_group_reference"])
    ensure_attributes_restorable(GROUP_ADDRESS, ["parameters.work_mem", "tags.team"])
    ensure_attributes_restorable(CLUSTER_ADDRESS, ["cluster_parameter_group_reference", "tags.team"])


def test_every_attribute_the_console_can_write_has_an_apply_profile() -> None:
    writable = {*INSTANCE_ATTRIBUTES, *CLUSTER_INSTANCE_ATTRIBUTES, *CLUSTER_ATTRIBUTES}
    writable |= set(GROUP_REFERENCE_ATTRIBUTE_BY_TYPE.values())
    assert writable == set(ATTRIBUTE_APPLY_PROFILES)


@pytest.mark.parametrize(
    ("attribute", "apply_type", "requires_reboot", "downtime"),
    [
        (
            "instance_class",
            "immediate",
            False,
            "a restart of the instance (Multi-AZ fails over to the standby)",
        ),
        ("allocated_storage", "immediate", False, "none (storage optimisation runs in the background)"),
        ("multi_az", "immediate", False, "none (conversion runs in the background)"),
        (
            "parameter_group_reference",
            "pending-reboot",
            True,
            "a reboot of the instance applies the new parameter group",
        ),
        (
            "cluster_parameter_group_reference",
            "pending-reboot",
            True,
            "a reboot of the instance applies the new parameter group",
        ),
        ("deletion_protection", "immediate", False, "none"),
        ("backup_retention_period", "immediate", False, "none"),
        ("preferred_backup_window", "immediate", False, "none"),
        ("preferred_maintenance_window", "immediate", False, "none"),
        ("tags.team", "immediate", False, "none"),
    ],
)
def test_apply_profiles_state_how_aws_applies_each_attribute(
    attribute: str, apply_type: str, requires_reboot: bool, downtime: str
) -> None:
    address = (
        "aws_rds_cluster.demo_aurora" if attribute == "cluster_parameter_group_reference" else ORDERS_ADDRESS
    )
    profile = apply_profile_for(address, attribute)
    assert (profile.apply_type, profile.requires_reboot, profile.downtime) == (
        apply_type,
        requires_reboot,
        downtime,
    )


def test_parameters_apply_at_the_next_reboot_until_the_family_metadata_says_otherwise() -> None:
    profile = apply_profile_for(GROUP_ADDRESS, "parameters.work_mem")
    assert (profile.apply_type, profile.requires_reboot) == ("pending-reboot", True)
    assert "reboot" in profile.downtime


def test_an_apply_profile_is_refused_for_an_attribute_the_console_cannot_write() -> None:
    with pytest.raises(UnsupportedRestoreError):
        apply_profile_for(ORDERS_ADDRESS, "publicly_accessible")


class _StubRdsClient:
    def __init__(self, description: dict) -> None:
        self.description = description

    def describe_db_instances(self, **kwargs):  # noqa: ANN003, ANN201
        return {"DBInstances": [self.description]}

    def describe_db_clusters(self, **kwargs):  # noqa: ANN003, ANN201
        return {"DBClusters": [self.description]}


def _stub_the_rds_client(monkeypatch, description: dict) -> None:
    monkeypatch.setattr(direct_api, "_rds_client", lambda context, step_name: _StubRdsClient(description))


def test_queued_modifications_are_read_from_the_description_without_the_nothing_ones(
    session_factory, sandbox_snapshot, moto_aws, make_step_context, monkeypatch
) -> None:
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
        assert read_pending_modifications(context, ORDERS_ADDRESS) == {}
        assert read_pending_modifications(context, GROUP_ADDRESS) == {}, "a parameter group queues nothing"
        with monkeypatch.context() as stubbed:
            _stub_the_rds_client(
                stubbed,
                {
                    "PendingModifiedValues": {
                        "DBInstanceClass": "db.r6g.xlarge",
                        "AllocatedStorage": None,
                        "PendingCloudwatchLogsExports": {},
                        "ProcessorFeatures": [],
                    }
                },
            )
            assert read_pending_modifications(context, ORDERS_ADDRESS) == {"DBInstanceClass": "db.r6g.xlarge"}


@pytest.mark.parametrize(
    ("field", "queued_value"),
    [
        ("MultiAZ", False),
        ("BackupRetentionPeriod", 0),
        ("IAMDatabaseAuthenticationEnabled", False),
        ("Port", 0),
        ("DBInstanceClass", "db.r6g.xlarge"),
        ("PendingCloudwatchLogsExports", {"LogTypesToEnable": ["postgresql"]}),
    ],
)
def test_a_queued_false_or_zero_is_a_modification_that_the_restore_would_apply(
    session_factory, sandbox_snapshot, moto_aws, make_step_context, monkeypatch, field, queued_value
) -> None:
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
        with monkeypatch.context() as stubbed:
            _stub_the_rds_client(stubbed, {"PendingModifiedValues": {field: queued_value}})
            assert read_pending_modifications(context, ORDERS_ADDRESS) == {field: queued_value}


def test_the_status_comes_from_the_instance_or_the_cluster_and_parameter_groups_have_none(
    session_factory, sandbox_snapshot, moto_aws, make_step_context, monkeypatch
) -> None:
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
        assert read_resource_status(context, ORDERS_ADDRESS) == "available"
        assert read_resource_status(context, GROUP_ADDRESS) is None
        with monkeypatch.context() as stubbed:
            _stub_the_rds_client(stubbed, {"DBInstanceStatus": "storage-full", "Status": "failed"})
            assert read_resource_status(context, ORDERS_ADDRESS) == "storage-full"
            assert read_resource_status(context, CLUSTER_ADDRESS) == "failed"


def test_the_terminal_statuses_are_the_ones_a_modification_never_recovers_from() -> None:
    assert TERMINAL_RESOURCE_STATUSES == {
        "failed",
        "incompatible-parameters",
        "incompatible-network",
        "incompatible-option-group",
        "incompatible-restore",
        "inaccessible-encryption-credentials",
        "storage-full",
        "deleting",
        "deleted",
    }
    assert "modifying" not in TERMINAL_RESOURCE_STATUSES and "available" not in TERMINAL_RESOURCE_STATUSES


def test_pending_window_is_an_apply_type_with_its_own_downtime_text():
    assert "pending-window" in ApplyType.__args__
    assert direct_api.PENDING_WINDOW_DOWNTIME == "none now; AWS applies it in the next maintenance window"


@pytest.mark.parametrize(
    ("resource_type", "attribute"),
    [
        ("aws_db_instance", "storage_type"),
        ("aws_db_instance", "iops"),
        ("aws_db_instance", "storage_throughput"),
        ("aws_db_instance", "performance_insights_retention_period"),
        ("aws_db_instance", "monitoring_role_arn"),
        ("aws_rds_cluster_instance", "promotion_tier"),
        ("aws_rds_cluster_instance", "performance_insights_retention_period"),
        ("aws_rds_cluster_instance", "monitoring_role_arn"),
        ("aws_rds_cluster", "serverless_v2_min_capacity"),
        ("aws_rds_cluster", "serverless_v2_max_capacity"),
        ("aws_rds_cluster", "auto_minor_version_upgrade"),
        ("aws_db_instance", "engine_version"),
        ("aws_rds_cluster", "engine_version"),
    ],
)
def test_new_attributes_are_writable_with_a_profile(resource_type, attribute):
    assert attribute in direct_api.MODIFIABLE_ATTRIBUTES_BY_TYPE[resource_type]
    assert attribute in direct_api.ATTRIBUTE_APPLY_PROFILES


def _record_modify_db_cluster_calls(calls: list[dict], monkeypatch) -> None:
    """Record the keyword arguments of every `modify_db_cluster` call the console makes, then pass it on."""
    from botocore.client import BaseClient

    original = BaseClient._make_api_call

    def recording(self, operation_name, api_params):
        if operation_name == "ModifyDBCluster":
            calls.append(dict(api_params))
        return original(self, operation_name, api_params)

    monkeypatch.setattr(BaseClient, "_make_api_call", recording)


def test_serverless_capacities_are_written_as_one_nested_dict_and_read_from_the_nested_key(
    session_factory, sandbox_snapshot, moto_aws, make_step_context, monkeypatch
) -> None:
    calls: list[dict] = []
    _record_modify_db_cluster_calls(calls, monkeypatch)
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-aurora"))
        values = {"serverless_v2_min_capacity": 0.5, "serverless_v2_max_capacity": 4.0}
        apply_attribute_values(context, CLUSTER_ADDRESS, values)
        assert read_attribute_values(context, CLUSTER_ADDRESS, list(values)) == values
    assert len(calls) == 1
    assert calls[0]["ServerlessV2ScalingConfiguration"] == {"MinCapacity": 0.5, "MaxCapacity": 4.0}
    assert "ServerlessV2ScalingConfiguration.MinCapacity" not in calls[0]


@pytest.mark.parametrize("apply_immediately", [True, False])
def test_apply_attribute_values_passes_apply_immediately_to_the_modify_call(
    session_factory, sandbox_snapshot, moto_aws, make_step_context, monkeypatch, apply_immediately
) -> None:
    calls: list[dict] = []
    real_client = direct_api._rds_client

    def spying_client(context, step_name):
        client = real_client(context, step_name)
        original = client.modify_db_instance

        def modify_db_instance(**request):
            calls.append(request)
            return original(**request)

        client.modify_db_instance = modify_db_instance
        return client

    monkeypatch.setattr(direct_api, "_rds_client", spying_client)
    with session_factory() as session:
        context = make_step_context(session, arn_for(session_factory, sandbox_snapshot, "demo-orders"))
        apply_attribute_values(
            context, ORDERS_ADDRESS, {"backup_retention_period": 9}, apply_immediately=apply_immediately
        )
    assert [call["ApplyImmediately"] for call in calls] == [apply_immediately]


def test_queued_modifications_are_named_by_attribute_and_unwritable_fields_are_left_out() -> None:
    queued = direct_api.queued_attribute_values(
        "aws_db_instance", {"BackupRetentionPeriod": 14, "MultiAZ": False, "Port": 5433, "Iops": None}
    )
    assert queued == {"backup_retention_period": 14, "multi_az": False}
    assert direct_api.queued_attribute_values("aws_db_parameter_group", {"BackupRetentionPeriod": 1}) == {}


def test_tags_on_a_parameter_group_are_written_and_read_through_its_arn(
    session_factory, sandbox_snapshot, moto_aws, make_step_context
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    boto3.client("rds", region_name="eu-west-2").add_tags_to_resource(
        ResourceName=group_arn, Tags=[{"Key": "rdscommander:managed", "Value": "true"}]
    )
    with session_factory() as session:
        context = make_step_context(session, group_arn)
        values = {"tags.team": "orders", "parameters.work_mem": "16384"}
        apply_attribute_values(context, GROUP_ADDRESS, values)
        assert read_attribute_values(context, GROUP_ADDRESS, ["tags.team", "parameters.work_mem"]) == values
        apply_attribute_values(context, GROUP_ADDRESS, {"tags.team": None})
        assert read_attribute_values(context, GROUP_ADDRESS, ["tags.team"]) == {"tags.team": None}
    live_tags = {
        tag["Key"]: tag["Value"]
        for tag in boto3.client("rds", region_name="eu-west-2").list_tags_for_resource(
            ResourceName=group_arn
        )["TagList"]
    }
    assert "team" not in live_tags and live_tags["rdscommander:managed"]


@pytest.mark.parametrize("resource_type", sorted(direct_api.MODIFIABLE_ATTRIBUTES_BY_TYPE))
def test_every_attribute_the_console_can_write_is_a_field_of_the_rendered_model(resource_type) -> None:
    # Terraform mode writes an attribute change into the rendered model: an attribute the API can write
    # but the model lacks fails there with a raw validation error.
    from rdscommander.terraform.render_input import (
        RenderedCluster,
        RenderedClusterInstance,
        RenderedInstance,
    )

    rendered_model_by_type = {
        "aws_db_instance": RenderedInstance,
        "aws_rds_cluster_instance": RenderedClusterInstance,
        "aws_rds_cluster": RenderedCluster,
    }
    missing = set(direct_api.MODIFIABLE_ATTRIBUTES_BY_TYPE[resource_type]) - set(
        rendered_model_by_type[resource_type].model_fields
    )
    assert not missing, f"{resource_type} writes {sorted(missing)} but its rendered model has no such field"


def test_an_engine_version_is_written_on_instances_and_clusters_but_never_on_an_aurora_member() -> None:
    # Aurora members run the version of their cluster; a minor upgrade restarts the instance.
    assert "engine_version" not in CLUSTER_INSTANCE_ATTRIBUTES
    assert resource_modify_request("aws_db_instance", {"engine_version": "16.6"}) == {"EngineVersion": "16.6"}
    assert resource_modify_request("aws_rds_cluster", {"engine_version": "16.6"}) == {"EngineVersion": "16.6"}
    profile = ATTRIBUTE_APPLY_PROFILES["engine_version"]
    assert (
        profile.apply_type,
        profile.requires_reboot,
        profile.downtime,
        profile.honours_apply_immediately,
    ) == (
        "immediate",
        False,
        "a restart of the instance (Multi-AZ fails over)",
        True,
    )


def test_an_aurora_member_takes_a_maintenance_window_of_its_own() -> None:
    assert "preferred_maintenance_window" in CLUSTER_INSTANCE_ATTRIBUTES
    profile = apply_profile_for("aws_rds_cluster_instance.demo_aurora_writer", "preferred_maintenance_window")
    assert (profile.apply_type, profile.requires_reboot, profile.downtime) == ("immediate", False, "none")
