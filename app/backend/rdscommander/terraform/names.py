"""Deterministic Terraform names from AWS identifiers."""

from __future__ import annotations

import re

_NON_IDENTIFIER = re.compile(r"[^a-z0-9_]+")


def terraform_resource_name(identifier: str) -> str:
    """Lowercase, replace runs of other characters with `_`, prefix `r_` when starting with a digit."""
    name = _NON_IDENTIFIER.sub("_", identifier.lower()).strip("_")
    return f"r_{name}" if name[:1].isdigit() else name


def terraform_address_for(resource_type: str, identifier: str) -> str:
    """`<type>.<sanitised name>`."""
    return f"{resource_type}.{terraform_resource_name(identifier)}"
