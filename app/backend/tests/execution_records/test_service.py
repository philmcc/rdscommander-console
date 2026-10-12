import sys

from rdscommander.control_db.models import ExecutionRecord
from rdscommander.execution_records.service import ExecutionRecorder


def test_record_subprocess_stores_full_output_and_exit_code(session_factory) -> None:
    recorder = ExecutionRecorder(session_factory)
    result = recorder.record_subprocess(
        record_kind="git",
        command=[
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        working_directory=None,
        environment=None,
        operation_id=None,
        step_name="probe",
        target_key=None,
    )
    assert result.exit_code == 3 and not result.succeeded
    with session_factory() as session:
        row = session.get(ExecutionRecord, result.record_id)
        assert row.standard_output == "out\n" and row.standard_error == "err\n"
        assert row.record_kind == "git" and row.command_line.endswith("sys.exit(3)'")
        assert row.duration_milliseconds >= 0


def test_record_aws_call_redacts_request_secrets(session_factory) -> None:
    recorder = ExecutionRecorder(session_factory)
    record_id = recorder.record_aws_call(
        operation_id=None,
        step_name=None,
        target_key="sandbox/000000000000/eu-west-2",
        api_name="rds:CreateDBInstance",
        request_parameters={"MasterUserPassword": "pw", "DBInstanceIdentifier": "x"},
        response_payload={"ok": True},
        succeeded=True,
        duration_milliseconds=12,
    )
    with session_factory() as session:
        row = session.get(ExecutionRecord, record_id)
        assert row.request_parameters == {"MasterUserPassword": "[REDACTED]", "DBInstanceIdentifier": "x"}


def test_record_subprocess_accepts_declared_success_exit_codes(session_factory) -> None:
    recorder = ExecutionRecorder(session_factory)
    result = recorder.record_subprocess(
        record_kind="terraform",
        command=[sys.executable, "-c", "import sys; sys.exit(2)"],
        working_directory=None,
        environment=None,
        operation_id=None,
        step_name="plan",
        target_key=None,
        success_exit_codes=(0, 2),
    )
    assert result.exit_code == 2 and result.succeeded
    with session_factory() as session:
        assert session.get(ExecutionRecord, result.record_id).succeeded is True
