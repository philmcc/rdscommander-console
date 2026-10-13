import pytest
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    SSOTokenLoadError,
    UnauthorizedSSOTokenError,
)

from rdscommander.aws.errors import diagnose_exception, iam_action_for


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "GetCallerIdentity")


def test_an_expired_token_names_the_sso_login_command() -> None:
    kind, _, fix_suggestion = diagnose_exception(_client_error("ExpiredToken"), "example-prod", "")
    assert kind == "aws_credentials_expired"
    assert "aws sso login --profile example-prod" in fix_suggestion


@pytest.mark.parametrize(
    "error",
    [
        SSOTokenLoadError(error_msg="Error loading SSO Token: Token for https://x does not exist"),
        UnauthorizedSSOTokenError(),
        NoCredentialsError(),
    ],
)
def test_a_profile_that_cannot_produce_credentials_is_reported_as_expired_credentials(
    error: Exception,
) -> None:
    kind, detail, fix_suggestion = diagnose_exception(error, "example-prod", "traceback text")
    assert kind == "aws_credentials_expired"
    assert detail == str(error) and "traceback text" not in detail
    assert "aws sso login --profile example-prod" in fix_suggestion


def test_a_missing_profile_is_reported_with_its_name() -> None:
    kind, detail, fix_suggestion = diagnose_exception(ProfileNotFound(profile="ghost"), "ghost", "")
    assert kind == "aws_profile_not_found"
    assert "ghost" in detail and "ghost" in fix_suggestion


def test_an_unexpected_exception_keeps_its_traceback() -> None:
    kind, detail, _ = diagnose_exception(ValueError("boom"), None, "Traceback: here")
    assert kind == "unhandled_exception" and "boom" in detail and "Traceback: here" in detail


@pytest.mark.parametrize(
    ("api_name", "expected_iam_action"),
    [
        ("rds:DescribeDBInstances", "rds:DescribeDBInstances"),
        ("rds:DescribeEngineDefaultClusterParameters", "rds:DescribeEngineDefaultClusterParameters"),
        ("rds:RebootDBInstance", "rds:RebootDBInstance"),
        ("RebootDBInstance", "rds:RebootDBInstance"),
        ("ResetDBClusterParameterGroup", "rds:ResetDBClusterParameterGroup"),
        ("DescribePendingMaintenanceActions", "rds:DescribePendingMaintenanceActions"),
        ("RestoreDBInstanceFromDBSnapshot", "rds:RestoreDBInstanceFromDBSnapshot"),
        ("FailoverDBCluster", "rds:FailoverDBCluster"),
        ("PromoteReadReplica", "rds:PromoteReadReplica"),
        ("ApplyPendingMaintenanceAction", "rds:ApplyPendingMaintenanceAction"),
        ("DescribeOrderableDBInstanceOptions", "rds:DescribeOrderableDBInstanceOptions"),
        ("CreateBlueGreenDeployment", "rds:CreateBlueGreenDeployment"),
        ("SwitchoverBlueGreenDeployment", "rds:SwitchoverBlueGreenDeployment"),
        ("DeleteBlueGreenDeployment", "rds:DeleteBlueGreenDeployment"),
        ("DescribeBlueGreenDeployments", "rds:DescribeBlueGreenDeployments"),
        ("account:ListRegions", "account:ListRegions"),
        ("ListRegions", "account:ListRegions"),
        ("s3:HeadBucket", "s3:ListBucket"),
        ("s3:PutBucketEncryption", "s3:PutEncryptionConfiguration"),
        ("PutPublicAccessBlock", "s3:PutBucketPublicAccessBlock"),
        ("sts:GetCallerIdentity", "sts:GetCallerIdentity"),
    ],
)
def test_iam_action_for_names_the_action_iam_checks_for_an_api_call(
    api_name: str, expected_iam_action: str
) -> None:
    assert iam_action_for(api_name) == expected_iam_action
