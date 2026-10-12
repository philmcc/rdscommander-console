import pytest

from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord, terraform_resource_type_for


def _record(
    kind: ResourceKind, facts: dict | None = None, relationships: dict | None = None
) -> ResourceRecord:
    return ResourceRecord(
        kind=kind,
        arn="arn:aws:rds:eu-west-2:000000000000:db:x",
        resource_id=encode_resource_id("arn:aws:rds:eu-west-2:000000000000:db:x"),
        identifier="x",
        environment_slug="sandbox",
        target_key="sandbox/000000000000/eu-west-2",
        ownership="unmanaged",
        status="available",
        snapshot_id=1,
        facts=facts or {},
        relationships=relationships or {},
    )


@pytest.mark.parametrize(
    ("kind", "facts", "relationships", "expected_type"),
    [
        (ResourceKind.INSTANCE, {}, {"cluster_identifier": None}, "aws_db_instance"),
        (ResourceKind.INSTANCE, {}, {"cluster_identifier": "demo-aurora"}, "aws_rds_cluster_instance"),
        (ResourceKind.CLUSTER, {}, {}, "aws_rds_cluster"),
        (ResourceKind.PARAMETER_GROUP, {"group_scope": "instance"}, {}, "aws_db_parameter_group"),
        (ResourceKind.PARAMETER_GROUP, {"group_scope": "cluster"}, {}, "aws_rds_cluster_parameter_group"),
        (ResourceKind.SNAPSHOT, {"snapshot_scope": "instance"}, {}, "aws_db_snapshot"),
        (ResourceKind.SNAPSHOT, {"snapshot_scope": "cluster"}, {}, "aws_db_cluster_snapshot"),
    ],
)
def test_terraform_resource_type_follows_kind_and_scope(kind, facts, relationships, expected_type) -> None:
    assert terraform_resource_type_for(_record(kind, facts, relationships)) == expected_type


def test_terraform_resource_type_rejects_environments() -> None:
    with pytest.raises(ValueError, match="no Terraform type"):
        terraform_resource_type_for(_record(ResourceKind.ENVIRONMENT))
