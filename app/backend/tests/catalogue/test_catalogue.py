import pytest

from rdscommander.catalogue.catalogue import (
    GROUP_INSTANCES_AND_CLUSTERS,
    GROUP_LIFECYCLE,
    GROUP_REPLICAS_AND_SCALING,
    GROUP_SNAPSHOTS,
    OPERATION_DEFINITIONS,
    actions_for_environment,
    actions_for_resource,
    definition_for,
    validate_catalogue,
)
from rdscommander.main import build_executors, build_services
from rdscommander.resources.queries import load_resource
from tests.terraform.conftest import arn_for


def test_every_definition_has_an_executor_schema_and_applicability(
    session_factory, operator_settings, config_store
) -> None:
    executors = build_executors(build_services(operator_settings, session_factory), config_store)
    validate_catalogue(executors)
    for definition in OPERATION_DEFINITIONS:
        schema = definition.options_model.model_json_schema()
        assert schema["type"] == "object"
        assert definition.applies_to and definition.modes


def test_definition_lookup_and_unknown_kind() -> None:
    assert definition_for("inventory_refresh").group == "Discovery and adoption"
    with pytest.raises(KeyError):
        definition_for("nope")


def test_actions_for_an_unmanaged_instance_in_terraform_mode(
    session_factory, console_config, sandbox_snapshot
) -> None:
    with session_factory() as session:
        record = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-orders")
        )
    actions = {a.kind: a for a in actions_for_resource(record, console_config.environment("sandbox"))}
    assert actions["terraform_adoption"].applicable
    assert "parameter_group_attach" not in actions or actions["parameter_group_attach"].applicable is False
    assert all(a.reason for a in actions.values() if not a.applicable)


def test_adoption_is_reported_as_unavailable_in_direct_mode(console_config) -> None:
    direct_environment = console_config.environment("sandbox").model_copy(
        update={"change_management": "direct"}
    )
    actions = {a.kind: a for a in actions_for_environment(direct_environment)}
    assert actions["inventory_refresh"].applicable
    assert actions["terraform_adoption"].applicable is False
    assert "direct" in actions["terraform_adoption"].reason


def test_validate_catalogue_fails_on_a_definition_without_executor(
    session_factory, operator_settings, config_store
) -> None:
    executors = build_executors(build_services(operator_settings, session_factory), config_store)
    executors.pop("inventory_refresh")
    with pytest.raises(RuntimeError, match="inventory_refresh"):
        validate_catalogue(executors)


def test_validate_catalogue_fails_on_an_executor_without_definition(
    session_factory, operator_settings, config_store
) -> None:
    executors = build_executors(build_services(operator_settings, session_factory), config_store)
    executors["mystery_kind"] = executors["inventory_refresh"]
    with pytest.raises(RuntimeError, match="mystery_kind"):
        validate_catalogue(executors)


def test_ui_hints_survive_json_schema() -> None:
    from rdscommander.catalogue.options import TerraformAdoptionOptions

    schema = TerraformAdoptionOptions.model_json_schema()
    assert schema["properties"]["resource_arns"]["ui"]["widget"] == "resource_picker"


def test_adoption_picker_offers_the_subnet_groups_instances_and_clusters_need_adopted_first() -> None:
    from rdscommander.catalogue.options import TerraformAdoptionOptions

    hints = TerraformAdoptionOptions.model_json_schema()["properties"]["resource_arns"]["ui"]
    assert hints["kinds"] == ["subnet_group", "parameter_group", "cluster", "instance"]
    assert hints["ownership"] == ["unmanaged"]


def test_attach_options_ask_the_picker_for_groups_of_the_resources_family_and_scope() -> None:
    from rdscommander.catalogue.options import ParameterGroupAttachOptions

    hints = ParameterGroupAttachOptions.model_json_schema()["properties"]["parameter_group_arn"]["ui"]
    assert hints["widget"] == "resource"
    assert hints["resource_kind"] == "parameter_group"
    assert hints["same_family_and_scope_as_resource"] is True


