"""The attribute change: one shape for every 'set these attributes' operation."""

from __future__ import annotations

import pytest

from rdscommander.operations.attribute_change import (
    AttributeChange,
    AttributeNotChangeableError,
    rendered_field_for,
)
from rdscommander.operations.change_models import ChangeSetEntry
from rdscommander.parameters.direct_api import (
    ATTRIBUTE_APPLY_PROFILES,
    INSTANCE_ATTRIBUTES,
    backup_retention_apply_profile,
    queued_attribute_values,
)

INSTANCE_ADDRESS = "aws_db_instance.demo_orders"


def test_rendered_field_for_maps_attributes_to_themselves_and_tags_to_the_tag_map():
    assert rendered_field_for(INSTANCE_ADDRESS, "instance_class") == "instance_class"
    assert rendered_field_for(INSTANCE_ADDRESS, "tags.team") == "tags"


def test_apply_to_render_input_sets_the_rendered_fields_and_tags(sandbox_render_input):
    change = AttributeChange(
        resource_arn="arn:aws:rds:eu-west-2:000000000000:db:demo-orders",
        address=INSTANCE_ADDRESS,
        values={"instance_class": "db.m6g.xlarge", "tags.team": "orders", "tags.old": None},
        summary="change class",
    )
    before = next(i for i in sandbox_render_input.instances if i.identifier == "demo-orders")
    before.tags = {"old": "x"}
    updated = change.apply_to_render_input(sandbox_render_input)
    after = next(i for i in updated.instances if i.identifier == "demo-orders")
    assert after.instance_class == "db.m6g.xlarge"
    assert after.tags == {"team": "orders"}
    assert before.instance_class != "db.m6g.xlarge", "the input is copied, not mutated"


def test_a_protected_tag_prefix_is_refused_before_any_call():
    with pytest.raises(ValueError, match="rdscommander:managed"):
        AttributeChange(
            resource_arn="arn",
            address=INSTANCE_ADDRESS,
            values={"tags.rdscommander:managed": "false"},
            summary="x",
        )


def test_a_render_only_attribute_is_accepted_and_rendered_but_never_sent_to_the_api(
    sandbox_render_input, monkeypatch
):
    monkeypatch.setattr(
        "rdscommander.operations.attribute_change.RENDER_ONLY_ATTRIBUTES",
        frozenset({"final_snapshot_identifier"}),
    )
    sent: list[dict] = []
    monkeypatch.setattr(
        "rdscommander.operations.attribute_change.apply_attribute_values",
        lambda context, address, values, **keywords: sent.append(values) or values,
    )
    monkeypatch.setattr(
        "rdscommander.operations.attribute_change.read_attribute_values",
        lambda context, address, attributes, step_name="verify_direct": {a: 99 for a in attributes},
    )
    change = AttributeChange(
        resource_arn="arn",
        address=INSTANCE_ADDRESS,
        values={"final_snapshot_identifier": "demo-orders-last", "allocated_storage": 99},
        summary="grow",
    )
    updated = change.apply_to_render_input(sandbox_render_input)
    assert (
        next(i for i in updated.instances if i.identifier == "demo-orders").final_snapshot_identifier
        == "demo-orders-last"
    )
    assert change.expected_impact(None).downtime == "none"  # type: ignore[arg-type]
    entries = [
        ChangeSetEntry(
            address=INSTANCE_ADDRESS,
            attribute=attribute,
            current=None,
            desired=desired,
            apply_type="immediate",
            requires_reboot=False,
            downtime="none",
        )
        for attribute, desired in change.values.items()
    ]
    change.apply_direct(None, entries)  # type: ignore[arg-type]
    assert sent == [{"allocated_storage": 99}]
    assert change.verify_direct(None, entries).ok  # type: ignore[arg-type]


def test_tags_are_allowed_on_a_parameter_group_but_other_attributes_are_not(sandbox_render_input):
    group_address = "aws_db_parameter_group.demo_postgres16_tuned"
    change = AttributeChange(
        resource_arn="arn", address=group_address, values={"tags.team": "data"}, summary="t"
    )
    updated = change.apply_to_render_input(sandbox_render_input)
    group = next(g for g in updated.instance_parameter_groups if g.terraform_name == "demo_postgres16_tuned")
    assert group.tags["team"] == "data"
    with pytest.raises(AttributeNotChangeableError):
        AttributeChange(
            resource_arn="arn", address=group_address, values={"instance_class": "x"}, summary="t"
        )


