"""Load and validate the three YAML files under `config/`."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from rdscommander.config.models import ConsoleConfig
from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)

REQUIRED_FILES = ("company.yaml", "environments.yaml", "terraform.yaml")


class ConfigurationError(Exception):
    """Configuration is missing or invalid; message names the file and field."""


def load_console_config(config_dir: Path) -> ConsoleConfig:
    """Read company.yaml, environments.yaml, and terraform.yaml from `config_dir`."""
    raw: dict[str, dict] = {}
    for file_name in REQUIRED_FILES:
        path = config_dir / file_name
        if not path.exists():
            logger.error(
                "configuration_file_missing",
                file_path=str(path),
                fix_suggestion=f"create {file_name} in {config_dir}; see config.example/{file_name}",
            )
            raise ConfigurationError(f"missing configuration file {path}")
        raw[file_name] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        config = ConsoleConfig.model_validate(
            {
                "company": raw["company.yaml"],
                "environments": raw["environments.yaml"].get("environments", []),
                "terraform": raw["terraform.yaml"],
            }
        )
    except ValidationError as error:
        fields = "; ".join(
            " -> ".join(str(part) for part in e["loc"]) + ": " + e["msg"] for e in error.errors()
        )
        logger.error(
            "configuration_invalid", problems=fields, fix_suggestion="correct the named fields in config/"
        )
        raise ConfigurationError(f"invalid configuration: {fields}") from error
    logger.info(
        "configuration_loaded",
        company_slug=config.company.company_slug,
        environment_count=len(config.environments),
        target_count=len(config.all_targets()),
    )
    return config
