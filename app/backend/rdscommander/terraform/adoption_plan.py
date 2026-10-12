"""Validate a selection of resources and turn it into the operation's plan."""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig
from rdscommander.control_db.models import (
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryParameterGroup,
    InventorySubnetGroup,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.resources.queries import adopted_addresses_of_target
from rdscommander.terraform.names import terraform_address_for

ADOPTION_ORDER = {
    "aws_db_parameter_group": 0,
    "aws_rds_cluster_parameter_group": 0,
    "aws_db_subnet_group": 1,
    "aws_rds_cluster": 2,
    "aws_rds_cluster_instance": 3,
    "aws_db_instance": 3,
}

Prerequisite = tuple[str, str]
CatalogueEntry = tuple[str, str, str, bool, list[Prerequisite]]


class SelectedResource(BaseModel):
    """One resource chosen for adoption."""

    resource_arn: str
    resource_identifier: str
    resource_type: str
    terraform_address: str


class AdoptionPlan(BaseModel):
    """The `plan` JSON of a `terraform_adoption` operation."""

    target_key: str
    selected_resources: list[SelectedResource] = Field(default_factory=list)

    @property
    def selected_addresses(self) -> set[str]:
        """All selected Terraform addresses."""
        return {r.terraform_address for r in self.selected_resources}


class AdoptionPlanError(Exception):
    """The selection cannot be adopted as given."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def _catalogue(session: Session, snapshot_id: int) -> dict[str, CatalogueEntry]:
    """ARN -> (resource type, identifier, ownership, is default, prerequisites)."""
    catalogue: dict[str, CatalogueEntry] = {}
    groups = session.execute(
        select(InventoryParameterGroup).where(InventoryParameterGroup.snapshot_id == snapshot_id)
    ).scalars()
    for group in groups:
        resource_type = (
            "aws_db_parameter_group" if group.group_scope == "instance" else "aws_rds_cluster_parameter_group"
        )
        catalogue[group.parameter_group_arn] = (
            resource_type,
            group.parameter_group_name,
            group.ownership,
            group.is_default,
            [],
        )
    subnet_groups = session.execute(
        select(InventorySubnetGroup).where(InventorySubnetGroup.snapshot_id == snapshot_id)
    ).scalars()
    for subnet_group in subnet_groups:
        catalogue[subnet_group.db_subnet_group_arn] = (
            "aws_db_subnet_group",
            subnet_group.db_subnet_group_name,
            subnet_group.ownership,
            False,
            [],
        )
    clusters = session.execute(
        select(InventoryDbCluster).where(InventoryDbCluster.snapshot_id == snapshot_id)
    ).scalars()
    for cluster in clusters:
        prerequisites: list[Prerequisite] = [
            ("aws_rds_cluster_parameter_group", cluster.cluster_parameter_group_name or ""),
            ("aws_db_subnet_group", cluster.db_subnet_group_name or ""),
        ]
        catalogue[cluster.db_cluster_arn] = (
            "aws_rds_cluster",
            cluster.db_cluster_identifier,
            cluster.ownership,
            False,
            [p for p in prerequisites if p[1]],
        )
    instances = session.execute(
        select(InventoryDbInstance).where(InventoryDbInstance.snapshot_id == snapshot_id)
    ).scalars()
    for instance in instances:
        if instance.db_cluster_identifier:
            prerequisites = [
                ("aws_rds_cluster", instance.db_cluster_identifier),
                ("aws_db_parameter_group", instance.parameter_group_name or ""),
            ]
            resource_type = "aws_rds_cluster_instance"
        else:
            prerequisites = [
                ("aws_db_parameter_group", instance.parameter_group_name or ""),
                ("aws_db_subnet_group", instance.db_subnet_group_name or ""),
                ("aws_db_instance", instance.read_replica_source_identifier or ""),
            ]
            resource_type = "aws_db_instance"
        catalogue[instance.db_instance_arn] = (
            resource_type,
            instance.db_instance_identifier,
            instance.ownership,
            False,
            [p for p in prerequisites if p[1]],
        )
    return catalogue


def build_adoption_plan(
    session: Session, console_config: ConsoleConfig, target_key: str, resource_arns: list[str]
) -> AdoptionPlan:
    """Resolve ARNs against the latest snapshot and enforce ownership and prerequisite rules."""
    target = console_config.target(target_key)
    snapshot = latest_snapshot_for_target(session, target.target_key)
    if snapshot is None:
        raise AdoptionPlanError([f"no inventory snapshot for {target_key}; refresh inventory first"])
    adopted_addresses = adopted_addresses_of_target(session, target_key)

    catalogue = _catalogue(session, snapshot.id)
    default_group_names = {name for (_, name, _, is_default, _) in catalogue.values() if is_default}
    problems: list[str] = []
    selected: list[SelectedResource] = []
    for arn in resource_arns:
        entry = catalogue.get(arn)
        if entry is None:
            problems.append(f"{arn}: unknown in the latest snapshot")
            continue
        resource_type, identifier, ownership, is_default, _ = entry
        address = terraform_address_for(resource_type, identifier)
        if is_default:
            problems.append(f"{identifier}: AWS default parameter groups cannot be adopted")
        elif ownership in ("foreign_terraform", "foreign_cloudformation"):
            problems.append(f"{identifier}: owned by other tooling ({ownership}); not adoptable")
        elif address in adopted_addresses:
            # The tag is no evidence: a group the console created in direct mode carries it too.
            problems.append(f"{identifier}: already adopted")
        else:
            selected.append(
                SelectedResource(
                    resource_arn=arn,
                    resource_identifier=identifier,
                    resource_type=resource_type,
                    terraform_address=address,
                )
            )
    selected_addresses = {r.terraform_address for r in selected}
    for resource in selected:
        for prerequisite_type, prerequisite_name in catalogue[resource.resource_arn][4]:
            if prerequisite_type.endswith("parameter_group") and prerequisite_name in default_group_names:
                continue
            address = terraform_address_for(prerequisite_type, prerequisite_name)
            if address not in selected_addresses and address not in adopted_addresses:
                problems.append(
                    f"{resource.resource_identifier}: requires {prerequisite_name} ({prerequisite_type}) "
                    "to be adopted first or in the same selection"
                )
    if problems:
        raise AdoptionPlanError(sorted(set(problems)))
    selected.sort(key=lambda r: (ADOPTION_ORDER[r.resource_type], r.terraform_address))
    return AdoptionPlan(target_key=target.target_key, selected_resources=selected)
