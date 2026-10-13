"""Parse `show -json` output into a summary and redact sensitive values using the plan's own markers."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

REDACTED = "[REDACTED]"


def _mask(values: Any, sensitivity: Any) -> Any:
    if sensitivity is True:
        return REDACTED
    if isinstance(values, dict) and isinstance(sensitivity, dict):
        return {k: _mask(v, sensitivity.get(k)) for k, v in values.items()}
    if isinstance(values, list) and isinstance(sensitivity, list):
        return [_mask(v, sensitivity[i] if i < len(sensitivity) else None) for i, v in enumerate(values)]
    return values


class ResourceChange(BaseModel):
    """One entry of `resource_changes`, redacted."""

    address: str
    resource_type: str
    actions: list[str]
    is_import: bool
    import_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    @property
    def is_replace(self) -> bool:
        """Both delete and create."""
        return "delete" in self.actions and "create" in self.actions

    @property
    def changed_attribute_names(self) -> list[str]:
        """Top-level attributes whose value differs."""
        before, after = self.before or {}, self.after or {}
        return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


class PlanSummary(BaseModel):
    """Counts plus every change."""

    resource_changes: list[ResourceChange] = Field(default_factory=list)
    create_count: int = 0
    update_count: int = 0
    delete_count: int = 0
    replace_count: int = 0
    import_count: int = 0
    no_op_count: int = 0

    @property
    def has_changes(self) -> bool:
        """Anything other than no-ops (imports count as changes to state)."""
        return any(c.actions != ["no-op"] or c.is_import for c in self.resource_changes)

    def fingerprint(self) -> str:
        """Stable hash of addresses, actions, import ids, and the after-values of changed attributes.

        Only attributes the plan changes are included, so read-only values that drift between two
        plans (such as `latest_restorable_time`) cannot make an unchanged plan look different.
        """
        material = [
            (
                c.address,
                c.actions,
                c.import_id,
                {k: (c.after or {}).get(k) for k in c.changed_attribute_names},
            )
            for c in sorted(self.resource_changes, key=lambda c: c.address)
        ]
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def summarise_plan_json(plan_json: dict[str, Any]) -> PlanSummary:
    """Build a `PlanSummary` from plan JSON."""
    summary = PlanSummary()
    for entry in plan_json.get("resource_changes", []):
        change = entry.get("change", {})
        actions = list(change.get("actions", []))
        importing = change.get("importing") or None
        resource_change = ResourceChange(
            address=entry["address"],
            resource_type=entry.get("type", ""),
            actions=actions,
            is_import=importing is not None,
            import_id=(importing or {}).get("id"),
            before=_mask(change.get("before"), change.get("before_sensitive")),
            after=_mask(change.get("after"), change.get("after_sensitive")),
        )
        summary.resource_changes.append(resource_change)
        if resource_change.is_import:
            summary.import_count += 1
        if resource_change.is_replace:
            summary.replace_count += 1
        elif actions == ["create"]:
            summary.create_count += 1
        elif actions == ["update"]:
            summary.update_count += 1
        elif actions == ["delete"]:
            summary.delete_count += 1
        elif actions == ["no-op"]:
            summary.no_op_count += 1
    return summary


def redact_plan_json(plan_json: dict[str, Any]) -> dict[str, Any]:
    """Deep copy with sensitive values masked in `resource_changes`, `planned_values`, and `prior_state`."""
    redacted = copy.deepcopy(plan_json)
    for entry in redacted.get("resource_changes", []):
        change = entry.get("change", {})
        change["before"] = _mask(change.get("before"), change.get("before_sensitive"))
        change["after"] = _mask(change.get("after"), change.get("after_sensitive"))
    for section in ("planned_values", "prior_state"):
        root = redacted.get(section, {})
        if section == "prior_state":
            root = root.get("values", {})
        _redact_module(root.get("root_module", {}))
    return redacted


def _redact_module(module: dict[str, Any]) -> None:
    for resource in module.get("resources", []):
        resource["values"] = _mask(resource.get("values"), resource.get("sensitive_values"))
    for child in module.get("child_modules", []):
        _redact_module(child)
