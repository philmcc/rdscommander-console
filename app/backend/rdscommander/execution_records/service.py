"""Run subprocesses and record AWS calls with complete input and output."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdscommander.control_db.models import ExecutionRecord
from rdscommander.control_db.session import ControlDbSessionFactory
from rdscommander.execution_records.redaction import redact_mapping
from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)
MAX_STORED_PAYLOAD_CHARACTERS = 262_144


@dataclass(frozen=True)
class SubprocessResult:
    """What a recorded subprocess returned."""

    record_id: int
    exit_code: int
    standard_output: str
    standard_error: str
    success_exit_codes: tuple[int, ...] = (0,)

    @property
    def succeeded(self) -> bool:
        """Exit code is one of the accepted success codes (zero by default)."""
        return self.exit_code in self.success_exit_codes


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


class ExecutionRecorder:
    """Writes `execution_records` rows."""

    def __init__(self, session_factory: ControlDbSessionFactory) -> None:
        self.session_factory = session_factory

    def record_subprocess(
        self,
        *,
        record_kind: str,
        command: list[str],
        working_directory: Path | None,
        environment: dict[str, str] | None,
        operation_id: uuid.UUID | None,
        step_name: str | None,
        target_key: str | None,
        timeout_seconds: int = 1800,
        success_exit_codes: tuple[int, ...] = (0,),
    ) -> SubprocessResult:
        """Run `command`, capture everything, store it, and return the result.

        Never raises on a non-zero exit; a missing binary or a timeout is recorded too.
        `success_exit_codes` marks which exit codes count as success (a Terraform plan with
        `-detailed-exitcode` returns 2 when changes are present).
        """
        started = time.monotonic()
        logger.info(
            "subprocess_started",
            record_kind=record_kind,
            command=command[0],
            operation_id=str(operation_id) if operation_id else None,
            step_name=step_name,
        )
        error_message: str | None = None
        try:
            completed = subprocess.run(
                command,
                cwd=working_directory,
                env=environment,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as timeout:
            exit_code, stdout, stderr = 124, _as_text(timeout.stdout), _as_text(timeout.stderr)
            error_message = f"timed out after {timeout_seconds}s"
        except FileNotFoundError as missing:
            exit_code, stdout, stderr = 127, "", str(missing)
            error_message = f"binary not found: {command[0]}"
        duration = int((time.monotonic() - started) * 1000)
        with self.session_factory() as session:
            row = ExecutionRecord(
                operation_id=operation_id,
                step_name=step_name,
                record_kind=record_kind,
                target_key=target_key,
                command_line=shlex.join(command),
                working_directory=str(working_directory) if working_directory else None,
                standard_output=stdout,
                standard_error=stderr,
                exit_code=exit_code,
                succeeded=exit_code in success_exit_codes,
                error_message=error_message,
                duration_milliseconds=duration,
            )
            session.add(row)
            session.commit()
            record_id = row.id
        if exit_code not in success_exit_codes:
            logger.error(
                "subprocess_failed",
                record_kind=record_kind,
                exit_code=exit_code,
                execution_record_id=record_id,
                fix_suggestion="open the execution record to read the full stderr",
            )
        return SubprocessResult(
            record_id=record_id,
            exit_code=exit_code,
            standard_output=stdout,
            standard_error=stderr,
            success_exit_codes=success_exit_codes,
        )

    def replace_standard_output(self, record_id: int, standard_output: str) -> None:
        """Overwrite a stored record's stdout, used to keep only the redacted form of a Terraform plan."""
        with self.session_factory() as session:
            row = session.get(ExecutionRecord, record_id)
            if row is not None:
                row.standard_output = standard_output
                session.commit()

    def record_aws_call(
        self,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
        target_key: str | None,
        api_name: str,
        request_parameters: dict[str, Any] | None,
        response_payload: dict[str, Any] | None,
        succeeded: bool,
        duration_milliseconds: int,
        error_message: str | None = None,
    ) -> int:
        """Store one AWS API call; secrets are redacted and huge payloads truncated."""
        with self.session_factory() as session:
            row = ExecutionRecord(
                operation_id=operation_id,
                step_name=step_name,
                record_kind="aws_api",
                target_key=target_key,
                api_name=api_name,
                request_parameters=_bounded(redact_mapping(request_parameters)),
                response_payload=_bounded(redact_mapping(response_payload)),
                succeeded=succeeded,
                error_message=error_message,
                duration_milliseconds=duration_milliseconds,
            )
            session.add(row)
            session.commit()
            return row.id


def _bounded(payload: Any) -> Any:
    if payload is None:
        return None
    encoded = json.dumps(payload, default=str)
    if len(encoded) <= MAX_STORED_PAYLOAD_CHARACTERS:
        return json.loads(encoded)
    return {
        "truncated": True,
        "original_characters": len(encoded),
        "head": encoded[:MAX_STORED_PAYLOAD_CHARACTERS],
    }
