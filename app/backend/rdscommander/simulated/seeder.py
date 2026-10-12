"""Idempotently create the simulated estate in moto."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from botocore.client import BaseClient
from botocore.exceptions import ClientError

from rdscommander.logging_setup import get_logger
from rdscommander.simulated.estate import SimulatedEstate, SimulatedParameterGroup, SimulatedSecurityGroup

logger = get_logger(__name__)
SIMULATED_MASTER_PASSWORD = "simulated-password-1"  # noqa: S105 - the simulated account has no real credentials
VPC_NAME_TAG = "rdscommander-simulated"


@dataclass
class SeedReport:
    """What the seeder created versus found.

    Attributes:
        created: `<kind>:<name>` of every resource the seeder created.
        existing: `<kind>:<name>` of every resource it found and left alone.
        security_group_ids: `{name: sg-id}` of the estate's security groups, created or found, so a
            walkthrough or a create operation can name one.
    """

    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    security_group_ids: dict[str, str] = field(default_factory=dict)


def _exists(call: Callable[..., Any], **kwargs: Any) -> bool:
    """True when a describe call returns the resource.

    Real AWS raises a NotFound fault; the simulator may instead answer with an
    empty list, so both shapes are handled.
    """
    try:
        response = call(**kwargs)
    except ClientError as error:
        if "NotFound" in error.response["Error"]["Code"]:
            return False
        raise
    listings = [v for k, v in response.items() if k != "ResponseMetadata" and isinstance(v, list)]
    return bool(listings) and any(listings)


def _parameter_entries(group: SimulatedParameterGroup) -> list[dict[str, str]]:
    # `Source` is part of the API shape and ignored by AWS, but the simulator stores it verbatim and the
    # Terraform provider keeps only parameters whose source is `user`, so set it explicitly here.
    return [
        {"ParameterName": p.name, "ParameterValue": p.value, "ApplyMethod": p.apply_method, "Source": "user"}
        for p in group.parameters
    ]


def _ensure_network(ec2: BaseClient, estate: SimulatedEstate, report: SeedReport) -> tuple[str, list[str]]:
    """The tagged VPC and its subnets, created when missing; returns the VPC id and the subnet ids."""
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [VPC_NAME_TAG]}])["Vpcs"]
    if vpcs:
        vpc_id = vpcs[0]["VpcId"]
        report.existing.append(f"vpc:{vpc_id}")
    else:
        vpc_id = ec2.create_vpc(
            CidrBlock=estate.vpc_cidr_block,
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": VPC_NAME_TAG}]}],
        )["Vpc"]["VpcId"]
        report.created.append(f"vpc:{vpc_id}")
    subnet_ids: list[str] = []
    zones = [z["ZoneName"] for z in ec2.describe_availability_zones()["AvailabilityZones"]]
    for index, cidr in enumerate(estate.subnet_cidr_blocks):
        existing = ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "cidr-block", "Values": [cidr]}]
        )["Subnets"]
        if existing:
            subnet_ids.append(existing[0]["SubnetId"])
            report.existing.append(f"subnet:{cidr}")
        else:
            subnet = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=zones[index % len(zones)]
            )
            subnet_ids.append(subnet["Subnet"]["SubnetId"])
            report.created.append(f"subnet:{cidr}")
    return vpc_id, subnet_ids


def _ensure_security_group(
    ec2: BaseClient, vpc_id: str, vpc_cidr_block: str, group: SimulatedSecurityGroup, report: SeedReport
) -> None:
    """The security group in the VPC, created with its PostgreSQL rule when missing; its id is reported."""
    existing = ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "group-name", "Values": [group.name]}]
    )["SecurityGroups"]
    if existing:
        report.security_group_ids[group.name] = existing[0]["GroupId"]
        report.existing.append(f"security_group:{group.name}")
        return
    group_id = ec2.create_security_group(GroupName=group.name, Description=group.description, VpcId=vpc_id)[
        "GroupId"
    ]
    ec2.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": group.postgres_port,
                "ToPort": group.postgres_port,
                "IpRanges": [{"CidrIp": vpc_cidr_block, "Description": "PostgreSQL from the VPC"}],
            }
        ],
    )
    report.security_group_ids[group.name] = group_id
    report.created.append(f"security_group:{group.name}")


def seed_simulated_estate(rds: BaseClient, ec2: BaseClient, estate: SimulatedEstate) -> SeedReport:
    """Create every missing resource of `estate`; existing ones are left untouched.

    Args:
        rds: RDS client of the simulated account.
        ec2: EC2 client of the simulated account.
        estate: What to create.

    Returns:
        What was created and found, with the security groups' ids.

    Example:
        >>> report = seed_simulated_estate(rds, ec2, load_simulated_estate(config_dir))  # doctest: +SKIP
        >>> report.security_group_ids  # doctest: +SKIP
        {'demo-db-access': 'sg-0123456789abcdef0'}
    """
    report = SeedReport()
    vpc_id, subnet_ids = _ensure_network(ec2, estate, report)
    for subnet_group in estate.subnet_groups:
        if _exists(rds.describe_db_subnet_groups, DBSubnetGroupName=subnet_group.name):
            report.existing.append(f"subnet_group:{subnet_group.name}")
        else:
            rds.create_db_subnet_group(
                DBSubnetGroupName=subnet_group.name,
                DBSubnetGroupDescription=subnet_group.description,
                SubnetIds=subnet_ids,
            )
            report.created.append(f"subnet_group:{subnet_group.name}")
    for group in estate.instance_parameter_groups:
        if _exists(rds.describe_db_parameter_groups, DBParameterGroupName=group.name):
            report.existing.append(f"parameter_group:{group.name}")
        else:
            rds.create_db_parameter_group(
                DBParameterGroupName=group.name,
                DBParameterGroupFamily=group.family,
                Description=group.description,
            )
            if group.parameters:
                rds.modify_db_parameter_group(
                    DBParameterGroupName=group.name, Parameters=_parameter_entries(group)
                )
            report.created.append(f"parameter_group:{group.name}")
    for group in estate.cluster_parameter_groups:
        if _exists(rds.describe_db_cluster_parameter_groups, DBClusterParameterGroupName=group.name):
            report.existing.append(f"cluster_parameter_group:{group.name}")
        else:
            rds.create_db_cluster_parameter_group(
                DBClusterParameterGroupName=group.name,
                DBParameterGroupFamily=group.family,
                Description=group.description,
            )
            if group.parameters:
                rds.modify_db_cluster_parameter_group(
                    DBClusterParameterGroupName=group.name, Parameters=_parameter_entries(group)
                )
            report.created.append(f"cluster_parameter_group:{group.name}")
    for cluster in estate.clusters:
        if _exists(rds.describe_db_clusters, DBClusterIdentifier=cluster.identifier):
            report.existing.append(f"cluster:{cluster.identifier}")
        else:
            arguments: dict[str, Any] = {
                "DBClusterIdentifier": cluster.identifier,
                "Engine": "aurora-postgresql",
                "EngineVersion": cluster.engine_version,
                "MasterUsername": cluster.master_username,
                "MasterUserPassword": SIMULATED_MASTER_PASSWORD,
            }
            if cluster.database_name:
                arguments["DatabaseName"] = cluster.database_name
            if cluster.cluster_parameter_group_name:
                arguments["DBClusterParameterGroupName"] = cluster.cluster_parameter_group_name
            if cluster.subnet_group_name:
                arguments["DBSubnetGroupName"] = cluster.subnet_group_name
            rds.create_db_cluster(**arguments)
            report.created.append(f"cluster:{cluster.identifier}")
        for member in cluster.instances:
            if _exists(rds.describe_db_instances, DBInstanceIdentifier=member.identifier):
                report.existing.append(f"instance:{member.identifier}")
            else:
                rds.create_db_instance(
                    DBInstanceIdentifier=member.identifier,
                    DBClusterIdentifier=cluster.identifier,
                    Engine="aurora-postgresql",
                    DBInstanceClass=member.instance_class,
                    PromotionTier=member.promotion_tier,
                )
                report.created.append(f"instance:{member.identifier}")
    for instance in estate.instances:
        if _exists(rds.describe_db_instances, DBInstanceIdentifier=instance.identifier):
            report.existing.append(f"instance:{instance.identifier}")
            continue
        if instance.read_replica_of:
            rds.create_db_instance_read_replica(
                DBInstanceIdentifier=instance.identifier,
                SourceDBInstanceIdentifier=instance.read_replica_of,
                DBInstanceClass=instance.instance_class,
            )
        else:
            arguments = {
                "DBInstanceIdentifier": instance.identifier,
                "Engine": "postgres",
                "DBInstanceClass": instance.instance_class,
                "MasterUsername": instance.master_username or "demo_admin",
                "MasterUserPassword": SIMULATED_MASTER_PASSWORD,
                "AllocatedStorage": instance.allocated_storage or 20,
                "MultiAZ": instance.multi_az,
            }
            if instance.engine_version:
                arguments["EngineVersion"] = instance.engine_version
            if instance.parameter_group_name:
                arguments["DBParameterGroupName"] = instance.parameter_group_name
            if instance.subnet_group_name:
                arguments["DBSubnetGroupName"] = instance.subnet_group_name
            rds.create_db_instance(**arguments)
        report.created.append(f"instance:{instance.identifier}")
    # Security groups come last so that the ids of everything above stay as they were before they existed.
    for security_group in estate.security_groups:
        _ensure_security_group(ec2, vpc_id, estate.vpc_cidr_block, security_group, report)
    logger.info(
        "simulated_estate_seeded", created_count=len(report.created), existing_count=len(report.existing)
    )
    return report
