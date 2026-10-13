import json
from pathlib import Path

import pytest

from rdscommander.terraform.plan_parser import summarise_plan_json
from rdscommander.terraform.policy import evaluate_adoption_policy, evaluate_change_policy

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "plan_with_import_and_update.json").read_text())


def test_replace_on_a_cluster_is_refused_outright() -> None:
    decision = evaluate_adoption_policy(
        summarise_plan_json(FIXTURE), {"aws_db_parameter_group.demo", "aws_db_instance.orders"}
    )
    assert not decision.allowed
    assert any("aws_rds_cluster.legacy" in r and "replace" in r for r in decision.refusals)


def test_import_plus_in_place_update_is_allowed() -> None:
    trimmed = {"resource_changes": FIXTURE["resource_changes"][:2]}
    decision = evaluate_adoption_policy(
        summarise_plan_json(trimmed), {"aws_db_parameter_group.demo", "aws_db_instance.orders"}
    )
    assert decision.allowed and decision.refusals == []


def test_selected_resource_not_imported_is_refused() -> None:
    trimmed = {"resource_changes": FIXTURE["resource_changes"][:1]}
    decision = evaluate_adoption_policy(
        summarise_plan_json(trimmed), {"aws_db_parameter_group.demo", "aws_db_instance.orders"}
    )
    assert not decision.allowed
    assert any("aws_db_instance.orders" in r and "absent" in r for r in decision.refusals)


def test_create_of_a_selected_resource_is_refused_because_it_would_duplicate() -> None:
    created = {
        "resource_changes": [
            {
                "address": "aws_db_instance.orders",
                "type": "aws_db_instance",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {},
                    "before_sensitive": {},
                    "after_sensitive": {},
                },
            }
        ]
    }
    decision = evaluate_adoption_policy(summarise_plan_json(created), {"aws_db_instance.orders"})
    assert not decision.allowed and any("create" in r for r in decision.refusals)


def _update(address: str, resource_type: str, before: dict, after: dict) -> dict:
    return {
        "address": address,
        "type": resource_type,
        "change": {
            "actions": ["update"],
            "before": before,
            "after": after,
            "before_sensitive": {},
            "after_sensitive": {},
            "importing": {"id": address.split(".")[1]},
        },
    }


def test_in_place_update_of_a_real_attribute_on_an_instance_is_refused() -> None:
    plan = {
        "resource_changes": [
            _update(
                "aws_db_instance.orders",
                "aws_db_instance",
                {"instance_class": "db.m6g.large", "tags_all": {}},
                {"instance_class": "db.r6g.xlarge", "tags_all": {"rdscommander:managed": "true"}},
            )
        ]
    }
    decision = evaluate_adoption_policy(summarise_plan_json(plan), {"aws_db_instance.orders"})
    assert not decision.allowed
    assert any("instance_class" in r and "aws_db_instance.orders" in r for r in decision.refusals)


def test_tag_and_config_only_updates_on_an_instance_are_allowed() -> None:
    plan = {
        "resource_changes": [
            _update(
                "aws_db_instance.orders",
                "aws_db_instance",
                {"tags_all": {}, "skip_final_snapshot": None, "apply_immediately": None},
                {
                    "tags_all": {"rdscommander:managed": "true"},
                    "skip_final_snapshot": False,
                    "apply_immediately": False,
                },
            )
        ]
    }
    assert evaluate_adoption_policy(summarise_plan_json(plan), {"aws_db_instance.orders"}).allowed


def test_attribute_updates_on_unprotected_types_are_allowed() -> None:
    plan = {
        "resource_changes": [
            _update(
                "aws_db_parameter_group.demo",
                "aws_db_parameter_group",
                {"parameter": []},
                {"parameter": [{"name": "work_mem", "value": "1"}]},
            )
        ]
    }
    assert evaluate_adoption_policy(summarise_plan_json(plan), {"aws_db_parameter_group.demo"}).allowed


def _plan_with(*changes: tuple[str, list[str]]) -> dict:
    return {
        "resource_changes": [
            {
                "address": address,
                "type": address.split(".")[0],
                "change": {"actions": actions, "before": {}, "after": {}},
            }
            for address, actions in changes
        ]
    }


