import json
from pathlib import Path

from rdscommander.terraform.plan_parser import redact_plan_json, summarise_plan_json

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "plan_with_import_and_update.json").read_text())


def test_summary_counts_actions_and_marks_imports() -> None:
    summary = summarise_plan_json(FIXTURE)
    by_address = {c.address: c for c in summary.resource_changes}
    assert by_address["aws_db_parameter_group.demo"].is_import
    assert by_address["aws_db_parameter_group.demo"].actions == ["no-op"]
    assert by_address["aws_db_instance.orders"].is_import and by_address[
        "aws_db_instance.orders"
    ].actions == ["update"]
    assert by_address["aws_rds_cluster.legacy"].actions == ["delete", "create"]
    counts = (
        summary.import_count,
        summary.update_count,
        summary.replace_count,
        summary.delete_count,
        summary.no_op_count,
    )
    assert counts == (2, 1, 1, 0, 1)
    assert summary.has_changes


def test_summary_redacts_sensitive_values_and_fingerprint_is_stable() -> None:
    summary = summarise_plan_json(FIXTURE)
    orders = next(c for c in summary.resource_changes if c.address == "aws_db_instance.orders")
    assert orders.after["password"] == "[REDACTED]" and orders.before["password"] == "[REDACTED]"
    assert summary.fingerprint() == summarise_plan_json(FIXTURE).fingerprint()
    changed = json.loads(json.dumps(FIXTURE))
    changed["resource_changes"][1]["change"]["after"]["identifier"] = "orders-2"
    assert summary.fingerprint() != summarise_plan_json(changed).fingerprint()


def test_fingerprint_ignores_read_only_values_that_drift_between_plans() -> None:
    first = json.loads(json.dumps(FIXTURE))
    second = json.loads(json.dumps(FIXTURE))
    for plan, stamp in ((first, "2026-10-10T15:15:00Z"), (second, "2026-10-10T15:15:04Z")):
        change = plan["resource_changes"][1]["change"]
        change["before"]["latest_restorable_time"] = stamp
        change["after"]["latest_restorable_time"] = stamp
    assert summarise_plan_json(first).fingerprint() == summarise_plan_json(second).fingerprint()


def test_redact_plan_json_masks_resource_changes_and_planned_values() -> None:
    redacted = redact_plan_json(FIXTURE)
    assert redacted["resource_changes"][1]["change"]["after"]["password"] == "[REDACTED]"
    assert redacted["planned_values"]["root_module"]["resources"][0]["values"]["password"] == "[REDACTED]"
    assert redacted["planned_values"]["root_module"]["resources"][0]["values"]["identifier"] == "orders"
    assert FIXTURE["resource_changes"][1]["change"]["after"]["password"] == "hunter2"
