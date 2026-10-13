from pathlib import Path

from rdscommander.execution_records.service import ExecutionRecorder
from rdscommander.terraform.runner import TerraformRunner


def test_environment_for_real_target_sets_profile_and_automation_flags(
    operator_settings, console_config, session_factory
) -> None:
    runner = TerraformRunner("tofu", ExecutionRecorder(session_factory))
    env = runner.environment_for(
        console_config.target("production/123456789012/eu-west-2"), operator_settings
    )
    assert env["AWS_PROFILE"] == "example-prod" and env["TF_IN_AUTOMATION"] == "1" and env["TF_INPUT"] == "0"
    assert "AWS_ACCESS_KEY_ID" not in env


def test_environment_for_simulated_target_uses_dummy_credentials(
    operator_settings, console_config, session_factory
) -> None:
    runner = TerraformRunner("tofu", ExecutionRecorder(session_factory))
    env = runner.environment_for(console_config.target("sandbox/000000000000/eu-west-2"), operator_settings)
    assert env["AWS_ACCESS_KEY_ID"] == "testing" and "AWS_PROFILE" not in env


def test_missing_binary_is_recorded_not_raised(
    operator_settings, console_config, session_factory, tmp_path: Path
) -> None:
    runner = TerraformRunner("definitely-not-a-binary", ExecutionRecorder(session_factory))
    result = runner.init(
        tmp_path,
        console_config.target("sandbox/000000000000/eu-west-2"),
        operator_settings,
        operation_id=None,
        step_name="init",
    )
    assert result.exit_code == 127


def test_plan_stores_redacted_plan_json_in_the_execution_record(
    operator_settings, console_config, session_factory, tmp_path: Path
) -> None:
    """The stored `show -json` output must carry the plan's own sensitivity markers applied."""
    import json
    import stat

    from rdscommander.control_db.models import ExecutionRecord

    plan_json = {
        "resource_changes": [
            {
                "address": "aws_db_instance.orders",
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"password": "hunter2"},
                    "after": {"password": "hunter2"},
                    "before_sensitive": {"password": True},
                    "after_sensitive": {"password": True},
                },
            }
        ]
    }
    fake = tmp_path / "fake-tofu"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "plan" ]; then exit 2; fi\n'
        f'if [ "$1" = "show" ]; then cat <<\'EOF_PLAN\'\n{json.dumps(plan_json)}\nEOF_PLAN\nexit 0; fi\n'
        "exit 1\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    runner = TerraformRunner(str(fake), ExecutionRecorder(session_factory))
    result = runner.plan(
        tmp_path,
        console_config.target("sandbox/000000000000/eu-west-2"),
        operator_settings,
        operation_id=None,
        step_name="plan",
    )
    assert (
        result.has_changes
        and result.plan_json["resource_changes"][0]["change"]["after"]["password"] == "hunter2"
    )
    with session_factory() as session:
        stored = [
            r.standard_output or "" for r in session.query(ExecutionRecord).filter_by(record_kind="terraform")
        ]
    assert any("[REDACTED]" in text for text in stored)
    assert all("hunter2" not in text for text in stored)


def test_apply_refresh_only_refreshes_state_without_prompting(
    operator_settings, console_config, session_factory, tmp_path: Path
) -> None:
    import stat

    arguments_file = tmp_path / "arguments.txt"
    fake = tmp_path / "fake-tofu"
    fake.write_text(f'#!/usr/bin/env bash\necho "$@" > "{arguments_file}"\nexit 0\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    runner = TerraformRunner(str(fake), ExecutionRecorder(session_factory))
    result = runner.apply_refresh_only(
        tmp_path,
        console_config.target("sandbox/000000000000/eu-west-2"),
        operator_settings,
        operation_id=None,
        step_name="refresh_state",
    )
    assert result.succeeded
    assert arguments_file.read_text().split() == ["apply", "-refresh-only", "-auto-approve", "-input=false"]


def test_apply_refresh_only_reports_a_failing_binary_as_unsuccessful(
    operator_settings, console_config, session_factory, tmp_path: Path
) -> None:
    runner = TerraformRunner("definitely-not-a-binary", ExecutionRecorder(session_factory))
    result = runner.apply_refresh_only(
        tmp_path,
        console_config.target("sandbox/000000000000/eu-west-2"),
        operator_settings,
        operation_id=None,
        step_name="refresh_state",
    )
    assert not result.succeeded and result.exit_code == 127


def test_state_show_returns_the_addresses_attributes_and_records_only_its_identity(
    operator_settings, console_config, session_factory, tmp_path: Path
) -> None:
    """OpenTofu writes `state show -json` as JSON lines; only the identity attributes are kept on record."""
    import json
    import stat

    from rdscommander.control_db.models import ExecutionRecord

    state_document = {
        "format_version": "1.0",
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_db_instance.demo_orders",
                        "type": "aws_db_instance",
                        "values": {
                            "identifier": "demo-orders",
                            "id": "db-GREENRESOURCEID",
                            "resource_id": "db-GREENRESOURCEID",
                            "password": "hunter2",
                        },
                    }
                ]
            }
        },
    }
    arguments_file = tmp_path / "arguments.txt"
    fake = tmp_path / "fake-tofu"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" > "{arguments_file}"\n'
        'echo \'{"@level":"info","@message":"OpenTofu 1.13.1","type":"version"}\'\n'
        f"cat <<'EOF_STATE'\n{json.dumps(state_document)}\nEOF_STATE\n"
        "exit 0\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    runner = TerraformRunner(str(fake), ExecutionRecorder(session_factory))
    shown = runner.state_show(
        tmp_path,
        console_config.target("sandbox/000000000000/eu-west-2"),
        operator_settings,
        "aws_db_instance.demo_orders",
        operation_id=None,
        step_name="reconcile",
    )
    assert shown.result.succeeded
    assert arguments_file.read_text().split() == ["state", "show", "-json", "aws_db_instance.demo_orders"]
    assert shown.attributes["identifier"] == "demo-orders"
    assert shown.attributes["resource_id"] == "db-GREENRESOURCEID"
    with session_factory() as session:
        stored = [
            r.standard_output or "" for r in session.query(ExecutionRecord).filter_by(record_kind="terraform")
        ]
    assert any("demo-orders" in text for text in stored)
    assert all("hunter2" not in text for text in stored)


def test_state_show_of_an_address_state_does_not_hold_returns_no_attributes(
    operator_settings, console_config, session_factory, tmp_path: Path
) -> None:
    import stat

    fake = tmp_path / "fake-tofu"
    fake.write_text(
        "#!/usr/bin/env bash\necho 'Error: No instance found for the given address' >&2\nexit 1\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    runner = TerraformRunner(str(fake), ExecutionRecorder(session_factory))
    shown = runner.state_show(
        tmp_path,
        console_config.target("sandbox/000000000000/eu-west-2"),
        operator_settings,
        "aws_db_instance.demo_orders_old1",
        operation_id=None,
        step_name="reconcile",
    )
    assert not shown.result.succeeded and shown.attributes == {}
