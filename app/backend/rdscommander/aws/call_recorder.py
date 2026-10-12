"""Record every boto3 call through botocore events."""

from __future__ import annotations

import time
import uuid
from typing import Any

from botocore.client import BaseClient

from rdscommander.execution_records.service import ExecutionRecorder

READ_ONLY_PREFIXES = ("Describe", "List", "Get", "Head")


def attach_call_recorder(
    client: BaseClient,
    recorder: ExecutionRecorder,
    *,
    target_key: str | None,
    operation_id: uuid.UUID | None,
    step_name: str | None,
) -> None:
    """Hook botocore events so each API call becomes an execution record.

    `provide-client-params` sees the caller's parameters, `before-call` marks the
    start time, and `after-call` writes the record. Errors surface as parsed
    responses carrying an `Error` key, so they are recorded too.
    """
    service_name = client.meta.service_model.service_name

    def remember_parameters(params: dict[str, Any], model: Any, context: dict[str, Any], **_: Any) -> None:
        context["rdscommander_request_parameters"] = dict(params)

    def mark_start(params: dict[str, Any], model: Any, context: dict[str, Any], **_: Any) -> None:
        context["rdscommander_started"] = time.monotonic()

    def write_record(parsed: dict[str, Any], model: Any, context: dict[str, Any], **_: Any) -> None:
        started = context.get("rdscommander_started", time.monotonic())
        request_parameters = context.get("rdscommander_request_parameters", {})
        metadata = parsed.get("ResponseMetadata", {})
        is_read = model.name.startswith(READ_ONLY_PREFIXES)
        payload = (
            {"HTTPStatusCode": metadata.get("HTTPStatusCode"), "RequestId": metadata.get("RequestId")}
            if is_read
            else {k: v for k, v in parsed.items() if k != "ResponseMetadata"}
        )
        error = parsed.get("Error")
        recorder.record_aws_call(
            operation_id=operation_id,
            step_name=step_name,
            target_key=target_key,
            api_name=f"{service_name}:{model.name}",
            request_parameters=request_parameters,
            response_payload=payload,
            succeeded=error is None,
            duration_milliseconds=int((time.monotonic() - started) * 1000),
            error_message=f"{error.get('Code')}: {error.get('Message')}" if error else None,
        )

    def write_error_record(
        exception: Exception, context: dict[str, Any], event_name: str = "", **_: Any
    ) -> None:
        started = context.get("rdscommander_started", time.monotonic())
        operation_name = event_name.rsplit(".", 1)[-1] if event_name else "unknown"
        recorder.record_aws_call(
            operation_id=operation_id,
            step_name=step_name,
            target_key=target_key,
            api_name=f"{service_name}:{operation_name}",
            request_parameters=context.get("rdscommander_request_parameters", {}),
            response_payload=None,
            succeeded=False,
            duration_milliseconds=int((time.monotonic() - started) * 1000),
            error_message=f"{type(exception).__name__}: {exception}",
        )

    # Prefix registrations fire for every service and operation of this client.
    client.meta.events.register(
        "provide-client-params", remember_parameters, unique_id=f"rdsc-params-{id(client)}"
    )
    client.meta.events.register("before-call", mark_start, unique_id=f"rdsc-before-{id(client)}")
    client.meta.events.register("after-call", write_record, unique_id=f"rdsc-after-{id(client)}")
    client.meta.events.register(
        "after-call-error", write_error_record, unique_id=f"rdsc-after-error-{id(client)}"
    )
