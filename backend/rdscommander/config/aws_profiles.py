"""Profiles available on the operator's machine, read from the mounted AWS CLI files."""

from __future__ import annotations

import configparser
from pathlib import Path


def list_aws_profiles(config_path: Path, credentials_path: Path | None = None) -> list[str]:
    """Profile names from the AWS CLI config and credentials files, sorted.

    The config file (`~/.aws/config`) names profiles as `[profile x]` plus `[default]`; the credentials
    file (`~/.aws/credentials`) names them as `[x]`. Missing files contribute no profiles.

    Args:
        config_path: Path of the AWS CLI config file.
        credentials_path: Path of the AWS CLI credentials file, if it should be read too.

    Returns:
        Sorted, de-duplicated profile names.

    Example:
        >>> list_aws_profiles(Path("~/.aws/config").expanduser())  # doctest: +SKIP
        ['example-prod', 'default']
    """
    names: set[str] = set()
    if config_path.exists():
        parser = configparser.ConfigParser()
        parser.read(config_path, encoding="utf-8")
        for section in parser.sections():
            if section == "default":
                names.add("default")
            elif section.startswith("profile "):
                names.add(section.removeprefix("profile ").strip())
    if credentials_path is not None and credentials_path.exists():
        parser = configparser.ConfigParser()
        parser.read(credentials_path, encoding="utf-8")
        names.update(parser.sections())
    return sorted(names)
