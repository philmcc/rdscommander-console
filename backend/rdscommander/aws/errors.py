"""Turn AWS client errors into diagnostics that say what to do."""

from __future__ import annotations

from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    SSOError,
    TokenRetrievalError,
)

IAM_POLICY_DOCUMENT = "app/docs/iam/rdscommander-policy.json"
EXPIRED_CREDENTIAL_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "RequestExpired",
}
CREDENTIAL_PROBLEM_ERRORS = (
    NoCredentialsError,
    PartialCredentialsError,
    CredentialRetrievalError,
    SSOError,
    TokenRetrievalError,
)
"""Failures that happen before any request is sent because the profile cannot produce credentials: an
expired or missing SSO login, a profile without keys, a credential process that failed."""
ACCESS_DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthorizationError"}
THROTTLING_CODES = {"Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequestsException"}
OPERATION_PREFIX_TO_SERVICE = (
    (
        (
            "DescribeDB",
            "ModifyDB",
            "CreateDB",
            "DeleteDB",
            "ResetDB",
            "RebootDB",
            "DescribeEngineDefault",
            "DescribePendingMaintenanceActions",
            "ListTagsForResource",
            "AddTagsToResource",
            "RemoveTagsFromResource",
            "RestoreDB",
            "FailoverDB",
            "StartDB",
            "StopDB",
            "CopyDB",
            "PromoteReadReplica",
            "ApplyPendingMaintenanceAction",
            "DescribeOrderable",
            "CreateBlueGreen",
            "DescribeBlueGreen",
            "SwitchoverBlueGreen",
            "DeleteBlueGreen",
        ),
        "rds",
    ),
    (
        (
            "HeadBucket",
            "CreateBucket",
            "PutBucket",
            "GetBucket",
            "PutPublicAccessBlock",
            "GetPublicAccessBlock",
            "PutObject",
            "GetObject",
            "DeleteObject",
            "ListBucket",
            "ListObjects",
        ),
        "s3",
    ),
    (("DescribeVpcs", "DescribeSubnets", "DescribeSecurityGroups", "CreateVpc", "CreateSubnet"), "ec2"),
    (("GetCallerIdentity",), "sts"),
    (("ListRegions",), "account"),
)
API_CALL_TO_DIFFERENTLY_NAMED_IAM_ACTION = {
    "s3:HeadBucket": "s3:ListBucket",
    "s3:ListObjects": "s3:ListBucket",
    "s3:ListObjectsV2": "s3:ListBucket",
    "s3:PutBucketEncryption": "s3:PutEncryptionConfiguration",
    "s3:GetBucketEncryption": "s3:GetEncryptionConfiguration",
    "s3:PutPublicAccessBlock": "s3:PutBucketPublicAccessBlock",
    "s3:GetPublicAccessBlock": "s3:GetBucketPublicAccessBlock",
}
"""API calls whose IAM permission has another name than the call itself."""


def iam_action_for(api_name: str) -> str:
    """The IAM action that allows an AWS API call.

    Args:
        api_name: `service:Operation` as execution records store it, or a bare operation name (as
            `ClientError.operation_name` carries it), whose service is then inferred from its prefix.

    Returns:
        `service:Action` as an IAM policy grants it; `aws:<name>` when the service cannot be inferred.

    Example:
        >>> iam_action_for("s3:HeadBucket")
        's3:ListBucket'
        >>> iam_action_for("RebootDBInstance")
        'rds:RebootDBInstance'
    """
    if ":" in api_name:
        qualified_name = api_name
    else:
        service = next(
            (service for prefixes, service in OPERATION_PREFIX_TO_SERVICE if api_name.startswith(prefixes)),
            "aws",
        )
        qualified_name = f"{service}:{api_name}"
    return API_CALL_TO_DIFFERENTLY_NAMED_IAM_ACTION.get(qualified_name, qualified_name)


def credentials_expired_fix_suggestion(profile_name: str | None) -> str:
    """What to do when the credentials of a profile cannot be used.

    Args:
        profile_name: The AWS profile, or None when the caller does not know it.

    Returns:
        A sentence naming `aws sso login --profile <name>`.

    Example:
        >>> "aws sso login --profile example-prod" in credentials_expired_fix_suggestion("example-prod")
        True
    """
    profile = profile_name or "<the profile for this target in config/environments.yaml>"
    return (
        "the AWS credentials for this target are expired or invalid; run "
        f"`aws sso login --profile {profile}` (or refresh the profile's keys), "
        "then create a new operation"
    )


def diagnose_client_error(error: ClientError, profile_name: str | None) -> tuple[str, str, str]:
    """Return `(diagnostic kind, detail, fix suggestion)` for a boto3 `ClientError`."""
    payload = error.response.get("Error", {})
    code = str(payload.get("Code", "Unknown"))
    message = str(payload.get("Message", ""))
    operation_name = getattr(error, "operation_name", "unknown operation")
    action = iam_action_for(operation_name)
    profile = profile_name or "<the profile for this target in config/environments.yaml>"
    detail = f"{action} failed with {code}: {message}"
    if code in EXPIRED_CREDENTIAL_CODES:
        return (
            "aws_credentials_expired",
            detail,
            credentials_expired_fix_suggestion(profile_name),
        )
    if code in ACCESS_DENIED_CODES:
        return (
            "aws_access_denied",
            detail,
            f"the identity behind profile {profile} is not allowed to call {action}; grant it using "
            f"{IAM_POLICY_DOCUMENT}, then create a new operation",
        )
    if code in THROTTLING_CODES:
        return (
            "aws_throttled",
            detail,
            "AWS throttled the request even after retries; wait a minute and create a new operation",
        )
    return (
        "aws_api_error",
        detail,
        f"AWS rejected {action}; read the message above, fix the cause, and create a new operation",
    )


def diagnose_exception(
    error: Exception, profile_name: str | None, formatted_traceback: str
) -> tuple[str, str, str]:
    """`(kind, detail, fix suggestion)` for any step exception, with AWS errors explained specifically.

    Besides API errors (`ClientError`), the failures of the SDK's credential handling are explained: a
    profile that is not in the AWS config, and a profile whose credentials cannot be produced, which is what
    an expired SSO login looks like before any request is sent.
    """
    if isinstance(error, ClientError):
        return diagnose_client_error(error, profile_name)
    if isinstance(error, ProfileNotFound):
        return (
            "aws_profile_not_found",
            str(error),
            f"the AWS profile {profile_name or '<the profile>'} is not in the AWS config on this machine; "
            "choose one of the profiles listed in `aws configure list-profiles`, or add it to ~/.aws/config, "
            "then create a new operation",
        )
    if isinstance(error, CREDENTIAL_PROBLEM_ERRORS):
        return ("aws_credentials_expired", str(error), credentials_expired_fix_suggestion(profile_name))
    return (
        "unhandled_exception",
        f"{error}\n{formatted_traceback}",
        "inspect the execution records for this step, fix the cause, and create a new operation",
    )