def test_an_attribute_the_api_cannot_write_is_refused(sandbox_render_input):
    with pytest.raises(AttributeNotChangeableError, match="publicly_accessible"):
        AttributeChange(
            resource_arn="arn", address=INSTANCE_ADDRESS, values={"publicly_accessible": True}, summary="x"
        )


def test_a_resource_missing_from_the_rendered_input_is_a_key_error(sandbox_render_input):
    change = AttributeChange(
        resource_arn="arn", address="aws_db_instance.nobody", values={"multi_az": True}, summary="x"
    )
    with pytest.raises(KeyError, match="aws_db_instance.nobody"):
        change.apply_to_render_input(sandbox_render_input)


def _entry(attribute: str, desired, apply_type: str = "pending-window") -> ChangeSetEntry:
    return ChangeSetEntry(
        address=INSTANCE_ADDRESS,
        attribute=attribute,
        current=None,
        desired=desired,
        apply_type=apply_type,  # type: ignore[arg-type]
        requires_reboot=False,
        downtime="none",
    )


def _verify(monkeypatch, live: dict, queued: dict, entries: list[ChangeSetEntry]):
    module = "rdscommander.operations.attribute_change"
    monkeypatch.setattr(f"{module}.read_attribute_values", lambda context, address, attributes, **kw: live)
    monkeypatch.setattr(f"{module}.read_pending_modifications", lambda context, address, **kw: queued)
    monkeypatch.setattr(f"{module}.read_resource_status", lambda context, address, **kw: "modifying")
    change = AttributeChange(
        resource_arn="arn",
        address=INSTANCE_ADDRESS,
        values={e.attribute: e.desired for e in entries},
        summary="x",
        apply_at="maintenance_window",
    )
    return change.verify_direct(None, entries)  # type: ignore[arg-type]


def test_a_queued_value_listed_under_the_aws_field_name_counts_as_held(monkeypatch):
    entries = [_entry("backup_retention_period", 14)]
    result = _verify(monkeypatch, {"backup_retention_period": 7}, {"BackupRetentionPeriod": 14}, entries)
    assert result.ok and result.pending == ["backup_retention_period"]


def test_a_value_that_is_neither_live_nor_queued_as_asked_is_not_verified(monkeypatch):
    entries = [_entry("backup_retention_period", 14)]
    result = _verify(monkeypatch, {"backup_retention_period": 7}, {"BackupRetentionPeriod": 30}, entries)
    assert not result.ok and "backup_retention_period" in result.detail


def test_a_partial_change_lists_nothing_as_pending_so_the_strategy_keeps_polling(monkeypatch):
    entries = [_entry("backup_retention_period", 14), _entry("multi_az", True)]
    result = _verify(
        monkeypatch,
        {"backup_retention_period": 7, "multi_az": False},
        {"BackupRetentionPeriod": 14},
        entries,
    )
    assert not result.ok and result.pending == []


def _window_change_set(monkeypatch, values: dict, live: dict, address: str = INSTANCE_ADDRESS):
    module = "rdscommander.operations.attribute_change"
    monkeypatch.setattr(f"{module}.read_attribute_values", lambda context, address, attributes, **kw: live)
    monkeypatch.setattr(f"{module}.read_pending_modifications", lambda context, address, **kw: {})
    change = AttributeChange(
        resource_arn="arn", address=address, values=values, summary="x", apply_at="maintenance_window"
    )
    return change.change_set(None)  # type: ignore[arg-type]


def test_a_tag_change_at_the_window_is_still_immediate_because_aws_ignores_apply_immediately_for_tags(
    monkeypatch,
):
    [entry] = _window_change_set(monkeypatch, {"tags.team": "orders"}, {"tags.team": None})
    assert entry.apply_type == "immediate" and entry.downtime == "none"


