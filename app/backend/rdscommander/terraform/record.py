"""`console-record.json`: the structured twin of the rendered Terraform files, used for drift.

The Terraform files in the repository are the record of the estate in both change-management modes. The
files are written for Terraform to read; this file holds the same content as the renderer's input so that
drift can be found by comparing two structured values attribute by attribute instead of parsing HCL.

Example:
    >>> path = write_record(workspace_dir, render_input, snapshot_id=12)  # doctest: +SKIP
    >>> path.name
    'console-record.json'
    >>> diff_records(read_record(workspace_dir).render_input, live_render_input)  # doctest: +SKIP
    {'aws_db_instance.demo_orders': [DriftAttribute(address='aws_db_instance.demo_orders', ...)]}
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from rdscommander.control_db.models import utc_now
from rdscommander.logging_setup import get_logger
from rdscommander.terraform.hcl import to_hcl
from rdscommander.terraform.names import terraform_address_for
from rdscommander.terraform.render_input import RenderWorkspaceInput

logger = get_logger(__name__)

RECORD_FILE_NAME = "console-record.json"
ENGINE_VERSION_WHEN_AUTO_MINOR = "engine_version_when_auto_minor"
RESTORE_SOURCE_ARGUMENTS = "restore_source_arguments"
RESTORE_SOURCE_ATTRIBUTES = ("snapshot_identifier", "restore_to_point_in_time")
"""How a restored instance or cluster was created; AWS never reports them, so live renders lack them."""
IGNORED_FOR_DRIFT: frozenset[str] = frozenset({ENGINE_VERSION_WHEN_AUTO_MINOR, RESTORE_SOURCE_ARGUMENTS})
"""Drift rules that exclude an attribute. `engine_version_when_auto_minor` excludes `engine_version` for
instances, cluster instances and clusters whose `auto_minor_version_upgrade` is true: the renderer leaves
the version to AWS for those (`ignore_changes = [engine_version]`), so a minor upgrade is not drift.
`restore_source_arguments` excludes a recorded `snapshot_identifier` or `restore_to_point_in_time`: the
renderer tells Terraform to ignore them after the create (`ignore_changes`), and AWS does not report them."""
RENDER_ONLY_ATTRIBUTES = ("depends_on_member", "final_snapshot_identifier")
"""Rendered fields AWS has no setting for, so drift never compares them: `depends_on_member` only orders
Terraform's work (a new cluster's readers after its writer), and `final_snapshot_identifier` names the
snapshot Terraform takes when it deletes the resource (a decommission sets it before the delete)."""
AUTO_MINOR_RESOURCE_TYPES = ("aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance")
PRESENCE_ATTRIBUTE = "presence"
RESOURCE_SECTIONS: dict[str, str] = {
    "aws_db_parameter_group": "instance_parameter_groups",
    "aws_rds_cluster_parameter_group": "cluster_parameter_groups",
    "aws_db_subnet_group": "subnet_groups",
    "aws_rds_cluster": "clusters",
    "aws_rds_cluster_instance": "cluster_instances",
    "aws_db_instance": "instances",
}
"""Terraform resource type -> the `RenderWorkspaceInput` list that holds its resources."""


class RecordedWorkspace(BaseModel):
    """What the record file holds.

    Attributes:
        render_input: The renderer input the committed Terraform files were produced from.
        recorded_at_utc: When the record was written.
        snapshot_id: Inventory snapshot the record was rendered from; None when not known.
    """

    render_input: RenderWorkspaceInput
    recorded_at_utc: datetime
    snapshot_id: int | None


class DriftAttribute(BaseModel):
    """One attribute that differs between the record and reality.

    Attributes:
        address: Terraform address of the resource, e.g. `aws_db_instance.demo_orders`.
        attribute: Attribute path, e.g. `instance_class`, `tags.team` or `parameters.work_mem`; `presence`
            when the resource exists on only one side.
        recorded: The value in the record.
        live: The value in reality.
    """

    address: str
    attribute: str
    recorded: Any
    live: Any


def write_record(workspace_dir: Path, render_input: RenderWorkspaceInput, snapshot_id: int | None) -> Path:
    """Write the record file next to the rendered files.

    The file is left untouched when it already holds this `render_input` from this `snapshot_id`, so
    rendering again without a change does not produce a spurious difference in the repository. Import
    blocks are never recorded: they are scaffolding for one adoption, not part of the estate.

    Args:
        workspace_dir: The target's workspace directory; created when missing.
        render_input: The renderer input the Terraform files were produced from.
        snapshot_id: Inventory snapshot the input was built from, if known.

    Returns:
        The path of `console-record.json`.

    Example:
        >>> workspace_dir = Path("terraform/sandbox/000000000000/eu-west-2")
        >>> write_record(workspace_dir, render_input, 12).name  # doctest: +SKIP
        'console-record.json'
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)
    path = workspace_dir / RECORD_FILE_NAME
    recorded_input = render_input.model_copy(update={"import_blocks": []})
    try:
        existing = read_record(workspace_dir)
    except ValueError:  # unreadable record: replace it
        existing = None
    if (
        existing is not None
        and existing.render_input == recorded_input
        and existing.snapshot_id == snapshot_id
    ):
        return path
    recorded = RecordedWorkspace(
        render_input=recorded_input, recorded_at_utc=utc_now(), snapshot_id=snapshot_id
    )
    path.write_text(
        json.dumps(recorded.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    logger.info(
        "terraform_record_written",
        record_path=str(path),
        snapshot_id=snapshot_id,
        target_key=recorded_input.target_key,
    )
    return path


def read_record(workspace_dir: Path) -> RecordedWorkspace | None:
    """Load the record file, or None when the workspace has never been recorded.

    Args:
        workspace_dir: The target's workspace directory.

    Returns:
        The recorded workspace, or None when `console-record.json` does not exist.

    Raises:
        pydantic.ValidationError: The file exists but is not a valid record.
        json.JSONDecodeError: The file exists but is not JSON.

    Example:
        >>> read_record(Path("does-not-exist")) is None
        True
    """
    path = workspace_dir / RECORD_FILE_NAME
    if not path.exists():
        return None
    return RecordedWorkspace.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _flatten(value: Any, path: str = "") -> dict[str, Any]:
    """Nested dicts become dotted keys; lists and scalars are leaves; empty dicts disappear."""
    if isinstance(value, dict):
        flattened: dict[str, Any] = {}
        for key, item in value.items():
            flattened.update(_flatten(item, f"{path}.{key}" if path else str(key)))
        return flattened
    return {path: value}


def _resources_by_address(render_input: RenderWorkspaceInput) -> dict[str, tuple[str, dict[str, Any]]]:
    """`{address: (resource type, JSON-mode data)}` for every resource the input renders."""
    resources: dict[str, tuple[str, dict[str, Any]]] = {}
    for resource_type, section_name in RESOURCE_SECTIONS.items():
        for item in getattr(render_input, section_name):
            data = item.model_dump(mode="json")
            resources[f"{resource_type}.{data['terraform_name']}"] = (resource_type, data)
    return resources


def resource_addresses(render_input: RenderWorkspaceInput) -> set[str]:
    """The Terraform address of every resource `render_input` renders.

    Example:
        >>> sorted(resource_addresses(recorded.render_input))  # doctest: +SKIP
        ['aws_db_instance.demo_orders', 'aws_db_parameter_group.demo_postgres16_tuned']
    """
    return set(_resources_by_address(render_input))


REFERENCE_FIELDS: dict[str, tuple[str, str]] = {
    "parameter_group_reference": ("aws_db_parameter_group", "name"),
    "cluster_parameter_group_reference": ("aws_rds_cluster_parameter_group", "name"),
    "db_subnet_group_reference": ("aws_db_subnet_group", "name"),
    "cluster_reference": ("aws_rds_cluster", "id"),
    "replicate_source_reference": ("aws_db_instance", "identifier"),
}
"""Fields that point at another resource -> (the resource type, the attribute a reference reads)."""


def resource_identifiers(render_input: RenderWorkspaceInput) -> dict[str, str]:
    """`{address: AWS identifier}` (a group's name, a database's identifier) for every rendered resource."""
    return {
        address: str(data.get("name") or data.get("identifier"))
        for address, (_, data) in _resources_by_address(render_input).items()
    }


def _referenced_identifier(value: str, identifiers: dict[str, str]) -> str | None:
    """The identifier a reference field names: a quoted literal's text, or the identifier of its address."""
    if value.startswith('"'):
        try:
            return str(json.loads(value))
        except ValueError:
            return None
    return identifiers.get(value.rsplit(".", 1)[0])


def with_references_resolved(
    render_input: RenderWorkspaceInput, known_identifiers: dict[str, str]
) -> RenderWorkspaceInput:
    """A copy whose references point at resources it renders, and whose other references are literals.

    The renderer refers to a resource of the same workspace by address (`aws_db_parameter_group.x.name`)
    and to any other by its quoted name. When resources join or leave a record (a mode switch), the
    references of the resources that stay have to follow, or the files would refer to an address they do
    not declare, and drift would compare a reference with a name.

    Args:
        render_input: The record after resources joined or left it.
        known_identifiers: `{address: identifier}` of every resource a reference may name, including those
            that left (`resource_identifiers` of the inputs involved).

    Returns:
        A deep copy with every reference field rewritten.

    Example:
        >>> with_references_resolved(rebaselined, resource_identifiers(recorded))  # doctest: +SKIP
    """
    resolved = render_input.model_copy(deep=True)
    rendered = set(_resources_by_address(resolved))
    identifiers = {**known_identifiers, **resource_identifiers(resolved)}
    for section_name in RESOURCE_SECTIONS.values():
        for item in getattr(resolved, section_name):
            for field_name, (resource_type, attribute) in REFERENCE_FIELDS.items():
                value = getattr(item, field_name, None)
                if not isinstance(value, str):
                    continue
                identifier = _referenced_identifier(value, identifiers)
                if identifier is None:
                    continue
                address = terraform_address_for(resource_type, identifier)
                reference = f"{address}.{attribute}" if address in rendered else to_hcl(identifier)
                setattr(item, field_name, reference)
    return resolved


def _section_sort_key(item: Any) -> str:
    """Resources are listed by `name` (groups) or `identifier` (databases), as the input is built."""
    return item.name if hasattr(item, "name") else item.identifier


def replace_resource(
    render_input: RenderWorkspaceInput, address: str, source: RenderWorkspaceInput
) -> RenderWorkspaceInput:
    """A copy of `render_input` in which the resource at `address` is taken from `source`.

    Accepting drift for one resource builds the new record this way: the recorded input with only the
    chosen resource replaced by its live rendering, so every other resource keeps its recorded values.
    A restored resource keeps the source it was created from (`RESTORE_SOURCE_ATTRIBUTES`) when `source`
    lacks it: AWS never reports it, and both are create-only, so dropping them would make Terraform plan
    a replace that every later change refuses.

    Args:
        render_input: The input to start from, typically the committed record. It is not modified.
        address: Terraform address of the resource, e.g. `aws_db_instance.demo_orders`.
        source: The input to take the resource from, typically the rendering of the latest snapshot.

    Returns:
        A deep copy of `render_input` where the resource at `address` is `source`'s version of it, is
        removed when `source` lacks it (the resource vanished), or is added when only `source` has it.

    Raises:
        ValueError: `address` is not of a resource type the renderer produces.

    Example:
        >>> address = "aws_db_instance.demo_orders"
        >>> accepted = replace_resource(recorded.render_input, address, live)  # doctest: +SKIP
        >>> accepted.instances[0].instance_class  # doctest: +SKIP
        'db.r6g.xlarge'
    """
    resource_type, _, terraform_name = address.partition(".")
    section_name = RESOURCE_SECTIONS.get(resource_type)
    if section_name is None or not terraform_name:
        raise ValueError(
            f"{address!r} is not the address of a resource the renderer produces; "
            f"known types: {', '.join(RESOURCE_SECTIONS)}"
        )
    replaced = render_input.model_copy(deep=True)
    section = getattr(replaced, section_name)
    live_version = next(
        (item for item in getattr(source, section_name) if item.terraform_name == terraform_name), None
    )
    position = next(
        (index for index, item in enumerate(section) if item.terraform_name == terraform_name), None
    )
    if live_version is None:
        if position is not None:
            del section[position]
    elif position is not None:
        replacement = live_version.model_copy(deep=True)
        for attribute in RESTORE_SOURCE_ATTRIBUTES:
            recorded_value = getattr(section[position], attribute, None)
            if recorded_value is not None and getattr(replacement, attribute, None) is None:
                setattr(replacement, attribute, recorded_value)
        section[position] = replacement
    else:
        section.append(live_version.model_copy(deep=True))
        section.sort(key=_section_sort_key)
    return replaced


def _attributes_ignored_for_drift(resource_type: str, recorded_data: dict[str, Any]) -> set[str]:
    """Attributes drift leaves out for one resource, decided by what the record's own files say.

    The record's files tell Terraform to leave `engine_version` to AWS when automatic minor upgrades are on,
    so a different live version is not a difference Terraform would act on; likewise the source a restored
    resource was created from (`snapshot_identifier`, `restore_to_point_in_time`).
    """
    ignored: set[str] = set()
    if (
        ENGINE_VERSION_WHEN_AUTO_MINOR in IGNORED_FOR_DRIFT
        and resource_type in AUTO_MINOR_RESOURCE_TYPES
        and recorded_data.get("auto_minor_version_upgrade")
    ):
        ignored.add("engine_version")
    if RESTORE_SOURCE_ARGUMENTS in IGNORED_FOR_DRIFT and resource_type in (
        "aws_db_instance",
        "aws_rds_cluster",
    ):
        ignored.update(attribute for attribute in RESTORE_SOURCE_ATTRIBUTES if recorded_data.get(attribute))
    return ignored


def _comparable(data: dict[str, Any], ignored_attributes: set[str]) -> dict[str, Any]:
    """Flat `{attribute path: value}` of what drift compares.

    Parameters are compared by value under `parameters.<name>`; their apply method is how a change was
    applied, not what the setting is, so it is not drift.
    """
    comparable = {name: value for name, value in data.items() if name != "terraform_name"}
    if "parameters" in comparable:
        comparable["parameters"] = {item["name"]: item["value"] for item in comparable["parameters"]}
    for ignored_attribute in (*ignored_attributes, *RENDER_ONLY_ATTRIBUTES):
        comparable.pop(ignored_attribute, None)
    return _flatten(comparable)


def diff_records(
    recorded: RenderWorkspaceInput, live: RenderWorkspaceInput
) -> dict[str, list[DriftAttribute]]:
    """Attribute-level differences per Terraform address.

    A resource present on only one side is reported as one `presence` attribute. Attributes that the
    record's own files leave to AWS (`IGNORED_FOR_DRIFT`: `engine_version` while the recorded resource has
    automatic minor upgrades on) are excluded.

    Args:
        recorded: The renderer input of the committed record.
        live: The renderer input built from the latest inventory snapshot.

    Returns:
        `{address: [DriftAttribute, ...]}` for the resources that differ, sorted by address then
        attribute; resources that match are absent.

    Example:
        >>> diff = diff_records(recorded_input, live_input)  # doctest: +SKIP
        >>> [d.attribute for d in diff["aws_db_instance.demo_orders"]]  # doctest: +SKIP
        ['instance_class']
    """
    before, after = _resources_by_address(recorded), _resources_by_address(live)
    drift: dict[str, list[DriftAttribute]] = {}
    for address in sorted(set(before) | set(after)):
        if address not in before or address not in after:
            drift[address] = [
                DriftAttribute(
                    address=address,
                    attribute=PRESENCE_ATTRIBUTE,
                    recorded=address in before,
                    live=address in after,
                )
            ]
            continue
        resource_type, recorded_data = before[address]
        ignored_attributes = _attributes_ignored_for_drift(resource_type, recorded_data)
        recorded_values = _comparable(recorded_data, ignored_attributes)
        live_values = _comparable(after[address][1], ignored_attributes)
        differences = [
            DriftAttribute(
                address=address,
                attribute=attribute,
                recorded=recorded_values.get(attribute),
                live=live_values.get(attribute),
            )
            for attribute in sorted(set(recorded_values) | set(live_values))
            if recorded_values.get(attribute) != live_values.get(attribute)
        ]
        if differences:
            drift[address] = differences
    return drift