def test_drift_options_carry_their_ui_hints_and_reject_unknown_choices() -> None:
    from pydantic import ValidationError

    from rdscommander.catalogue.options import DriftResolveOptions

    properties = DriftResolveOptions.model_json_schema()["properties"]
    assert properties["resource_arn"]["ui"]["widget"] == "hidden"
    assert properties["choice"]["ui"]["widget"] == "choice"
    assert properties["choice"]["ui"]["labels"] == {
        "accept": "Accept into the record",
        "restore": "Restore from the record",
    }
    with pytest.raises(ValidationError):
        DriftResolveOptions.model_validate({"resource_arn": "arn:aws:rds:x", "choice": "ignore"})
    with pytest.raises(ValidationError):
        DriftResolveOptions.model_validate({"resource_arn": "arn:aws:rds:x", "choice": "accept", "extra": 1})


def test_detect_and_resolve_drift_are_environment_actions_in_both_modes(console_config) -> None:
    for mode in ("terraform", "direct"):
        environment = console_config.environment("sandbox").model_copy(update={"change_management": mode})
        actions = {a.kind: a for a in actions_for_environment(environment)}
        assert actions["drift_detect"].applicable, mode
        assert actions["drift_resolve"].applicable, "an inbox row without a resource page resolves from here"


def test_resolve_drift_is_offered_only_for_resources_with_open_drift(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.control_db.models import DriftFinding

    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    sandbox = console_config.environment("sandbox")
    with session_factory() as session:
        before = load_resource(session, console_config, orders_arn)
        session.add(
            DriftFinding(
                resource_arn=orders_arn,
                environment_slug="sandbox",
                target_key="sandbox/000000000000/eu-west-2",
                finding_kind="record",
                attributes=[],
            )
        )
        session.commit()
        after = load_resource(session, console_config, orders_arn)
    assert before.facts["has_open_drift"] is False and after.facts["has_open_drift"] is True
    resolve_before = next(a for a in actions_for_resource(before, sandbox) if a.kind == "drift_resolve")
    resolve_after = next(a for a in actions_for_resource(after, sandbox) if a.kind == "drift_resolve")
    assert resolve_before.applicable is False and resolve_before.reason == "no open drift on this resource"
    assert resolve_after.applicable is True
    assert resolve_after.destructive is False and resolve_after.disruptive is False


def test_every_kind_in_this_plan_is_defined_and_executable(
    session_factory, operator_settings, config_store
) -> None:
    kinds = {definition.kind for definition in OPERATION_DEFINITIONS}
    assert kinds == {
        "inventory_refresh",
        "terraform_adoption",
        "drift_detect",
        "drift_resolve",
        "environment_import",
        "parameter_group_create",
        "parameter_group_edit",
        "parameter_group_attach",
        "parameter_group_delete",
        "reboot_instance",
        "parameter_group_build_for_upgrade",
        "instance_class_change",
        "storage_change",
        "multi_az_change",
        "backup_settings_change",
        "maintenance_window_change",
        "monitoring_change",
        "deletion_protection_change",
        "serverless_capacity_change",
        "tags_change",
        "cluster_failover",
        "apply_pending_maintenance",
        "snapshot_take",
        "snapshot_delete",
        "replica_promote",
        "cluster_reader_remove",
        "decommission",
        "replica_create",
        "cluster_reader_add",
        "snapshot_restore",
        "pitr_restore",
        "instance_create",
        "cluster_create",
        "auto_minor_upgrade_change",
        "minor_upgrade",
        "major_upgrade",
        "blue_green_cleanup",
    }
    validate_catalogue(build_executors(build_services(operator_settings, session_factory), config_store))


LIFECYCLE_GROUP_NAMES = (
    GROUP_INSTANCES_AND_CLUSTERS,
    GROUP_REPLICAS_AND_SCALING,
    GROUP_SNAPSHOTS,
    GROUP_LIFECYCLE,
)
IDENTIFIER_CONFIRMED_LIFECYCLE_KINDS = {
    "cluster_failover",
    "reboot_instance",
    "snapshot_delete",
    "cluster_reader_remove",
    "decommission",
    "replica_promote",
    "instance_class_change",
}


def test_lifecycle_definitions_have_targets_full_stop_summaries_and_identifier_confirmed_flags() -> None:
    lifecycle_definitions = [d for d in OPERATION_DEFINITIONS if d.group in LIFECYCLE_GROUP_NAMES]
    assert {d.group for d in lifecycle_definitions} == set(LIFECYCLE_GROUP_NAMES)
    assert len(lifecycle_definitions) == 23  # 22 new kinds and the retitled reboot
    assert [d.kind for d in lifecycle_definitions if not d.applies_to] == []
    assert [d.kind for d in lifecycle_definitions if not d.summary.endswith(".")] == []
    flagged_kinds = {d.kind for d in lifecycle_definitions if d.destructive or d.disruptive}
    assert flagged_kinds == IDENTIFIER_CONFIRMED_LIFECYCLE_KINDS


def _mark_foreign_and_pending_reboot(session_factory, arn: str) -> None:
    from rdscommander.control_db.models import InventoryDbInstance

    with session_factory() as session:
        row = session.query(InventoryDbInstance).filter_by(db_instance_arn=arn).one()
        row.ownership = "foreign_cloudformation"
        row.parameter_apply_status = "pending-reboot"
        session.commit()


def test_reboot_is_not_offered_for_a_foreign_owned_instance_unless_the_environment_allows_it(
    session_factory, console_config, sandbox_snapshot
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _mark_foreign_and_pending_reboot(session_factory, orders_arn)
    with session_factory() as session:
        record = load_resource(session, console_config, orders_arn)
    environment = console_config.environment("sandbox")
    reboot = {a.kind: a for a in actions_for_resource(record, environment)}["reboot_instance"]
    assert reboot.applicable is False and reboot.reason == "owned by other tooling; read-only here"
    allowed = environment.model_copy(update={"allow_direct_changes_to_foreign_owned": True})
    assert {a.kind: a for a in actions_for_resource(record, allowed)}["reboot_instance"].applicable


def test_a_group_reboot_is_not_offered_when_an_instance_it_would_reboot_is_foreign_owned(
    session_factory, console_config, sandbox_snapshot
) -> None:
    _mark_foreign_and_pending_reboot(
        session_factory, arn_for(session_factory, sandbox_snapshot, "demo-orders")
    )
    with session_factory() as session:
        group = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
        )
    environment = console_config.environment("sandbox")
    reboot = {a.kind: a for a in actions_for_resource(group, environment)}["reboot_instance"]
    assert reboot.applicable is False
    assert reboot.reason == "demo-orders is owned by other tooling; read-only here"
    allowed = environment.model_copy(update={"allow_direct_changes_to_foreign_owned": True})
    assert {a.kind: a for a in actions_for_resource(group, allowed)}["reboot_instance"].applicable


SAMPLE_STEP_OPTIONS_BY_KIND = {
    "parameter_group_attach": [
        {"parameter_group_arn": "arn:aws:rds:eu-west-2:000000000000:pg:any", "reboot": "none"},
        {"parameter_group_arn": "arn:aws:rds:eu-west-2:000000000000:pg:any", "reboot": "now"},
    ],
    "drift_resolve": [{"choice": "accept"}, {"choice": "restore"}],
    "instance_class_change": [{"instance_class": "db.m6g.large"}],
    "storage_change": [{"allocated_storage_gb": 200}],
    "multi_az_change": [{"multi_az": True}],
    "backup_settings_change": [{"backup_retention_period_days": 7}],
    "maintenance_window_change": [{"preferred_maintenance_window": "sun:03:00-sun:04:00"}],
    "monitoring_change": [{"performance_insights_enabled": True, "monitoring_interval_seconds": 0}],
    "deletion_protection_change": [{"deletion_protection": True}],
    "serverless_capacity_change": [{"min_capacity_acu": 0.5, "max_capacity_acu": 4}],
    "tags_change": [{"set_tags": {"team": "orders"}}],
    "cluster_failover": [{"target_member_identifier": "demo-aurora-reader"}],
    "apply_pending_maintenance": [{"action": "system-update", "opt_in_type": "immediate"}],
    "snapshot_take": [{"snapshot_identifier": "demo-before-change"}],
    "decommission": [{"final_snapshot_identifier": "demo-orders-final"}],
    "replica_promote": [{"backup_retention_period_days": 7}],
    "replica_create": [{"replica_identifier": "demo-orders-replica-2", "instance_class": "db.m6g.large"}],
    "cluster_reader_add": [{"reader_identifier": "demo-aurora-reader-2", "instance_class": "db.r6g.large"}],
    "snapshot_restore": [
        {
            "new_identifier": "demo-orders-restored",
            "instance_class": "db.m6g.large",
            "subnet_group_name": "demo-private-subnets",
            "vpc_security_group_ids": ["sg-0123456789abcdef0"],
        }
    ],
    "pitr_restore": [
        {
            "new_identifier": "demo-orders-pitr",
            "instance_class": "db.m6g.large",
            "subnet_group_name": "demo-private-subnets",
            "vpc_security_group_ids": ["sg-0123456789abcdef0"],
            "restore_time_utc": "2026-10-11T10:00:00Z",
            "use_latest_restorable_time": False,
        }
    ],
    "instance_create": [
        {
            "target_key": "sandbox/000000000000/eu-west-2",
            "identifier": "demo-reports",
            "engine_version": "16.4",
            "instance_class": "db.m6g.large",
            "subnet_group_name": "demo-private-subnets",
            "vpc_security_group_ids": ["sg-0123456789abcdef0"],
            "parameter_group_name": "demo-postgres16-tuned",
        }
    ],
    "cluster_create": [
        {
            "target_key": "sandbox/000000000000/eu-west-2",
            "identifier": "demo-analytics",
            "engine_version": "16.4",
            "subnet_group_name": "demo-private-subnets",
            "vpc_security_group_ids": ["sg-0123456789abcdef0"],
            "cluster_parameter_group_name": "demo-aurora-pg16-tuned",
            "writer_instance_class": "db.r6g.large",
        }
    ],
}
"""Options that change an executor's step list; every other kind's steps do not depend on its options."""


def _console_config_in_mode(console_config, mode: str):  # noqa: ANN202
    environments = [
        environment.model_copy(update={"change_management": mode})
        for environment in console_config.environments
    ]
    return console_config.model_copy(update={"environments": environments})


def test_every_definition_says_whether_it_pauses_exactly_as_its_executor_steps_do_in_every_mode(
    session_factory, operator_settings, console_config
) -> None:
    from rdscommander.config.store import StaticConfigStore
    from rdscommander.control_db.models import Operation

    services = build_services(operator_settings, session_factory)
    for mode in ("terraform", "direct"):
        executors = build_executors(
            services, StaticConfigStore(_console_config_in_mode(console_config, mode), session_factory)
        )
        for definition in OPERATION_DEFINITIONS:
            if mode not in definition.modes:
                continue
            for options in SAMPLE_STEP_OPTIONS_BY_KIND.get(definition.kind, [{}]):
                operation = Operation(
                    operation_kind=definition.kind,
                    environment_slug="sandbox",
                    target_key="sandbox/000000000000/eu-west-2",
                    plan={"options": options},
                )
                pauses = "await_approval" in executors[definition.kind].step_names(operation)
                deciding_value = options.get("choice")
                expected = (
                    definition.pauses_for_approval_when[deciding_value]
                    if definition.pauses_for_approval_when and deciding_value is not None
                    else definition.pauses_for_approval
                )
                assert pauses is expected, (definition.kind, mode, options)
    assert definition_for("drift_resolve").pauses_for_approval_when == {"accept": False, "restore": True}
    assert {d.kind for d in OPERATION_DEFINITIONS if not d.pauses_for_approval} == {
        "inventory_refresh",
        "drift_detect",
        "environment_import",
    }


def _option_field_hints() -> dict[tuple[str, str], dict]:
    """The `ui` hints of every option field of every definition, keyed by (kind, field name)."""
    return {
        (definition.kind, name): field_schema.get("ui", {})
        for definition in OPERATION_DEFINITIONS
        for name, field_schema in definition.options_model.model_json_schema().get("properties", {}).items()
    }


def test_every_option_field_of_every_definition_carries_a_human_label() -> None:
    unlabelled = [
        f"{kind}.{name}"
        for (kind, name), hints in _option_field_hints().items()
        if not isinstance(hints.get("label"), str) or not hints["label"].strip()
    ]
    assert unlabelled == [], "give these option fields a label through ui_field(label=...)"


def test_option_labels_are_the_words_a_person_uses_not_the_field_names() -> None:
    labels = {key: hints.get("label") for key, hints in _option_field_hints().items()}
    assert labels[("terraform_adoption", "resource_arns")] == "Resources to adopt"
    assert labels[("parameter_group_attach", "parameter_group_arn")] == "Parameter group"
    assert labels[("parameter_group_create", "clone_from_arn")] == "Clone from"
    assert labels[("environment_import", "aws_profile_name")] == "AWS profile"
    assert labels[("environment_import", "is_production")] == "Production environment"
    assert labels[("inventory_refresh", "target_keys")] == "Targets"
    assert labels[("drift_resolve", "choice")] == "Resolution"
    assert labels[("reboot_instance", "force_failover")] == "Force failover"
    assert labels[("replica_promote", "backup_retention_period_days")] == "Backup retention (days)"
    assert labels[("replica_create", "replica_identifier")] == "Replica name"
    assert labels[("cluster_reader_add", "promotion_tier")] == "Promotion tier"
    assert labels[("snapshot_restore", "new_identifier")] == "New name"
    assert labels[("snapshot_restore", "vpc_security_group_ids")] == "Security groups"
    assert labels[("pitr_restore", "restore_time_utc")] == "Restore to"
    assert labels[("instance_create", "parameter_group_name")] == "Parameter group"
    assert labels[("instance_create", "master_username")] == "Master user name"
    assert labels[("cluster_create", "reader_count")] == "Readers"


def _record(session_factory, console_config, sandbox_snapshot, identifier):
    with session_factory() as session:
        return load_resource(session, console_config, arn_for(session_factory, sandbox_snapshot, identifier))


def test_instance_setting_rules_ask_for_an_instance_first(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.catalogue.catalogue import (
        instance_or_member_changeable,
        standalone_instance_changeable,
    )

    environment = console_config.environment("sandbox")
    for rule in (instance_or_member_changeable, standalone_instance_changeable):
        missing = rule(None, environment)
        assert (missing.ok, missing.reason) == (False, "pick an instance")
        cluster = rule(_record(session_factory, console_config, sandbox_snapshot, "demo-aurora"), environment)
        assert (cluster.ok, cluster.reason) == (False, "applies to instances")


def test_instance_setting_rules_refuse_an_instance_that_is_not_available(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.catalogue.catalogue import (
        instance_or_member_changeable,
        standalone_instance_changeable,
    )

    direct = _console_config_in_mode(console_config, "direct").environment("sandbox")
    orders = _record(session_factory, console_config, sandbox_snapshot, "demo-orders")
    modifying = orders.model_copy(update={"status": "modifying"})
    for rule in (instance_or_member_changeable, standalone_instance_changeable):
        assert rule(orders, direct).ok is True
        refused = rule(modifying, direct)
        assert refused.ok is False and "modifying" in refused.reason


def test_storage_and_multi_az_rule_refuses_a_cluster_member_that_the_class_rule_accepts(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.catalogue.catalogue import (
        instance_or_member_changeable,
        standalone_instance_changeable,
    )

    direct = _console_config_in_mode(console_config, "direct").environment("sandbox")
    writer = _record(session_factory, console_config, sandbox_snapshot, "demo-aurora-writer")
    assert writer.relationships["cluster_identifier"] == "demo-aurora"
    assert instance_or_member_changeable(writer, direct).ok is True
    refused = standalone_instance_changeable(writer, direct)
    assert refused.ok is False and "set this on the cluster" in refused.reason


def test_backup_settings_and_deletion_protection_are_refused_on_an_aurora_member_and_offered_on_its_cluster(
    session_factory, console_config, sandbox_snapshot
) -> None:
    # Aurora keeps backups and deletion protection on the cluster; AWS does not apply them to a member, and
    # the Actions menu of a member offered both (backup settings then failed on submit).
    from rdscommander.catalogue.catalogue import OPERATION_DEFINITIONS

    direct = _console_config_in_mode(console_config, "direct").environment("sandbox")
    writer = _record(session_factory, console_config, sandbox_snapshot, "demo-aurora-writer")
    cluster = _record(session_factory, console_config, sandbox_snapshot, "demo-aurora")
    orders = _record(session_factory, console_config, sandbox_snapshot, "demo-orders")
    for kind in ("backup_settings_change", "deletion_protection_change"):
        definition = next(d for d in OPERATION_DEFINITIONS if d.kind == kind)
        refused = definition.applicability(writer, direct)
        assert refused.ok is False and "set this on the cluster" in refused.reason
        assert definition.applicability(cluster, direct).ok is True
        assert definition.applicability(orders, direct).ok is True


def test_instance_setting_rules_refuse_foreign_owned_and_unadopted_instances_as_the_change_gate_does(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.catalogue.catalogue import (
        FOREIGN_OWNED_REFUSAL,
        instance_or_member_changeable,
        standalone_instance_changeable,
    )

    terraform = console_config.environment("sandbox")
    direct = _console_config_in_mode(console_config, "direct").environment("sandbox")
    orders = _record(session_factory, console_config, sandbox_snapshot, "demo-orders")
    foreign = orders.model_copy(update={"ownership": "foreign_terraform"})
    for rule in (instance_or_member_changeable, standalone_instance_changeable):
        assert rule(foreign, direct).reason == FOREIGN_OWNED_REFUSAL
        unadopted = rule(orders, terraform)
        assert unadopted.ok is False and "adopt it first" in unadopted.reason
        assert rule(orders, direct).ok is True
        assert rule(orders.model_copy(update={"ownership": "adopted"}), terraform).ok is True


def test_instance_or_cluster_rules_ask_for_the_right_kind_and_an_available_resource(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.catalogue.catalogue import (
        cluster_changeable,
        instance_or_cluster_changeable,
        serverless_cluster_changeable,
    )

    direct = _console_config_in_mode(console_config, "direct").environment("sandbox")
    orders = _record(session_factory, console_config, sandbox_snapshot, "demo-orders")
    aurora = _record(session_factory, console_config, sandbox_snapshot, "demo-aurora")
    group = _record(session_factory, console_config, sandbox_snapshot, "demo-postgres16-tuned")
    assert (
        instance_or_cluster_changeable(orders, direct).ok
        and instance_or_cluster_changeable(aurora, direct).ok
    )
    assert instance_or_cluster_changeable(group, direct).reason == "applies to instances and clusters"
    assert instance_or_cluster_changeable(None, direct).ok is False
    assert (
        "modifying"
        in instance_or_cluster_changeable(aurora.model_copy(update={"status": "modifying"}), direct).reason
    )
    assert cluster_changeable(aurora, direct).ok is True
    assert cluster_changeable(orders, direct).reason == "applies to clusters"
    refused = serverless_cluster_changeable(aurora, direct)
    assert refused.ok is False and "serverless" in refused.reason
    serverless = aurora.model_copy(update={"facts": {**aurora.facts, "serverless_v2_min_capacity": 0.5}})
    assert serverless_cluster_changeable(serverless, direct).ok is True


def test_taggable_accepts_groups_but_not_default_groups_or_other_kinds(
    session_factory, console_config, sandbox_snapshot
) -> None:
    from rdscommander.catalogue.catalogue import taggable

    direct = _console_config_in_mode(console_config, "direct").environment("sandbox")
    group = _record(session_factory, console_config, sandbox_snapshot, "demo-postgres16-tuned")
    assert taggable(group, direct).ok is True
    default_group = group.model_copy(update={"facts": {**group.facts, "is_default": True}})
    assert taggable(default_group, direct).reason == "AWS default groups cannot be tagged"
    assert taggable(None, direct).ok is False
    subnet_group = group.model_copy(update={"kind": "subnet_group"})
    assert taggable(subnet_group, direct).ok is False


def test_storage_optimisation_blocks_only_another_storage_change(
    session_factory, console_config, sandbox_snapshot
) -> None:
    # AWS reports storage-optimization for hours after a storage change and allows every other modification
    # meanwhile; only a further storage change has to wait.
    from rdscommander.catalogue.catalogue import OPERATION_DEFINITIONS, status_allows_changes

    assert status_allows_changes("available") and status_allows_changes("storage-optimization")
    assert not status_allows_changes("modifying") and not status_allows_changes(None)
    direct = _console_config_in_mode(console_config, "direct").environment("sandbox")
    for identifier in ("demo-orders", "demo-orders-replica"):
        available = _record(session_factory, console_config, sandbox_snapshot, identifier)
        optimising = available.model_copy(update={"status": "storage-optimization"})
        offered_when_available = {
            d.kind for d in OPERATION_DEFINITIONS if d.applicability(available, direct).ok
        }
        offered_when_optimising = {
            d.kind for d in OPERATION_DEFINITIONS if d.applicability(optimising, direct).ok
        }
        assert {"instance_class_change", "multi_az_change", "reboot_instance", "snapshot_take"} <= (
            offered_when_optimising
        )
        assert offered_when_optimising == offered_when_available - {"storage_change"}
        refused = definition_for("storage_change").applicability(optimising, direct)
        assert refused.ok is False and "six hours" in refused.reason
