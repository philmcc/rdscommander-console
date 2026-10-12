from pathlib import Path

import boto3

from rdscommander.simulated.estate import load_simulated_estate
from rdscommander.simulated.seeder import seed_simulated_estate


def test_seeding_creates_the_default_estate_and_is_idempotent(moto_aws, tmp_path: Path) -> None:
    estate = load_simulated_estate(tmp_path)
    rds = boto3.client("rds", region_name="eu-west-2")
    ec2 = boto3.client("ec2", region_name="eu-west-2")
    first = seed_simulated_estate(rds, ec2, estate)
    assert "instance:demo-orders" in first.created and "cluster:demo-aurora" in first.created
    second = seed_simulated_estate(rds, ec2, estate)
    assert second.created == [] and set(second.existing) >= set(first.created)
    identifiers = sorted(i["DBInstanceIdentifier"] for i in rds.describe_db_instances()["DBInstances"])
    assert identifiers == ["demo-aurora-reader", "demo-aurora-writer", "demo-orders", "demo-orders-replica"]
    replica = rds.describe_db_instances(DBInstanceIdentifier="demo-orders-replica")["DBInstances"][0]
    assert replica["ReadReplicaSourceDBInstanceIdentifier"] == "demo-orders"
    parameters = rds.describe_db_parameters(DBParameterGroupName="demo-postgres16-tuned")["Parameters"]
    work_mem = next(p for p in parameters if p["ParameterName"] == "work_mem")
    assert work_mem["ParameterValue"] == "16384" and work_mem["Source"] == "user"


def test_override_file_replaces_the_default_estate(moto_aws, tmp_path: Path) -> None:
    (tmp_path / "simulated_estate.yaml").write_text(
        "vpc_cidr_block: 10.1.0.0/16\nsubnet_cidr_blocks: [10.1.1.0/24, 10.1.2.0/24]\n"
        "subnet_groups: [{name: s, description: d}]\n"
        "instance_parameter_groups: []\ncluster_parameter_groups: []\nclusters: []\n"
        "instances: [{identifier: only-one, engine_version: '16.4', instance_class: db.t4g.micro, "
        "allocated_storage: 20, master_username: u, subnet_group_name: s}]\n"
    )
    estate = load_simulated_estate(tmp_path)
    rds = boto3.client("rds", region_name="eu-west-2")
    seed_simulated_estate(rds, boto3.client("ec2", region_name="eu-west-2"), estate)
    assert [i["DBInstanceIdentifier"] for i in rds.describe_db_instances()["DBInstances"]] == ["only-one"]


def test_seeding_creates_the_demo_db_access_security_group_once_in_the_estate_vpc(
    moto_aws, tmp_path: Path
) -> None:
    estate = load_simulated_estate(tmp_path)
    rds = boto3.client("rds", region_name="eu-west-2")
    ec2 = boto3.client("ec2", region_name="eu-west-2")
    first = seed_simulated_estate(rds, ec2, estate)
    group_id = first.security_group_ids["demo-db-access"]
    assert group_id.startswith("sg-")
    assert "security_group:demo-db-access" in first.created
    second = seed_simulated_estate(rds, ec2, estate)
    assert second.security_group_ids == {"demo-db-access": group_id}
    assert "security_group:demo-db-access" in second.existing
    [group] = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": ["demo-db-access"]}])[
        "SecurityGroups"
    ]
    [vpc] = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": ["rdscommander-simulated"]}])["Vpcs"]
    assert group["GroupId"] == group_id and group["VpcId"] == vpc["VpcId"]
    [rule] = group["IpPermissions"]
    assert (rule["FromPort"], rule["ToPort"], rule["IpRanges"][0]["CidrIp"]) == (5432, 5432, "10.42.0.0/16")


def test_an_estate_file_without_security_groups_still_loads(moto_aws, tmp_path: Path) -> None:
    (tmp_path / "simulated_estate.yaml").write_text(
        "vpc_cidr_block: 10.1.0.0/16\nsubnet_cidr_blocks: [10.1.1.0/24, 10.1.2.0/24]\n"
        "subnet_groups: []\ninstance_parameter_groups: []\ncluster_parameter_groups: []\n"
        "clusters: []\ninstances: []\n"
    )
    estate = load_simulated_estate(tmp_path)
    assert estate.security_groups == []
    report = seed_simulated_estate(
        boto3.client("rds", region_name="eu-west-2"), boto3.client("ec2", region_name="eu-west-2"), estate
    )
    assert report.security_group_ids == {}
