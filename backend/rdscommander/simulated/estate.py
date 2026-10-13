"""Shape of the simulated estate."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_ESTATE_PATH = Path(__file__).parent / "default_estate.yaml"
OVERRIDE_FILE_NAME = "simulated_estate.yaml"


class SimulatedParameter(BaseModel):
    """One user-set parameter."""

    model_config = ConfigDict(extra="forbid")
    name: str
    value: str
    apply_method: str = "immediate"


class SimulatedParameterGroup(BaseModel):
    """Instance or cluster parameter group."""

    model_config = ConfigDict(extra="forbid")
    name: str
    family: str
    description: str
    parameters: list[SimulatedParameter] = Field(default_factory=list)


class SimulatedSubnetGroup(BaseModel):
    """A subnet group spanning the two simulated subnets."""

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str


class SimulatedSecurityGroup(BaseModel):
    """A VPC security group that admits PostgreSQL from the simulated VPC.

    Attributes:
        name: The group's name, such as demo-db-access.
        description: The group's description.
        postgres_port: The port admitted from the VPC's CIDR block.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    postgres_port: int = 5432


class SimulatedClusterInstance(BaseModel):
    """A cluster member."""

    model_config = ConfigDict(extra="forbid")
    identifier: str
    instance_class: str
    promotion_tier: int = 1


class SimulatedCluster(BaseModel):
    """An Aurora PostgreSQL cluster."""

    model_config = ConfigDict(extra="forbid")
    identifier: str
    engine_version: str
    master_username: str
    database_name: str | None = None
    cluster_parameter_group_name: str | None = None
    subnet_group_name: str | None = None
    instances: list[SimulatedClusterInstance] = Field(default_factory=list)


class SimulatedInstance(BaseModel):
    """An RDS PostgreSQL instance or read replica."""

    model_config = ConfigDict(extra="forbid")
    identifier: str
    instance_class: str
    engine_version: str | None = None
    allocated_storage: int | None = None
    master_username: str | None = None
    parameter_group_name: str | None = None
    subnet_group_name: str | None = None
    multi_az: bool = False
    read_replica_of: str | None = None


class SimulatedEstate(BaseModel):
    """Everything the seeder creates."""

    model_config = ConfigDict(extra="forbid")
    vpc_cidr_block: str
    subnet_cidr_blocks: list[str] = Field(min_length=2)
    subnet_groups: list[SimulatedSubnetGroup]
    instance_parameter_groups: list[SimulatedParameterGroup]
    cluster_parameter_groups: list[SimulatedParameterGroup]
    clusters: list[SimulatedCluster]
    instances: list[SimulatedInstance]
    security_groups: list[SimulatedSecurityGroup] = Field(default_factory=list)


def load_simulated_estate(config_dir: Path) -> SimulatedEstate:
    """Load `config/simulated_estate.yaml` when present, else the packaged default."""
    path = config_dir / OVERRIDE_FILE_NAME
    if not path.exists():
        path = DEFAULT_ESTATE_PATH
    return SimulatedEstate.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
