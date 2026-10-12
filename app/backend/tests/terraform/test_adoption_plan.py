import pytest

from rdscommander.terraform.adoption_plan import AdoptionPlanError, build_adoption_plan
from tests.terraform.conftest import TARGET_KEY, arn_for


def test_plan_requires_prerequisites_in_the_same_selection(
    session_factory, console_config, sandbox_snapshot
) -> None:
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session, pytest.raises(AdoptionPlanError) as error:
        build_adoption_plan(session, console_config, TARGET_KEY, [orders])
    assert any("demo-postgres16-tuned" in p for p in error.value.problems)
    assert any("demo-private-subnets" in p for p in error.value.problems)


def test_plan_accepts_a_complete_selection_and_orders_addresses(
    session_factory, console_config, sandbox_snapshot
) -> None:
    arns = [
        arn_for(session_factory, sandbox_snapshot, name)
        for name in ("demo-orders", "demo-postgres16-tuned", "demo-private-subnets")
    ]
    with session_factory() as session:
        plan = build_adoption_plan(session, console_config, TARGET_KEY, arns)
    assert [r.terraform_address for r in plan.selected_resources] == [
        "aws_db_parameter_group.demo_postgres16_tuned",
        "aws_db_subnet_group.demo_private_subnets",
        "aws_db_instance.demo_orders",
    ]


def test_plan_rejects_unknown_and_default_resources(
    session_factory, console_config, sandbox_snapshot
) -> None:
    with session_factory() as session, pytest.raises(AdoptionPlanError, match="unknown"):
        build_adoption_plan(
            session, console_config, TARGET_KEY, ["arn:aws:rds:eu-west-2:000000000000:db:nope"]
        )
