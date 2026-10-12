"""Build boto3 clients per target: profile sessions for real accounts, dummy credentials when simulated."""

from __future__ import annotations

import uuid
from typing import Any

import boto3
from botocore import xform_name
from botocore.client import BaseClient
from botocore.config import Config

from rdscommander.aws.call_recorder import attach_call_recorder
from rdscommander.aws.rds_reader import UNSUPPORTED_MARKER
from rdscommander.config.models import ResolvedTarget
from rdscommander.config.settings import OperatorSettings
from rdscommander.execution_records.service import ExecutionRecorder

SIMULATED_ACCESS_KEY_ID = "testing"
SIMULATED_SECRET_ACCESS_KEY = "testing"  # noqa: S105 - not a real secret
SIMULATOR_INTERNAL_ERROR_TITLE = b"<title>500 Internal Server Error</title>"


def name_the_action_the_simulator_has_not_implemented(
    response_dict: dict[str, Any], operation_model: Any, **_: Any
) -> None:
    """Give the simulator server's bare internal-error page the wording the in-process simulator uses.

    The simulator raises `NotImplementedError("The <action> action has not been implemented")` for an
    action it lacks. In process that error reaches the caller as it is; the simulator container answers a
    generic HTML page with status 500 instead, which says nothing about the action. This `before-parse`
    handler replaces that page with the in-process wording, so every caller recognises the limitation the
    same way (`UNSUPPORTED_MARKER`). botocore still cannot parse the body and raises at once, without
    retrying. Only simulated clients get this handler; a real account's error page stays an error.

    Example:
        >>> handler = name_the_action_the_simulator_has_not_implemented
        >>> client.meta.events.register("before-parse", handler)  # doctest: +SKIP
    """
    body = response_dict.get("body") or b""
    if response_dict.get("status_code") != 500 or SIMULATOR_INTERNAL_ERROR_TITLE not in body:
        return
    action = xform_name(operation_model.name)
    response_dict["body"] = (
        f"The {action} action {UNSUPPORTED_MARKER} (the simulated account answered HTTP 500)".encode()
    )


class AwsSessionFactory:
    """Creates recorded clients."""

    def __init__(self, settings: OperatorSettings, recorder: ExecutionRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.retry_config = Config(retries={"max_attempts": 10, "mode": "adaptive"})

    def client(
        self,
        target: ResolvedTarget,
        service_name: str,
        *,
        operation_id: uuid.UUID | None = None,
        step_name: str | None = None,
    ) -> BaseClient:
        """A client for `service_name` in `target`, with every call recorded."""
        if target.simulated:
            # A None endpoint means the SDK defaults, which is what an in-process mock intercepts.
            endpoint: dict[str, str] = {}
            if self.settings.simulated_aws_endpoint_url:
                endpoint["endpoint_url"] = self.settings.simulated_aws_endpoint_url
            client = boto3.client(
                service_name,
                region_name=target.aws_region,
                aws_access_key_id=SIMULATED_ACCESS_KEY_ID,
                aws_secret_access_key=SIMULATED_SECRET_ACCESS_KEY,
                config=self.retry_config,
                **endpoint,
            )
            client.meta.events.register("before-parse", name_the_action_the_simulator_has_not_implemented)
        else:
            session = boto3.Session(profile_name=target.aws_profile_name, region_name=target.aws_region)
            client = session.client(service_name, config=self.retry_config)
        attach_call_recorder(
            client,
            self.recorder,
            target_key=target.target_key,
            operation_id=operation_id,
            step_name=step_name,
        )
        return client