def test_a_setting_queued_by_aws_is_pending_window_but_one_it_applies_at_once_is_not(monkeypatch):
    entries = _window_change_set(
        monkeypatch,
        {"multi_az": True, "deletion_protection": True, "copy_tags_to_snapshot": True},
        {"multi_az": False, "deletion_protection": False, "copy_tags_to_snapshot": False},
    )
    assert {e.attribute: e.apply_type for e in entries} == {
        "multi_az": "pending-window",
        "deletion_protection": "immediate",
        "copy_tags_to_snapshot": "immediate",
    }


def test_a_retention_change_between_non_zero_values_applies_at_once_even_when_the_window_is_asked(
    monkeypatch,
):
    # AWS applies 1 -> 14 as soon as possible whatever ApplyImmediately says; the walkthrough's review
    # wrongly said "queued for the maintenance window".
    [entry] = _window_change_set(monkeypatch, {"backup_retention_period": 14}, {"backup_retention_period": 1})
    assert entry.apply_type == "immediate"
    assert entry.downtime == "none"


@pytest.mark.parametrize(("current", "desired"), [(0, 7), (7, 0)])
def test_turning_automated_backups_on_or_off_waits_for_the_window_and_warns_of_an_outage(
    monkeypatch, current, desired
):
    [queued] = _window_change_set(
        monkeypatch, {"backup_retention_period": desired}, {"backup_retention_period": current}
    )
    assert queued.apply_type == "pending-window"
    module = "rdscommander.operations.attribute_change"
    monkeypatch.setattr(
        f"{module}.read_attribute_values",
        lambda context, address, attributes, **kw: {"backup_retention_period": current},
    )
    monkeypatch.setattr(f"{module}.read_pending_modifications", lambda context, address, **kw: {})
    now = AttributeChange(
        resource_arn="arn", address=INSTANCE_ADDRESS, values={"backup_retention_period": desired}, summary="x"
    )
    [applied_now] = now.change_set(None)  # type: ignore[arg-type]
    assert applied_now.apply_type == "immediate"
    assert "outage" in applied_now.downtime


def test_the_review_of_turning_backups_off_warns_of_an_outage_without_reading_aws():
    change = AttributeChange(
        resource_arn="arn", address=INSTANCE_ADDRESS, values={"backup_retention_period": 0}, summary="x"
    )
    assert "outage" in change.expected_impact(None).downtime  # type: ignore[arg-type]
    grow = AttributeChange(
        resource_arn="arn", address=INSTANCE_ADDRESS, values={"backup_retention_period": 14}, summary="x"
    )
    assert grow.expected_impact(None).downtime == "none"  # type: ignore[arg-type]


def test_the_retention_profile_depends_on_whether_backups_are_turned_on_or_off():
    assert not backup_retention_apply_profile(1, 14).honours_apply_immediately
    assert backup_retention_apply_profile(0, 14).honours_apply_immediately
    assert backup_retention_apply_profile(14, 0).honours_apply_immediately
    assert backup_retention_apply_profile(None, 14).downtime == "none"


# DBInstance.PendingModifiedValues members (RDS API reference) that the console can write.
AWS_HOLDS_FOR_THE_WINDOW = {
    "DBInstanceClass",
    "AllocatedStorage",
    "BackupRetentionPeriod",
    "MultiAZ",
    "Iops",
    "StorageType",
    "StorageThroughput",
    "EngineVersion",
}


@pytest.mark.parametrize("attribute", sorted(set(INSTANCE_ATTRIBUTES) - {"backup_retention_period"}))
def test_an_attribute_honours_apply_immediately_exactly_when_aws_lists_it_among_pending_modified_values(
    attribute,
):
    request_field = INSTANCE_ATTRIBUTES[attribute].request_field
    assert ATTRIBUTE_APPLY_PROFILES[attribute].honours_apply_immediately == (
        request_field in AWS_HOLDS_FOR_THE_WINDOW
    )


@pytest.mark.parametrize("apply_at", ["later", "", "Maintenance_Window", "next_week"])
def test_an_unknown_apply_at_is_refused_naming_the_allowed_values(apply_at):
    with pytest.raises(ValueError, match="now.*maintenance_window"):
        AttributeChange(
            resource_arn="arn",
            address=INSTANCE_ADDRESS,
            values={"multi_az": True},
            summary="x",
            apply_at=apply_at,
        )


