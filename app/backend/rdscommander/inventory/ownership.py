"""Classify who manages a resource from its tags."""

from __future__ import annotations

MANAGED_TAG_KEY = "rdscommander:managed"
TERRAFORM_TAG_KEYS = {"terraform", "tf_workspace", "terraform_workspace", "tf_module"}


def derive_ownership(tags: dict[str, str]) -> str:
    """Return `adopted`, `foreign_cloudformation`, `foreign_terraform`, or `unmanaged`, in that priority."""
    if tags.get(MANAGED_TAG_KEY, "").lower() == "true":
        return "adopted"
    if any(key.startswith("aws:cloudformation:") for key in tags):
        return "foreign_cloudformation"
    lowered = {key.lower(): str(value).lower() for key, value in tags.items()}
    if (
        TERRAFORM_TAG_KEYS & set(lowered)
        or lowered.get("managedby") == "terraform"
        or lowered.get("managed_by") == "terraform"
    ):
        return "foreign_terraform"
    return "unmanaged"