def test_change_policy_allows_an_in_place_update_of_a_touched_address() -> None:
    summary = summarise_plan_json(_plan_with(("aws_db_parameter_group.demo", ["update"])))
    decision = evaluate_change_policy(summary, {"aws_db_parameter_group.demo"})
    assert decision.allowed and decision.refusals == []


def test_change_policy_ignores_no_op_entries() -> None:
    summary = summarise_plan_json(
        _plan_with(("aws_db_instance.orders", ["no-op"]), ("aws_db_parameter_group.demo", ["update"]))
    )
    assert evaluate_change_policy(summary, {"aws_db_parameter_group.demo"}).allowed


def test_change_policy_refuses_an_update_outside_the_touched_addresses() -> None:
    summary = summarise_plan_json(_plan_with(("aws_db_instance.orders", ["update"])))
    decision = evaluate_change_policy(summary, {"aws_db_parameter_group.demo"})
    assert not decision.allowed
    assert decision.refusals == ["aws_db_instance.orders: not part of this operation"]


@pytest.mark.parametrize("actions", [["create"], ["delete"], ["delete", "create"], ["create", "delete"]])
def test_change_policy_refuses_creates_deletes_and_replaces_by_default(actions: list[str]) -> None:
    summary = summarise_plan_json(_plan_with(("aws_db_parameter_group.demo", actions)))
    decision = evaluate_change_policy(summary, {"aws_db_parameter_group.demo"})
    assert not decision.allowed
    assert "is not allowed by a change operation" in decision.refusals[0]


def test_change_policy_allows_a_delete_only_of_an_address_the_operation_may_delete() -> None:
    summary = summarise_plan_json(_plan_with(("aws_db_parameter_group.demo", ["delete"])))
    permitted = evaluate_change_policy(
        summary,
        {"aws_db_parameter_group.demo"},
        allow_delete_of=frozenset({"aws_db_parameter_group.demo"}),
    )
    assert permitted.allowed
    other = evaluate_change_policy(
        summary,
        {"aws_db_parameter_group.demo"},
        allow_delete_of=frozenset({"aws_db_parameter_group.other"}),
    )
    assert not other.allowed


def test_change_policy_allows_a_create_only_of_an_address_the_operation_may_create() -> None:
    summary = summarise_plan_json(_plan_with(("aws_db_parameter_group.demo", ["create"])))
    permitted = evaluate_change_policy(
        summary,
        {"aws_db_parameter_group.demo"},
        allow_create_of=frozenset({"aws_db_parameter_group.demo"}),
    )
    assert permitted.allowed
    assert not evaluate_change_policy(summary, {"aws_db_parameter_group.demo"}).allowed


def test_change_policy_never_allows_a_replace_even_when_create_and_delete_are_allowed() -> None:
    summary = summarise_plan_json(_plan_with(("aws_db_parameter_group.demo", ["delete", "create"])))
    decision = evaluate_change_policy(
        summary,
        {"aws_db_parameter_group.demo"},
        allow_delete_of=frozenset({"aws_db_parameter_group.demo"}),
        allow_create_of=frozenset({"aws_db_parameter_group.demo"}),
    )
    assert not decision.allowed


def _plain_update(address: str, before: dict, after: dict) -> dict:
    return {
        "address": address,
        "type": address.split(".")[0],
        "change": {
            "actions": ["update"],
            "before": before,
            "after": after,
            "before_sensitive": {},
            "after_sensitive": {},
        },
    }


SUBNET_GROUP_IMPORT = {
    "address": "aws_db_subnet_group.private",
    "type": "aws_db_subnet_group",
    "change": {
        "actions": ["no-op"],
        "before": {},
        "after": {},
        "before_sensitive": {},
        "after_sensitive": {},
        "importing": {"id": "private"},
    },
}


