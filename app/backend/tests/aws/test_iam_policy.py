"""The shipped IAM policy grants every AWS call the console makes."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

from rdscommander.aws.errors import iam_action_for
from tests.recorded_aws_calls import PRODUCT_AWS_API_NAMES

IAM_POLICY_PATH = Path(__file__).resolve().parents[3] / "docs" / "iam" / "rdscommander-policy.json"
READ_ONLY_ACTION_PREFIXES = ("Describe", "List", "Get")


def _policy_statements() -> list[dict]:
    return json.loads(IAM_POLICY_PATH.read_text())["Statement"]


def _actions_of(statement: dict) -> list[str]:
    actions = statement["Action"]
    return [actions] if isinstance(actions, str) else list(actions)


def _granted_action_patterns() -> list[str]:
    return [
        action
        for statement in _policy_statements()
        if statement["Effect"] == "Allow"
        for action in _actions_of(statement)
    ]


def _is_granted(iam_action: str, granted_patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(iam_action.lower(), pattern.lower()) for pattern in granted_patterns)


@pytest.mark.runs_after_every_other_test
def test_iam_policy_grants_every_aws_call_the_console_made_during_the_suite() -> None:
    if not PRODUCT_AWS_API_NAMES:
        pytest.skip("no AWS call was recorded; run this test together with the rest of the suite")
    granted_patterns = _granted_action_patterns()
    missing_iam_actions = sorted(
        {
            iam_action_for(api_name)
            for api_name in PRODUCT_AWS_API_NAMES
            if not _is_granted(iam_action_for(api_name), granted_patterns)
        }
    )
    assert missing_iam_actions == [], (
        f"{IAM_POLICY_PATH} does not grant {missing_iam_actions}; recorded calls: "
        f"{sorted(PRODUCT_AWS_API_NAMES)}"
    )


def test_iam_policy_keeps_reads_and_changes_in_separate_statements() -> None:
    statements = {statement.get("Sid"): statement for statement in _policy_statements()}
    read_actions = _actions_of(statements["ConsoleReads"])
    change_actions = _actions_of(statements["ConsoleChanges"])
    assert read_actions and all(
        action.split(":", 1)[1].startswith(READ_ONLY_ACTION_PREFIXES) for action in read_actions
    )
    assert change_actions and not any(
        action.split(":", 1)[1].startswith(READ_ONLY_ACTION_PREFIXES) for action in change_actions
    )
    assert "rds:RebootDBInstance" in change_actions
    assert "rds:DescribeEngineDefaultParameters" in read_actions


def test_iam_policy_grants_what_rds_needs_to_keep_a_master_password_in_secrets_manager() -> None:
    """RDS acts on the caller's behalf for `ManageMasterUserPassword`; the suite records none of these calls.

    It describes the `aws/secretsmanager` KMS key (`kms:DescribeKey`) and creates and tags the secret.
    """
    statements = {statement.get("Sid"): statement for statement in _policy_statements()}
    key_statement = statements["ManagedMasterPasswordKey"]
    assert _actions_of(key_statement) == ["kms:DescribeKey"] and key_statement["Resource"] == "*"
    granted_patterns = _granted_action_patterns()
    for action in ("kms:DescribeKey", "secretsmanager:CreateSecret", "secretsmanager:TagResource"):
        assert _is_granted(action, granted_patterns), action
    for action in ("secretsmanager:DescribeSecret", "secretsmanager:RotateSecret"):
        assert not _is_granted(action, granted_patterns), f"{action} is not needed to create the secret"


def test_iam_policy_lets_rds_pass_the_monitoring_role_and_use_customer_managed_keys() -> None:
    """Calls RDS makes on the console's behalf, which the simulated account never checks.

    Sending `MonitoringRoleArn` (enhanced monitoring on a change, create, replica or restore) needs
    `iam:PassRole` on the monitoring role, limited to the RDS monitoring service; restoring or
    replicating from a customer-managed-key-encrypted source needs `kms:CreateGrant` through RDS
    for an AWS resource; the provider reads `aws_rds_cluster` global membership and the
    decommission dependants check reads `GlobalClusterIdentifier`.
    """
    statements = {statement.get("Sid"): statement for statement in _policy_statements()}
    pass_role_statement = statements["EnhancedMonitoringRole"]
    assert _actions_of(pass_role_statement) == ["iam:PassRole"]
    assert pass_role_statement["Resource"] == "arn:aws:iam::*:role/*"
    assert pass_role_statement["Condition"] == {
        "StringEquals": {"iam:PassedToService": "monitoring.rds.amazonaws.com"}
    }
    grant_statement = statements["CustomerManagedKeyGrants"]
    assert _actions_of(grant_statement) == ["kms:CreateGrant"]
    assert grant_statement["Condition"] == {
        "StringLike": {"kms:ViaService": "rds.*.amazonaws.com"},
        "Bool": {"kms:GrantIsForAWSResource": "true"},
    }
    assert "rds:DescribeGlobalClusters" in _actions_of(statements["ConsoleReads"])


def test_iam_policy_grants_what_rds_uses_on_the_callers_behalf_for_a_blue_green_deployment() -> None:
    """Creating and switching over a deployment makes RDS act as the caller; the suite never records these.

    The console calls `CreateBlueGreenDeployment`, `DescribeBlueGreenDeployments`,
    `SwitchoverBlueGreenDeployment` and `DeleteBlueGreenDeployment`; to build the green side RDS uses the
    caller's permissions to create instances, replicas, clusters, cluster endpoints and parameter groups,
    to modify and promote them, to tag them and to describe instances. An Aurora switchover promotes the
    green cluster, so `PromoteReadReplicaDBCluster` is granted too.
    """
    granted_patterns = _granted_action_patterns()
    for action in (
        "rds:CreateBlueGreenDeployment",
        "rds:DescribeBlueGreenDeployments",
        "rds:SwitchoverBlueGreenDeployment",
        "rds:DeleteBlueGreenDeployment",
        "rds:CreateDBInstance",
        "rds:CreateDBInstanceReadReplica",
        "rds:ModifyDBInstance",
        "rds:CreateDBParameterGroup",
        "rds:AddTagsToResource",
        "rds:DescribeDBInstances",
        "rds:CreateDBCluster",
        "rds:CreateDBClusterEndpoint",
        "rds:PromoteReadReplica",
        "rds:PromoteReadReplicaDBCluster",
        "rds:DescribeDBEngineVersions",
        "rds:DescribeOrderableDBInstanceOptions",
        "rds:DescribePendingMaintenanceActions",
    ):
        assert _is_granted(action, granted_patterns), action
    statements = {statement.get("Sid"): statement for statement in _policy_statements()}
    assert _actions_of(statements["BlueGreenGreenSideEndpoints"]) == ["rds:CreateDBClusterEndpoint"]
