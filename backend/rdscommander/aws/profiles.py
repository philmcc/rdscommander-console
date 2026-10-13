"""Clients for a profile that is not in the configuration yet (environment import).

An imported environment has no target in `config/environments.yaml` until the import has finished, so
`AwsSessionFactory` cannot serve it. These helpers build the same kind of recorded client from a profile
name (or from the simulated account) instead. Credential values are never read, logged or stored here: the
SDK resolves them from the profile on its own.
"""

from __future__ import annotations

import uuid
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from rdscommander.aws.call_recorder import attach_call_recorder
from rdscommander.aws.sessions import SIMULATED_ACCESS_KEY_ID, SIMULATED_SECRET_ACCESS_KEY
from rdscommander.config.settings import OperatorSettings
from rdscommander.execution_records.service import ExecutionRecorder
from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)

IMPORT_TARGET_KEY_PREFIX = "import"
EXECUTION_RECORD_TARGET_KEY_LENGTH = 80
"""Width of the execution record's target column; a long profile name must not overflow it."""


def client_for_profile(
    profile_name: str | None,
    region: str,
    service_name: str,
    settings: OperatorSettings,
    recorder: ExecutionRecorder,
    *,
    simulated: bool,
    operation_id: uuid.UUID | None,
    step_name: str | None,
) -> BaseClient:
    """A recorded client for `profile_name` in `region`; the simulated account uses the fixed keys.

    Args:
        profile_name: Profile from the AWS CLI config; ignored when `simulated`.
        region: AWS region of the client.
        service_name: boto3 service name, e.g. `sts`, `rds` or `account`.
        settings: Operator settings; `simulated_aws_endpoint_url` points a simulated client at the simulator.
        recorder: Records every call made through the client.
        simulated: Use the simulated account instead of a profile.
        operation_id: Operation the calls belong to.
        step_name: Step the calls belong to.

    Returns:
        A boto3 client whose calls are recorded under `import/<profile or simulated>/<region>`.

    Raises:
        botocore.exceptions.ProfileNotFound: `profile_name` is not in the AWS config on this machine.

    Example:
        >>> sts = client_for_profile(  # doctest: +SKIP
        ...     "example-prod", "eu-west-2", "sts", settings, recorder,
        ...     simulated=False, operation_id=None, step_name="resolve_account",
        ... )
    """
    config = Config(retries={"max_attempts": 10, "mode": "adaptive"})
    if simulated:
        endpoint: dict[str, Any] = (
            {"endpoint_url": settings.simulated_aws_endpoint_url}
            if settings.simulated_aws_endpoint_url
            else {}
        )
        client = boto3.client(
            service_name,
            region_name=region,
            aws_access_key_id=SIMULATED_ACCESS_KEY_ID,
            aws_secret_access_key=SIMULATED_SECRET_ACCESS_KEY,
            config=config,
            **endpoint,
        )
    else:
        client = boto3.Session(profile_name=profile_name, region_name=region).client(
            service_name, config=config
        )
    attach_call_recorder(
        client,
        recorder,
        target_key=f"{IMPORT_TARGET_KEY_PREFIX}/{profile_name or 'simulated'}/{region}"[
            :EXECUTION_RECORD_TARGET_KEY_LENGTH
        ],
        operation_id=operation_id,
        step_name=step_name,
    )
    return client


def caller_identity(sts_client: BaseClient) -> dict[str, Any]:
    """`GetCallerIdentity`, kept separate so that tests can make it fail like an expired SSO session.

    Args:
        sts_client: An STS client from `client_for_profile`.

    Returns:
        The `GetCallerIdentity` response; its `Account` is the 12-digit account id.

    Example:
        >>> caller_identity(sts_client)["Account"]  # doctest: +SKIP
        '123456789012'
    """
    return sts_client.get_caller_identity()


def enabled_regions(account_client: BaseClient) -> list[str]:
    """Regions enabled for the account (`account:ListRegions`); the partition's RDS regions when denied.

    Args:
        account_client: An `account` client from `client_for_profile`.

    Returns:
        Sorted region names.

    Example:
        >>> enabled_regions(account_client)[:2]  # doctest: +SKIP
        ['eu-west-1', 'eu-west-2']
    """
    try:
        regions: list[str] = []
        paginator = account_client.get_paginator("list_regions")
        for page in paginator.paginate(RegionOptStatusContains=["ENABLED", "ENABLED_BY_DEFAULT"]):
            regions.extend(region["RegionName"] for region in page["Regions"])
        return sorted(regions)
    except Exception as error:  # noqa: BLE001 - permission or API shape differences fall back
        fallback_regions = sorted(boto3.Session().get_available_regions("rds"))
        logger.warning(
            "enabled_regions_lookup_failed",
            error=str(error),
            fallback_region_count=len(fallback_regions),
            fix_suggestion="grant account:ListRegions to scan only the regions enabled for the account, "
            "or list the regions to import explicitly",
        )
        return fallback_regions
