"""Ride out passing AWS failures while a blue/green step polls (throttling, an expired login, the network).

A poll of a blue/green deployment may run for hours; one throttled describe, an SSO login that expires and is
renewed, or a dropped connection must not end the upgrade and leave a deployment behind for nothing. These
errors are logged as warnings and the call is asked again after an exponential backoff (doubling from the poll
interval, at most `MAXIMUM_TRANSIENT_BACKOFF_SECONDS`) until the poll's own time limit; only then is the error
raised. Any other error (access denied, a deployment AWS does not know) is raised at once.

Example:
    >>> transient_backoff_seconds(0, 15.0), transient_backoff_seconds(3, 15.0)
    (15.0, 60.0)
"""

from __future__ import annotations

from collections.abc import Callable

from botocore.exceptions import BotoCoreError, ClientError

from rdscommander.aws.errors import EXPIRED_CREDENTIAL_CODES, THROTTLING_CODES
from rdscommander.logging_setup import get_logger
from rdscommander.operations.executor import StepContext

logger = get_logger(__name__)

MAXIMUM_TRANSIENT_BACKOFF_SECONDS = 60.0
"""The longest wait between two attempts of a call AWS failed for a passing reason."""
MINIMUM_TRANSIENT_BACKOFF_SECONDS = 1.0
"""The first wait when the poll interval is shorter (a test's zero, say)."""
TRANSIENT_AWS_ERROR_CODES = frozenset(THROTTLING_CODES | EXPIRED_CREDENTIAL_CODES | {"UnauthorizedSSOToken"})
"""AWS error codes that pass: throttling, and credentials that expired and can be renewed meanwhile."""


def is_transient_aws_error(error: Exception) -> bool:
    """Whether asking again later can succeed: throttling, expired credentials, or no answer from AWS.

    Every `BotoCoreError` counts: it is raised before AWS answered (credentials could not be loaded or a
    login expired, the connection failed or timed out).

    Example:
        >>> is_transient_aws_error(ClientError({"Error": {"Code": "Throttling"}}, "Describe"))
        True
        >>> is_transient_aws_error(ClientError({"Error": {"Code": "AccessDenied"}}, "Describe"))
        False
    """
    if isinstance(error, BotoCoreError):
        return True
    if isinstance(error, ClientError):
        return error.response.get("Error", {}).get("Code") in TRANSIENT_AWS_ERROR_CODES
    return False


def transient_backoff_seconds(failures_so_far: int, poll_seconds: float) -> float:
    """The wait before the next attempt: the poll interval doubled per failure so far, at most 60 s.

    Example:
        >>> [transient_backoff_seconds(n, 10.0) for n in range(4)]
        [10.0, 20.0, 40.0, 60.0]
    """
    first_wait_seconds = max(float(poll_seconds), MINIMUM_TRANSIENT_BACKOFF_SECONDS)
    return min(first_wait_seconds * (2**failures_so_far), MAXIMUM_TRANSIENT_BACKOFF_SECONDS)


def call_riding_out_transient_errors[T](
    context: StepContext,
    call: Callable[[], T],
    *,
    step_name: str,
    waited_seconds: float,
    limit_seconds: float,
    poll_seconds: float,
) -> tuple[T, float]:
    """Run `call`; on a transient AWS error wait with backoff and ask again, until `limit_seconds` of waiting.

    Args:
        context: The running step's context (its `services.sleep` waits).
        call: The AWS read to make, for example a describe of the deployment.
        step_name: The step polling, for the log.
        waited_seconds: How long the poll has waited so far; counts against the limit.
        limit_seconds: The poll's time limit.
        poll_seconds: The poll interval, the first backoff.

    Returns:
        `call`'s result and the waited seconds, including the backoff spent here.

    Raises:
        ClientError, BotoCoreError: Not transient, or still failing once the limit is reached.

    Example:
        >>> status, waited = call_riding_out_transient_errors(  # doctest: +SKIP
        ...     context, lambda: describe_deployment(context, deployment_id, "switchover"),
        ...     step_name="switchover", waited_seconds=0.0, limit_seconds=900, poll_seconds=15,
        ... )
    """
    failures_so_far = 0
    while True:
        try:
            return call(), waited_seconds
        except (ClientError, BotoCoreError) as error:
            if not is_transient_aws_error(error) or waited_seconds >= limit_seconds:
                raise
            backoff_seconds = transient_backoff_seconds(failures_so_far, poll_seconds)
            logger.warning(
                "blue_green_poll_transient_aws_error",
                operation_id=str(context.operation.id),
                step_name=step_name,
                error_type=type(error).__name__,
                aws_error_code=error.response.get("Error", {}).get("Code")
                if isinstance(error, ClientError)
                else None,
                backoff_seconds=backoff_seconds,
                waited_seconds=waited_seconds,
                limit_seconds=limit_seconds,
                fix_suggestion="nothing to do if it passes; if it lasts, renew the AWS login of the "
                "environment's profile or wait for the throttling to ease",
            )
            context.services.sleep(backoff_seconds)
            waited_seconds += backoff_seconds
            failures_so_far += 1
