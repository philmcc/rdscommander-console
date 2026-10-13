"""Per-operator settings from `.env`, all prefixed `RDSC_`."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class OperatorSettings(BaseSettings):
    """Settings that differ per operator machine, never committed."""

    model_config = SettingsConfigDict(env_prefix="RDSC_", extra="ignore")

    operator_name: str = "unknown-operator"
    runner_identity_name: str | None = None
    """Replaces the hostname in the runner identity (`<name>:<operator>`) when set.

    Inside a container the hostname is the container id, which changes whenever the container is
    re-created; a fixed name keeps operations that wait for approval resumable by the same console.
    """
    git_author_name: str = "Console Operator"
    git_author_email: str = "operator@localhost"
    control_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://rdscommander:rdscommander@control-db:5432/rdscommander"
    )
    http_port: int = 8470
    listen_on_all_interfaces_inside_container: bool = False
    terraform_binary: str = "tofu"
    config_dir: Path = Path("/data/config")
    repo_root: Path = Path("/data")
    aws_config_path: Path = Path.home() / ".aws" / "config"
    """The AWS CLI config file whose profiles the environment import offers."""
    simulated_aws_endpoint_url: str | None = "http://simulated-aws:5000"
    runner_enabled: bool = True
    runner_poll_interval_seconds: float = 2.0
    lease_seconds: int = 30
    scheduler_enabled: bool = True
    scheduler_interval_seconds: float = 60.0
    allow_checkout_without_upstream: bool = False
    max_inventory_age_minutes: int = 1440
    direct_verify_timeout_seconds: int = 1800
    """How long a direct-mode change may take before verification gives up waiting for AWS to apply it."""
    direct_verify_poll_seconds: float = 10.0
    """Pause between two read-backs while AWS is still applying a direct-mode change."""
    blue_green_create_timeout_seconds: int = 14400
    """How long a major upgrade waits for AWS to report the green side of its blue/green deployment
    `AVAILABLE`; past it the upgrade fails and leaves the deployment, still being created, in place."""
    inventory_snapshot_retention_per_target: int = Field(default=96, ge=1)
    """How many of a target's newest inventory snapshots every refresh keeps; older ones are pruned unless a
    change event, a drift finding or an operation points at them (96 is a day at the 15-minute default)."""
    execution_record_retention_days: int = Field(default=30, ge=1)
    """Age in days after which execution records of no operation, and of finished inventory refreshes and
    drift detections, are pruned; every other operation keeps its records."""
    log_level: str = "INFO"