def test_an_adoption_that_would_change_an_earlier_adoption_is_refused_until_its_drift_is_resolved() -> None:
    """Rendered from the record, a drifted earlier adoption plans an update the adoption must not apply."""
    drifted = _plain_update(
        "aws_db_parameter_group.demo",
        {"parameter": [{"name": "work_mem", "value": "65536"}]},
        {"parameter": [{"name": "work_mem", "value": "16384"}]},
    )
    decision = evaluate_adoption_policy(
        summarise_plan_json({"resource_changes": [drifted, SUBNET_GROUP_IMPORT]}),
        {"aws_db_subnet_group.private"},
        adopted_earlier={"aws_db_parameter_group.demo"},
    )
    assert not decision.allowed
    [refusal] = decision.refusals
    assert refusal.startswith("aws_db_parameter_group.demo: the plan would change parameter")
    assert refusal.endswith("Resolve drift on aws_db_parameter_group.demo first")
    tags_only = _plain_update("aws_db_parameter_group.demo", {"tags": {}}, {"tags": {"a": "b"}})
    assert evaluate_adoption_policy(
        summarise_plan_json({"resource_changes": [tags_only, SUBNET_GROUP_IMPORT]}),
        {"aws_db_subnet_group.private"},
        adopted_earlier={"aws_db_parameter_group.demo"},
    ).allowed, "tags and configuration-only attributes are not drift worth stopping for"


def test_change_policy_lets_an_untouched_address_take_the_apply_immediately_line_0_2_0_lacked() -> None:
    """After upgrading from 0.2.0 the first re-render adds `apply_immediately` to every resource.

    That update never reaches AWS, so it does not refuse the operation; the review names the address.
    """
    plan = {
        "resource_changes": [
            _plain_update("aws_db_parameter_group.demo", {"parameter": []}, {"parameter": [{"name": "a"}]}),
            _plain_update(
                "aws_db_instance.orders",
                {"instance_class": "db.t3.micro", "apply_immediately": None},
                {"instance_class": "db.t3.micro", "apply_immediately": True},
            ),
            _plain_update(
                "aws_rds_cluster_instance.ledger_1",
                {"promotion_tier": 1, "apply_immediately": None, "final_snapshot_identifier": None},
                {"promotion_tier": 1, "apply_immediately": True, "final_snapshot_identifier": None},
            ),
        ]
    }
    decision = evaluate_change_policy(summarise_plan_json(plan), {"aws_db_parameter_group.demo"})
    assert decision.allowed, decision.refusals
    assert decision.also_brought_up_to_date == ["aws_db_instance.orders", "aws_rds_cluster_instance.ledger_1"]


def test_change_policy_still_refuses_an_untouched_address_whose_update_reaches_aws() -> None:
    plan = {
        "resource_changes": [
            _plain_update(
                "aws_db_instance.orders",
                {"instance_class": "db.t3.micro", "apply_immediately": None},
                {"instance_class": "db.t3.small", "apply_immediately": True},
            ),
        ]
    }
    decision = evaluate_change_policy(summarise_plan_json(plan), {"aws_db_parameter_group.demo"})
    assert not decision.allowed
    assert decision.refusals == ["aws_db_instance.orders: not part of this operation"]
    assert decision.also_brought_up_to_date == []


def test_change_policy_lets_an_untouched_address_drop_the_default_tag_0_4_0_rendered() -> None:
    """After upgrading from 0.4.0 the first re-render removes the `rdscommander:company` default tag.

    Only the provider's mirror of the default tags changes on each resource, so it does not refuse the
    operation; the review names the address.
    """
    plan = {
        "resource_changes": [
            _plain_update("aws_db_parameter_group.demo", {"parameter": []}, {"parameter": [{"name": "a"}]}),
            _plain_update(
                "aws_db_instance.orders",
                {
                    "instance_class": "db.t3.micro",
                    "tags_all": {"rdscommander:company": "x", "rdscommander:managed": "true"},
                },
                {"instance_class": "db.t3.micro", "tags_all": {"rdscommander:managed": "true"}},
            ),
        ]
    }
    decision = evaluate_change_policy(summarise_plan_json(plan), {"aws_db_parameter_group.demo"})
    assert decision.allowed, decision.refusals
    assert decision.also_brought_up_to_date == ["aws_db_instance.orders"]
