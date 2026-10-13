"""Run the Terraform-compatible binary with every invocation recorded."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from rdscommander.aws.sessions import SIMULATED_ACCESS_KEY_ID, SIMULATED_SECRET_ACCESS_KEY
from rdscommander.config.models import ResolvedTarget
from rdscommander.config.settings import OperatorSettings
from rdscommander.control_db.models import Operation
from rdscommander.execution_records.service import ExecutionRecorder, SubprocessResult
from rdscommander.logging_setup import get_logger
from rdscommander.terraform.plan_parser import redact_plan_json

logger = get_logger(__name__)

CREDENTIAL_VARIABLE_PREFIXES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
)
TERRAFORM_APPLY_INVOKED_PLAN_KEY = "terraform_apply_invoked"
"""Key on an operation's plan, true once the binary's `apply` was about to run: AWS may have changed since."""


def mark_terraform_apply_invoked(session: Session, operation: Operation) -> None:
    """Say on the operation, durably, that the binary's `apply` is about to run.

    Callers mark the operation right before `TerraformRunner.apply` and commit, so the mark holds even when
    the apply never returns (a lost process, a console that stopped half-way). Change history counts a
    failed operation with this mark as one that may have changed AWS. A refusal at the pre-apply plan
    comes before the mark, so the outside change that caused it stays an outside change.

    Args:
        session: Control database session the operation belongs to; it is committed.
        operation: The operation whose `apply` step is about to invoke the binary.

    Example:
        >>> mark_terraform_apply_invoked(session, operation)  # doctest: +SKIP
        >>> operation.plan[TERRAFORM_APPLY_INVOKED_PLAN_KEY]  # doctest: +SKIP
        True
    """
    operation.plan = {**operation.plan, TERRAFORM_APPLY_INVOKED_PLAN_KEY: True}
    session.commit()
    logger.info("terraform_apply_invocation_marked", operation_id=str(operation.id))


@dataclass(frozen=True)
class PlanRunResult:
    """Outcome of `plan` plus `show -json`."""

    result: SubprocessResult
    has_changes: bool
    plan_json: dict[str, Any]


STATE_SHOW_IDENTITY_ATTRIBUTES = ("identifier", "id", "resource_id", "arn")
"""What a `state show` keeps on its execution record: the attributes that say which AWS object it is."""


@dataclass(frozen=True)
class StateShowResult:
    """Outcome of `state show -json <address>`.

    Attributes:
        result: The recorded subprocess result.
        attributes: The attributes state holds at the address (`{}` when the binary failed or state does
            not hold it), e.g. `{"identifier": "demo-orders-old1", "resource_id": "db-ABC", ...}`.
    """

    result: SubprocessResult
    attributes: dict[str, Any]


def _state_show_attributes(standard_output: str, address: str) -> dict[str, Any]:
    """The attributes of `address` in `state show -json` output; `{}` when it is not there.

    OpenTofu writes JSON lines (UI messages, then the state document with `values.root_module`); each line
    is read and the document holding the address is used.
    """
    for line in standard_output.splitlines():
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(document, dict):
            continue
        module = (document.get("values") or {}).get("root_module") or {}
        for resource in module.get("resources") or []:
            if resource.get("address") == address:
                return dict(resource.get("values") or {})
    return {}


class TerraformRunner:
    """Thin, recorded wrapper over `tofu` or `terraform`."""

    def __init__(self, binary: str, recorder: ExecutionRecorder) -> None:
        self.binary = binary
        self.recorder = recorder

    def environment_for(self, target: ResolvedTarget, settings: OperatorSettings) -> dict[str, str]:
        """Process environment: profile for real targets, dummy credentials when simulated."""
        environment = {k: v for k, v in os.environ.items() if not k.startswith(CREDENTIAL_VARIABLE_PREFIXES)}
        environment.update(TF_IN_AUTOMATION="1", TF_INPUT="0", TF_CLI_ARGS="-no-color")
        if target.simulated:
            environment.update(
                AWS_ACCESS_KEY_ID=SIMULATED_ACCESS_KEY_ID,
                AWS_SECRET_ACCESS_KEY=SIMULATED_SECRET_ACCESS_KEY,
                AWS_REGION=target.aws_region,
                AWS_EC2_METADATA_DISABLED="true",
            )
        else:
            environment.update(AWS_PROFILE=target.aws_profile_name or "", AWS_REGION=target.aws_region)
        return environment

    def _run(
        self,
        arguments: list[str],
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        operation_id: uuid.UUID | None,
        step_name: str | None,
        timeout_seconds: int,
        success_exit_codes: tuple[int, ...] = (0,),
    ) -> SubprocessResult:
        return self.recorder.record_subprocess(
            record_kind="terraform",
            command=[self.binary, *arguments],
            working_directory=workspace_dir,
            environment=self.environment_for(target, settings),
            operation_id=operation_id,
            step_name=step_name,
            target_key=target.target_key,
            timeout_seconds=timeout_seconds,
            success_exit_codes=success_exit_codes,
        )

    def init(
        self,
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
    ) -> SubprocessResult:
        """`init -input=false`."""
        return self._run(
            ["init", "-input=false"], workspace_dir, target, settings, operation_id, step_name, 900
        )

    def plan(
        self,
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
        plan_file_name: str = "console.tfplan",
    ) -> PlanRunResult:
        """`plan -detailed-exitcode -out=<file>` then `show -json <file>`; exit 2 means changes."""
        result = self._run(
            ["plan", "-input=false", "-detailed-exitcode", f"-out={plan_file_name}"],
            workspace_dir,
            target,
            settings,
            operation_id,
            step_name,
            1800,
            success_exit_codes=(0, 2),
        )
        if result.exit_code not in (0, 2):
            return PlanRunResult(result=result, has_changes=False, plan_json={})
        shown = self._run(
            ["show", "-json", plan_file_name], workspace_dir, target, settings, operation_id, step_name, 300
        )
        plan_json = (
            json.loads(shown.standard_output) if shown.succeeded and shown.standard_output.strip() else {}
        )
        if plan_json:
            # Only the redacted form of a plan is ever kept; the raw stdout may carry sensitive values.
            self.recorder.replace_standard_output(shown.record_id, json.dumps(redact_plan_json(plan_json)))
        return PlanRunResult(result=result, has_changes=result.exit_code == 2, plan_json=plan_json)

    def apply(
        self,
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
        plan_file_name: str = "console.tfplan",
    ) -> SubprocessResult:
        """`apply -input=false <file>`."""
        return self._run(
            ["apply", "-input=false", plan_file_name],
            workspace_dir,
            target,
            settings,
            operation_id,
            step_name,
            7200,
        )

    def state_list(
        self,
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
    ) -> SubprocessResult:
        """`state list`: the addresses state tracks, one per line of standard output; AWS is not touched.

        Args:
            workspace_dir: The target's workspace directory (initialised).
            target: The target whose credentials the binary runs with.
            settings: Operator settings.
            operation_id: Operation the execution record belongs to.
            step_name: Step the execution record belongs to.

        Returns:
            The recorded subprocess result.

        Example:
            >>> runner.state_list(  # doctest: +SKIP
            ...     workspace_dir, target, settings, operation_id=operation_id, step_name="reconcile"
            ... ).standard_output.splitlines()
            ['aws_db_instance.demo_orders', 'aws_db_parameter_group.demo_postgres16_tuned']
        """
        return self._run(["state", "list"], workspace_dir, target, settings, operation_id, step_name, 600)

    def state_move(
        self,
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        source_address: str,
        destination_address: str,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
    ) -> SubprocessResult:
        """`state mv <source> <destination>`: give the object state tracks at one address another address.

        AWS is not touched. Used after a blue/green switchover: the production address tracks the blue
        instance's resource id, which now belongs to `<identifier>-old1`, so its entry moves to that address.

        Args:
            workspace_dir: The target's workspace directory (initialised).
            target: The target whose credentials the binary runs with.
            settings: Operator settings.
            source_address: The address state tracks the object at, e.g. `aws_db_instance.demo_orders`.
            destination_address: The address to track it at, e.g. `aws_db_instance.demo_orders_old1`.
            operation_id: Operation the execution record belongs to.
            step_name: Step the execution record belongs to.

        Returns:
            The recorded subprocess result; state is unchanged when it did not succeed.

        Example:
            >>> runner.state_move(  # doctest: +SKIP
            ...     workspace_dir, target, settings, "aws_db_instance.demo_orders",
            ...     "aws_db_instance.demo_orders_old1", operation_id=operation_id, step_name="reconcile",
            ... ).succeeded
            True
        """
        return self._run(
            ["state", "mv", source_address, destination_address],
            workspace_dir,
            target,
            settings,
            operation_id,
            step_name,
            600,
        )

    def state_show(
        self,
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        address: str,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
    ) -> StateShowResult:
        """`state show -json <address>`: the attributes state holds at `address`; AWS is not touched.

        Used to tell which AWS object an address tracks (its `identifier`) when state already holds both
        sides of a move. State carries sensitive values (a master password), so the execution record keeps
        only the address and its identity attributes (`STATE_SHOW_IDENTITY_ATTRIBUTES`).

        Args:
            workspace_dir: The target's workspace directory (initialised).
            target: The target whose credentials the binary runs with.
            settings: Operator settings.
            address: The Terraform address to show, e.g. `aws_db_instance.demo_orders_old1`.
            operation_id: Operation the execution record belongs to.
            step_name: Step the execution record belongs to.

        Returns:
            The recorded result and the attributes; `attributes` is `{}` when the binary failed.

        Example:
            >>> runner.state_show(  # doctest: +SKIP
            ...     workspace_dir, target, settings, "aws_db_instance.demo_orders_old1",
            ...     operation_id=operation_id, step_name="reconcile",
            ... ).attributes["identifier"]
            'demo-orders-old1'
        """
        result = self._run(
            ["state", "show", "-json", address], workspace_dir, target, settings, operation_id, step_name, 600
        )
        attributes = _state_show_attributes(result.standard_output, address) if result.succeeded else {}
        identity = {key: attributes[key] for key in STATE_SHOW_IDENTITY_ATTRIBUTES if key in attributes}
        # Only the identity is ever kept; the raw stdout carries every attribute, sensitive ones included.
        self.recorder.replace_standard_output(result.record_id, json.dumps({"address": address, **identity}))
        if result.succeeded and not attributes:
            logger.warning(
                "terraform_state_show_unreadable",
                address=address,
                fix_suggestion="run `state show -json` on the address in the workspace by hand and check the "
                "configured binary supports `state show -json` (OpenTofu does)",
            )
        return StateShowResult(result=result, attributes=attributes)

    def state_remove(
        self,
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        address: str,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
    ) -> SubprocessResult:
        """`state rm <address>`: forget the object state holds at `address`; AWS is not touched.

        Used before importing an address again when state tracks an object that no longer answers to it
        (after a blue/green switchover, the blue instance's resource id). The binary fails with
        `No matching objects found` when state does not hold the address.

        Args:
            workspace_dir: The target's workspace directory (initialised).
            target: The target whose credentials the binary runs with.
            settings: Operator settings.
            address: The Terraform address to remove from state, e.g. `aws_db_instance.demo_orders`.
            operation_id: Operation the execution record belongs to.
            step_name: Step the execution record belongs to.

        Returns:
            The recorded subprocess result.

        Example:
            >>> runner.state_remove(  # doctest: +SKIP
            ...     workspace_dir, target, settings, "aws_db_instance.demo_orders",
            ...     operation_id=operation_id, step_name="reconcile",
            ... ).succeeded
            True
        """
        return self._run(
            ["state", "rm", address], workspace_dir, target, settings, operation_id, step_name, 600
        )

    def apply_refresh_only(
        self,
        workspace_dir: Path,
        target: ResolvedTarget,
        settings: OperatorSettings,
        *,
        operation_id: uuid.UUID | None,
        step_name: str | None,
    ) -> SubprocessResult:
        """`apply -refresh-only -auto-approve -input=false`: bring state in line with AWS and nothing else.

        Args:
            workspace_dir: The target's workspace directory.
            target: The target whose credentials the binary runs with.
            settings: Operator settings.
            operation_id: Operation the execution record belongs to.
            step_name: Step the execution record belongs to.

        Returns:
            The recorded subprocess result; `succeeded` is False when the binary failed.

        Example:
            >>> runner.apply_refresh_only(  # doctest: +SKIP
            ...     workspace_dir, target, settings, operation_id=operation_id, step_name="refresh_state"
            ... ).succeeded
            True
        """
        return self._run(
            ["apply", "-refresh-only", "-auto-approve", "-input=false"],
            workspace_dir,
            target,
            settings,
            operation_id,
            step_name,
            7200,
        )