def test_a_nested_capacity_attribute_is_never_taken_as_queued_so_verification_agrees_with_drift(monkeypatch):
    cluster_address = "aws_rds_cluster.demo_aurora"
    module = "rdscommander.operations.attribute_change"
    monkeypatch.setattr(
        f"{module}.read_attribute_values",
        lambda context, address, attributes, **kw: {"serverless_v2_max_capacity": 2.0},
    )
    monkeypatch.setattr(
        f"{module}.read_pending_modifications",
        lambda context, address, **kw: {"ServerlessV2ScalingConfiguration": {"MaxCapacity": 8.0}},
    )
    monkeypatch.setattr(f"{module}.read_resource_status", lambda context, address, **kw: "modifying")
    entry = _entry("serverless_v2_max_capacity", 8.0)
    entry.address = cluster_address
    change = AttributeChange(
        resource_arn="arn",
        address=cluster_address,
        values={"serverless_v2_max_capacity": 8.0},
        summary="x",
        apply_at="maintenance_window",
    )
    result = change.verify_direct(None, [entry])  # type: ignore[arg-type]
    assert not result.ok and result.pending == []
    assert (
        queued_attribute_values("aws_rds_cluster", {"ServerlessV2ScalingConfiguration": {"MaxCapacity": 8.0}})
        == {}
    )


def test_the_review_of_turning_backups_on_warns_of_an_outage_when_the_current_retention_is_known():
    turning_on = AttributeChange(
        resource_arn="arn",
        address=INSTANCE_ADDRESS,
        values={"backup_retention_period": 7},
        summary="x",
        known_current_values={"backup_retention_period": 0},
    )
    assert "outage" in turning_on.expected_impact(None).downtime  # type: ignore[arg-type]
    growing = AttributeChange(
        resource_arn="arn",
        address=INSTANCE_ADDRESS,
        values={"backup_retention_period": 14},
        summary="x",
        known_current_values={"backup_retention_period": 7},
    )
    assert growing.expected_impact(None).downtime == "none"  # type: ignore[arg-type]


def test_a_serverless_range_with_one_end_changed_still_sends_both_ends(monkeypatch):
    # AWS refuses a ServerlessV2ScalingConfiguration that carries only one of MinCapacity and MaxCapacity.
    module = "rdscommander.operations.attribute_change"
    monkeypatch.setattr(
        f"{module}.read_attribute_values",
        lambda context, address, attributes, **kw: {
            "serverless_v2_min_capacity": 0.5,
            "serverless_v2_max_capacity": 4.0,
        },
    )
    monkeypatch.setattr(f"{module}.read_pending_modifications", lambda context, address, **kw: {})
    submitted: list[dict] = []
    monkeypatch.setattr(
        f"{module}.apply_attribute_values",
        lambda context, address, values, apply_immediately=True: submitted.append(values) or values,
    )
    change = AttributeChange(
        resource_arn="arn",
        address="aws_rds_cluster.demo_aurora",
        values={"serverless_v2_min_capacity": 0.5, "serverless_v2_max_capacity": 8.0},
        summary="scale",
    )
    entries = change.change_set(None)  # type: ignore[arg-type]
    assert {entry.attribute for entry in entries} == {
        "serverless_v2_min_capacity",
        "serverless_v2_max_capacity",
    }
    change.apply_direct(None, entries)  # type: ignore[arg-type]
    assert submitted == [{"serverless_v2_min_capacity": 0.5, "serverless_v2_max_capacity": 8.0}]


def test_a_serverless_range_that_matches_live_is_still_nothing_to_change(monkeypatch):
    module = "rdscommander.operations.attribute_change"
    monkeypatch.setattr(
        f"{module}.read_attribute_values",
        lambda context, address, attributes, **kw: {
            "serverless_v2_min_capacity": 0.5,
            "serverless_v2_max_capacity": 4.0,
        },
    )
    change = AttributeChange(
        resource_arn="arn",
        address="aws_rds_cluster.demo_aurora",
        values={"serverless_v2_min_capacity": 0.5, "serverless_v2_max_capacity": 4.0},
        summary="scale",
    )
    assert change.change_set(None) == []  # type: ignore[arg-type]
