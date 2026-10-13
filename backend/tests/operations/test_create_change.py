"""The resource-create change: the base every replica, reader, restore and create builds on."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rdscommander.operations.change_strategy import ResourceChangeExecutor
from rdscommander.operations.create_change import (
    CREATED_RESOURCE_ARNS_PLAN_KEY,
    NEW_RESOURCE_DOWNTIME,
    ResourceAlreadyExistsError,
    ResourceCreateChange,
    ResourceCreateExecutor,
    expected_arn_of,
    sibling_arn_of,
)
from rdscommander.terraform.render_input import RenderedInstance

NEW_ARN = "arn:aws:rds:eu-west-2:000000000000:db:demo-orders-replica-2"


class StubCreate(ResourceCreateChange):
    """A create whose AWS answers are given up front."""

    def __init__(
        self,
        identifier: str,
        template: RenderedInstance | None = None,
        already_exists: bool = False,
        status_answer: str | None = "available",
        link_operation_to_created: bool = False,
    ) -> None:
        super().__init__(
            "sandbox/000000000000/eu-west-2",
            identifier,
            "aws_db_instance",
            f"create {identifier}",
            link_operation_to_created=link_operation_to_created,
        )
        self.template = template
        self.already_exists = already_exists
        self.status_answer = status_answer

    def rendered(self, context: Any) -> RenderedInstance:
        assert self.template is not None
        return self.template.model_copy(
            update={"identifier": self.identifier, "terraform_name": self.address.split(".", 1)[1]}
        )

    def create(self, context: Any) -> str:
        return NEW_ARN

    def exists(self, context: Any, step_name: str = "compute_change_set") -> bool:
        return self.already_exists

    def status(self, context: Any, step_name: str = "verify_direct") -> str | None:
        return self.status_answer

    def review_entries(self, context: Any) -> list[tuple[str, Any]]:
        return [("instance_class", "db.m6g.large")]


def _context(plan: dict | None = None, resource_arn: str | None = "arn:source") -> SimpleNamespace:
    commits: list[bool] = []
    return SimpleNamespace(
        operation=SimpleNamespace(plan=dict(plan or {}), resource_arn=resource_arn),
        session=SimpleNamespace(commit=lambda: commits.append(True)),
        commits=commits,
    )


def _orders(sandbox_render_input) -> RenderedInstance:
    return next(i for i in sandbox_render_input.instances if i.identifier == "demo-orders")


def test_apply_to_render_input_appends_the_new_resource_and_sorts_the_section(sandbox_render_input) -> None:
    change = StubCreate("demo-a-first", template=_orders(sandbox_render_input))
    change.prepare(_context())
    updated = change.apply_to_render_input(sandbox_render_input)
    identifiers = [instance.identifier for instance in updated.instances]
    assert identifiers == sorted(identifiers)
    assert "demo-a-first" in identifiers
    assert "demo-a-first" not in [i.identifier for i in sandbox_render_input.instances], "a copy is changed"


def test_apply_to_render_input_replaces_an_entry_of_the_same_name(sandbox_render_input) -> None:
    template = _orders(sandbox_render_input).model_copy(update={"instance_class": "db.r6g.xlarge"})
    change = StubCreate("demo-orders", template=template)
    change.prepare(_context())
    updated = change.apply_to_render_input(sandbox_render_input)
    [orders] = [i for i in updated.instances if i.identifier == "demo-orders"]
    assert orders.instance_class == "db.r6g.xlarge"


def test_apply_to_render_input_needs_prepare_first(sandbox_render_input) -> None:
    change = StubCreate("demo-a-first", template=_orders(sandbox_render_input))
    with pytest.raises(ValueError, match="prepare"):
        change.apply_to_render_input(sandbox_render_input)


def test_change_set_refuses_an_identifier_that_exists_already() -> None:
    with pytest.raises(ResourceAlreadyExistsError) as raised:
        StubCreate("demo-orders", already_exists=True).change_set(_context())
    assert raised.value.diagnostic_kind == "resource_already_exists"
    assert "demo-orders already exists" in str(raised.value)


def test_change_set_is_presence_then_the_review_entries() -> None:
    presence, instance_class = StubCreate("demo-orders-replica-2").change_set(_context())
    assert (presence.attribute, presence.current, presence.desired) == (
        "presence",
        None,
        "aws_db_instance demo-orders-replica-2",
    )
    assert presence.address == "aws_db_instance.demo_orders_replica_2"
    assert presence.downtime == NEW_RESOURCE_DOWNTIME and presence.requires_reboot is False
    assert (instance_class.attribute, instance_class.desired) == ("instance_class", "db.m6g.large")


def test_expected_impact_is_no_reboot_and_no_downtime_for_anything_existing() -> None:
    impact = StubCreate("demo-orders-replica-2").expected_impact(_context())
    assert impact.requires_reboot is False and impact.downtime == NEW_RESOURCE_DOWNTIME


def test_apply_direct_records_the_created_arn_and_keeps_the_operation_on_its_source() -> None:
    context = _context({CREATED_RESOURCE_ARNS_PLAN_KEY: ["arn:earlier"]})
    submitted = StubCreate("demo-orders-replica-2").apply_direct(context, [])
    assert submitted == {"arn": NEW_ARN}
    assert context.operation.resource_arn == "arn:source"
    assert context.operation.plan[CREATED_RESOURCE_ARNS_PLAN_KEY] == ["arn:earlier", NEW_ARN]
    assert context.commits


def test_apply_direct_links_the_operation_to_the_new_resource_when_asked() -> None:
    context = _context(resource_arn=None)
    StubCreate("demo-orders-replica-2", link_operation_to_created=True).apply_direct(context, [])
    assert context.operation.resource_arn == NEW_ARN
    assert context.operation.plan[CREATED_RESOURCE_ARNS_PLAN_KEY] == [NEW_ARN]


def test_verify_direct_waits_for_available_and_stops_on_a_terminal_status() -> None:
    assert StubCreate("x", status_answer="available").verify_direct(_context(), []).ok is True
    creating = StubCreate("x", status_answer="creating").verify_direct(_context(), [])
    assert creating.ok is False and creating.terminal is False
    failed = StubCreate("x", status_answer="failed").verify_direct(_context(), [])
    assert failed.ok is False and failed.terminal is True and "x is failed" in failed.detail


def test_verify_direct_does_not_give_up_while_aws_does_not_list_the_new_resource_yet() -> None:
    missing = StubCreate("x", status_answer=None).verify_direct(_context(), [])
    assert missing.ok is False and missing.terminal is False


def test_the_executor_base_allows_the_plain_create_and_guards_nothing() -> None:
    assert issubclass(ResourceCreateExecutor, ResourceChangeExecutor)
    assert ResourceCreateExecutor.allows_create is True
    assert ResourceCreateExecutor.refuses_open_record_drift is False
    assert ResourceCreateExecutor(config_store=None)._guard_resource(_context(), "check_checkout") is None


def test_expected_arn_of_names_instances_members_and_clusters() -> None:
    target = SimpleNamespace(aws_region="eu-west-2", aws_account_id="000000000000")
    prefix = "arn:aws:rds:eu-west-2:000000000000"
    assert expected_arn_of(target, "aws_db_instance", "a") == f"{prefix}:db:a"
    assert expected_arn_of(target, "aws_rds_cluster_instance", "b") == f"{prefix}:db:b"
    assert expected_arn_of(target, "aws_rds_cluster", "c") == f"{prefix}:cluster:c"


def test_sibling_arn_of_keeps_the_account_and_region_of_the_launching_resource() -> None:
    prefix = "arn:aws:rds:eu-west-2:123456789012"
    assert sibling_arn_of(f"{prefix}:db:demo-orders", "aws_db_instance", "r2") == f"{prefix}:db:r2"
    member_arn = sibling_arn_of(f"{prefix}:cluster:demo-aurora", "aws_rds_cluster_instance", "x")
    assert member_arn == f"{prefix}:db:x"


def test_a_create_declares_no_create_only_fields_unless_it_overrides_them() -> None:
    assert StubCreate("demo-orders-replica-2").create_only_fields() == {}


def test_the_record_as_built_keeps_the_create_only_fields_the_change_declares(sandbox_render_input) -> None:
    class RestoreLikeCreate(StubCreate):
        def create_only_fields(self) -> dict[str, Any]:
            return {"snapshot_identifier": "demo-orders-before-change", "manage_master_user_password": True}

    executor = ResourceCreateExecutor(config_store=None)
    updated = executor._with_create_only_fields(sandbox_render_input, RestoreLikeCreate("demo-orders"))
    orders = next(i for i in updated.instances if i.identifier == "demo-orders")
    assert orders.snapshot_identifier == "demo-orders-before-change"
    assert orders.manage_master_user_password is True
    replica = next(i for i in updated.instances if i.identifier == "demo-orders-replica")
    assert replica.snapshot_identifier is None, "only the created resource takes them"
    assert _orders(sandbox_render_input).snapshot_identifier is None, "the input is copied"


def test_only_terraform_mode_adds_the_record_as_built_step(config_store) -> None:
    from rdscommander.control_db.models import Operation
    from rdscommander.operations.change_strategy import DIRECT_STEPS
    from tests.operations.test_change_strategy import _direct_store

    operation = Operation(
        operation_kind="replica_create", target_key="sandbox/000000000000/eu-west-2", plan={}
    )
    terraform_steps = ResourceCreateExecutor(config_store).step_names(operation)
    assert terraform_steps[-3:] == ["commit_to_checkout", "refresh_resource", "record_as_built"]
    assert ResourceCreateExecutor(_direct_store(config_store)).step_names(operation) == DIRECT_STEPS
