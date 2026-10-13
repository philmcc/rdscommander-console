"""`drift_detect`: one step per target; writes and closes drift findings.

Detection only reads: it compares the committed record with the latest inventory snapshot and, in
Terraform mode, asks `plan` what differs from state. It never changes anything in AWS or in state. The one
thing it writes is the first record of a direct-mode target: that environment has no adoption to create
`console-record.json`, so the first run renders the whole estate from the latest snapshot and commits it
as the baseline to compare against from then on. It does so only from a checkout the guard accepts, and it
puts the workspace back as `HEAD` has it when the commit fails, so the next run starts again.

Example:
    >>> OperationService(session_factory).create(  # doctest: +SKIP
    ...     "drift_detect", "sandbox", None, {"options": {}}, "drift-1", "phil"
    ... )
    <Operation ...>
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig, ResolvedTarget
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import (
    DriftFinding,
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryParameterGroup,
    InventorySnapshot,
    InventorySubnetGroup,
    Operation,
    TerraformWorkspace,
    utc_now,
)
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.parameters.direct_api import queued_attribute_values
from rdscommander.repo.commit import GitCommitError, commit_paths
from rdscommander.repo.git_runner import run_git_recorded
from rdscommander.repo.guard import check_checkout
from rdscommander.terraform.names import terraform_address_for
from rdscommander.terraform.plan_parser import ResourceChange, summarise_plan_json
from rdscommander.terraform.record import (
    PRESENCE_ATTRIBUTE,
    RECORD_FILE_NAME,
    RESOURCE_SECTIONS,
    DriftAttribute,
    diff_records,
    read_record,
    replace_resource,
    resource_addresses,
    resource_identifiers,
    with_references_resolved,
)
from rdscommander.terraform.render_input import RenderWorkspaceInput, build_render_input
from rdscommander.terraform.renderer import render_workspace_files, write_workspace_files
from rdscommander.terraform.state_addresses import (
    TerraformStateUnreadableError,
    read_state_addresses,
    sync_adoptions_with_state,
)
from rdscommander.terraform.workspace_revert import revert_workspace
from rdscommander.terraform.workspaces import (
    adopted_terraform_addresses,
    ensure_workspace_record,
    workspace_directory,
)

logger = get_logger(__name__)

STEP_PREFIX = "detect:"
RECORD_FINDING_KIND = "record"
STATE_FINDING_KIND = "state"
STATE_DRIFT_PLAN_FILE_NAME = "drift.tfplan"
PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN = (0, 2)
FindingAttributes = dict[str, list[dict[str, Any]]]
"""`{resource ARN: [attribute entries]}` for the resources that drifted."""


def arns_by_terraform_address(session: Session, snapshot_id: int) -> dict[str, str]:
    """Map every Terraform address the renderer can produce for a snapshot to the resource's ARN.

    Args:
        session: Control database session.
        snapshot_id: The inventory snapshot to read.

    Returns:
        `{address: ARN}` for instances, cluster members, clusters, parameter groups and subnet groups.

    Example:
        >>> arns_by_terraform_address(session, 12)["aws_db_instance.demo_orders"]  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:db:demo-orders'
    """
    arns: dict[str, str] = {}
    for instance_row in session.execute(
        select(InventoryDbInstance).where(InventoryDbInstance.snapshot_id == snapshot_id)
    ).scalars():
        resource_type = (
            "aws_rds_cluster_instance" if instance_row.db_cluster_identifier else "aws_db_instance"
        )
        address = terraform_address_for(resource_type, instance_row.db_instance_identifier)
        arns[address] = instance_row.db_instance_arn
    for cluster_row in session.execute(
        select(InventoryDbCluster).where(InventoryDbCluster.snapshot_id == snapshot_id)
    ).scalars():
        arns[terraform_address_for("aws_rds_cluster", cluster_row.db_cluster_identifier)] = (
            cluster_row.db_cluster_arn
        )
    for group_row in session.execute(
        select(InventoryParameterGroup).where(InventoryParameterGroup.snapshot_id == snapshot_id)
    ).scalars():
        resource_type = (
            "aws_db_parameter_group"
            if group_row.group_scope == "instance"
            else "aws_rds_cluster_parameter_group"
        )
        arns[terraform_address_for(resource_type, group_row.parameter_group_name)] = (
            group_row.parameter_group_arn
        )
    for subnet_group_row in session.execute(
        select(InventorySubnetGroup).where(InventorySubnetGroup.snapshot_id == snapshot_id)
    ).scalars():
        arns[terraform_address_for("aws_db_subnet_group", subnet_group_row.db_subnet_group_name)] = (
            subnet_group_row.db_subnet_group_arn
        )
    return arns


def queued_values_by_terraform_address(session: Session, snapshot_id: int) -> dict[str, dict[str, Any]]:
    """Per instance and cluster, the modifications AWS holds for the maintenance window, by attribute.

    Read from the snapshot's described `PendingModifiedValues`, so detection makes no AWS call.

    Args:
        session: Control database session.
        snapshot_id: The inventory snapshot to read.

    Returns:
        `{address: {attribute: queued value}}`; resources with nothing queued are absent.

    Example:
        >>> queued_values_by_terraform_address(session, 12)  # doctest: +SKIP
        {'aws_db_instance.demo_orders': {'backup_retention_period': 14}}
    """
    queued: dict[str, dict[str, Any]] = {}
    for instance_row in session.execute(
        select(InventoryDbInstance).where(InventoryDbInstance.snapshot_id == snapshot_id)
    ).scalars():
        resource_type = (
            "aws_rds_cluster_instance" if instance_row.db_cluster_identifier else "aws_db_instance"
        )
        values = queued_attribute_values(
            resource_type, (instance_row.raw_description or {}).get("PendingModifiedValues") or {}
        )
        if values:
            queued[terraform_address_for(resource_type, instance_row.db_instance_identifier)] = values
    for cluster_row in session.execute(
        select(InventoryDbCluster).where(InventoryDbCluster.snapshot_id == snapshot_id)
    ).scalars():
        values = queued_attribute_values(
            "aws_rds_cluster", (cluster_row.raw_description or {}).get("PendingModifiedValues") or {}
        )
        if values:
            queued[terraform_address_for("aws_rds_cluster", cluster_row.db_cluster_identifier)] = values
    return queued


def without_queued_values(
    differences: dict[str, list[DriftAttribute]], queued: dict[str, dict[str, Any]]
) -> dict[str, list[DriftAttribute]]:
    """Drop the differences that are a change AWS holds for the maintenance window.

    An attribute whose live value differs from the record is not drift when AWS has queued exactly the
    recorded value: the record holds what the operator asked for, and AWS has not applied it yet.

    Example:
        >>> without_queued_values(  # doctest: +SKIP
        ...     {"aws_db_instance.x": [DriftAttribute(address="aws_db_instance.x",
        ...      attribute="multi_az", recorded=True, live=False)]},
        ...     {"aws_db_instance.x": {"multi_az": True}},
        ... )
        {}
    """
    kept: dict[str, list[DriftAttribute]] = {}
    for address, attributes in differences.items():
        queued_here = queued.get(address, {})
        remaining = [
            attribute
            for attribute in attributes
            if not (
                attribute.attribute in queued_here and queued_here[attribute.attribute] == attribute.recorded
            )
        ]
        if remaining:
            kept[address] = remaining
    return kept


def build_live_render_input(
    session: Session,
    target: ResolvedTarget,
    console_config: ConsoleConfig,
    snapshot: InventorySnapshot,
    workspace: TerraformWorkspace,
    simulated_endpoint_url: str | None,
) -> RenderWorkspaceInput:
    """The renderer input for reality: the latest snapshot, for the resources under the record.

    The resources are those the console has adopted into the workspace, or every non-default resource in a
    direct-mode environment. An adoption that is waiting for approval does not count: its resources become
    adopted, and enter the record, only once the import has applied and verified.

    Args:
        session: Control database session.
        target: The target being compared.
        console_config: Loaded console configuration.
        snapshot: The latest inventory snapshot of `target`.
        workspace: The target's workspace row.
        simulated_endpoint_url: Endpoint of the simulated AWS, for simulated targets.

    Returns:
        A renderer input comparable with the record's.

    Example:
        >>> live = build_live_render_input(  # doctest: +SKIP
        ...     session, target, console_config, snapshot, workspace, None
        ... )
    """
    return build_render_input(
        session,
        target,
        console_config,
        snapshot,
        set(),
        adopted_terraform_addresses(session, workspace),
        include_imports=False,
        simulated_endpoint_url=simulated_endpoint_url,
        include_all=target.is_direct,
    )


def _state_drift_attributes(change: ResourceChange) -> list[DriftAttribute]:
    """What a planned change says about the resource: it is missing or extra, or these attributes differ.

    In a plan, `before` is what state holds after refreshing from AWS (reality) and `after` is what the
    files ask for (the record).
    """
    if change.actions == ["create"]:
        return [
            DriftAttribute(address=change.address, attribute=PRESENCE_ATTRIBUTE, recorded=True, live=False)
        ]
    if change.actions == ["delete"]:
        return [
            DriftAttribute(address=change.address, attribute=PRESENCE_ATTRIBUTE, recorded=False, live=True)
        ]
    return [
        DriftAttribute(
            address=change.address,
            attribute=attribute_name,
            recorded=(change.after or {}).get(attribute_name),
            live=(change.before or {}).get(attribute_name),
        )
        for attribute_name in change.changed_attribute_names
    ]


def _reconcile_findings(
    session: Session,
    target: ResolvedTarget,
    finding_kind: str,
    current: FindingAttributes,
    operation_id: uuid.UUID,
) -> None:
    """Make the open findings of `finding_kind` for `target` equal `current`; the caller's session commits.

    A finding that is still current keeps its original detection time and takes the new attributes; a
    finding whose resource no longer differs is resolved by `operation_id`.
    """
    open_findings = {
        finding.resource_arn: finding
        for finding in session.execute(
            select(DriftFinding).where(
                DriftFinding.target_key == target.target_key,
                DriftFinding.finding_kind == finding_kind,
                DriftFinding.resolved_at_utc.is_(None),
            )
        ).scalars()
    }
    for resource_arn, attributes in current.items():
        existing_finding = open_findings.get(resource_arn)
        if existing_finding is not None:
            existing_finding.attributes = attributes
            continue
        session.add(
            DriftFinding(
                resource_arn=resource_arn,
                environment_slug=target.environment_slug,
                target_key=target.target_key,
                finding_kind=finding_kind,
                attributes=attributes,
                detected_by_operation_id=operation_id,
            )
        )
    for resource_arn, finding in open_findings.items():
        if resource_arn not in current:
            finding.resolved_at_utc = utc_now()
            finding.resolved_by_operation_id = operation_id
    session.commit()


class DriftDetectExecutor:
    """Compares the record with reality for every target of the environment."""

    operation_kind = "drift_detect"

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store` whenever a step needs it."""
        self.config_store = config_store

    def step_names(self, operation: Operation) -> list[str]:
        """`detect:<target_key>` per target of the operation's environment."""
        targets = self.config_store.current.targets_for_environment(operation.environment_slug or "")
        return [f"{STEP_PREFIX}{target.target_key}" for target in targets]

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """Satisfied once this operation already detected for the target."""
        already_detected = context.step_already_completed(step_name)
        return StepObservation(
            state=ObservationState.SATISFIED if already_detected else ObservationState.ABSENT
        )

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Record drift always; state drift in Terraform mode; close findings that disappeared.

        Args:
            step_name: `detect:<target_key>`.
            context: The step context; its session is committed as findings change.

        Returns:
            Completed with the finding counts, or failed with a diagnostic when the record cannot be read,
            the first record of a direct-mode target cannot be written (the checkout guard refuses the
            checkout, or git refuses the commit) or `plan` fails. A target without a snapshot, or a
            Terraform-mode target without a record, completes with nothing to do. A direct-mode target
            without a record is recorded from the latest snapshot and reports no findings.
        """
        target = context.console_config.target(step_name.removeprefix(STEP_PREFIX))
        session = context.session
        logger.info(
            "drift_detection_started", target_key=target.target_key, operation_id=str(context.operation.id)
        )
        snapshot = latest_snapshot_for_target(session, target.target_key)
        workspace = ensure_workspace_record(session, target, context.console_config)
        session.commit()
        directory = workspace_directory(context.settings.repo_root, workspace)
        try:
            recorded = read_record(directory)
        except ValueError as error:
            return self._fail(
                context,
                step_name,
                "record_unreadable",
                "The Terraform record file cannot be read",
                f"{directory / RECORD_FILE_NAME}: {error}",
                "restore console-record.json from git (git checkout -- <file>) or accept the live estate "
                "by adopting again, then run Detect drift",
            )
        if snapshot is None or (recorded is None and not target.is_direct):
            reason = "no inventory snapshot yet" if snapshot is None else "nothing has been recorded yet"
            logger.info("drift_detection_skipped", target_key=target.target_key, reason=reason)
            return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"skipped": reason})

        arns = arns_by_terraform_address(session, snapshot.id)
        adopted: set[str] = set()
        if not target.is_direct:
            try:
                adopted = self._adopted_addresses(context, target, workspace, arns, step_name)
            except TerraformStateUnreadableError as error:
                return self._fail(
                    context, step_name, error.diagnostic_kind, error.title, str(error), error.fix_suggestion
                )
        live = build_live_render_input(
            session,
            target,
            context.console_config,
            snapshot,
            workspace,
            context.settings.simulated_aws_endpoint_url,
        )
        output: dict[str, Any] = {"snapshot_id": snapshot.id}
        if recorded is None:
            record_failure = self._record_estate(
                context, target, workspace, directory, live, snapshot, step_name
            )
            if record_failure is not None:
                return record_failure
            recorded_input = live
            output["record_bootstrapped"] = True
        else:
            rebaselined = self._record_matching_the_mode(
                context,
                target,
                workspace,
                directory,
                recorded.render_input,
                live,
                adopted,
                snapshot,
                step_name,
            )
            if isinstance(rebaselined, StepOutcome):
                return rebaselined
            recorded_input, changed_addresses = rebaselined
            if changed_addresses:
                key = "record_extended" if target.is_direct else "record_rebaselined"
                output[key] = changed_addresses
        record_drift: FindingAttributes = {
            arns.get(address, address): [attribute.model_dump(mode="json") for attribute in attributes]
            for address, attributes in without_queued_values(
                diff_records(recorded_input, live), queued_values_by_terraform_address(session, snapshot.id)
            ).items()
        }
        _reconcile_findings(session, target, RECORD_FINDING_KIND, record_drift, context.operation.id)
        output["record_findings"] = len(record_drift)

        if target.is_direct:
            # Direct mode has no state; findings left over from a Terraform-mode past are closed.
            _reconcile_findings(session, target, STATE_FINDING_KIND, {}, context.operation.id)
            output["state_findings"] = 0
        elif uncommitted_files := self._uncommitted_workspace_files(context, target, workspace, step_name):
            # A plan compares AWS with the files on disk; uncommitted files (a change awaiting approval, a
            # hand edit) are not the record, so their difference from AWS is not drift. Open state findings
            # stay as they are until a detection can plan against committed files.
            output["state_drift"] = (
                f"skipped: the workspace has uncommitted files ({', '.join(uncommitted_files)}); planning "
                "against them would report a pending change as drift"
            )
            logger.info(
                "state_drift_skipped_for_uncommitted_files",
                target_key=target.target_key,
                operation_id=str(context.operation.id),
                uncommitted_files=uncommitted_files,
            )
        elif (directory / ".terraform").is_dir():
            state_drift = self._detect_state_drift(context, target, directory, step_name, arns)
            if isinstance(state_drift, StepOutcome):
                return state_drift
            _reconcile_findings(session, target, STATE_FINDING_KIND, state_drift, context.operation.id)
            output["state_findings"] = len(state_drift)
        else:
            output["state_drift"] = "skipped: the workspace has not been initialised on this machine"
        logger.info("drift_detection_completed", target_key=target.target_key, **output)
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output=output)

    def _uncommitted_workspace_files(
        self, context: StepContext, target: ResolvedTarget, workspace: TerraformWorkspace, step_name: str
    ) -> list[str]:
        """The workspace's files that differ from what is committed (`git status --porcelain`), if any.

        When git cannot answer, the answer is the error, so state drift is skipped rather than planned
        against files nobody can vouch for.
        """
        status = run_git_recorded(
            context.services.execution_recorder,
            context.settings.repo_root,
            ["-c", "core.quotePath=false", "status", "--porcelain", "--", workspace.workspace_relative_dir],
            operation_id=context.operation.id,
            step_name=step_name,
            target_key=target.target_key,
        )
        if not status.succeeded:
            return [f"git status failed: {status.standard_error.strip()}"]
        return [line[3:] for line in status.standard_output.splitlines() if line.strip()]

    def _adopted_addresses(
        self,
        context: StepContext,
        target: ResolvedTarget,
        workspace: TerraformWorkspace,
        arns: dict[str, str],
        step_name: str,
    ) -> set[str]:
        """What is under Terraform in this workspace: the adoption rows plus what state holds.

        Raises:
            TerraformStateUnreadableError: The state object could not be read.
        """
        s3 = context.services.aws_sessions.client(
            target, "s3", operation_id=context.operation.id, step_name=step_name
        )
        in_state = read_state_addresses(s3, workspace.state_bucket_name, workspace.state_key)
        return sync_adoptions_with_state(context.session, workspace, in_state, arns)

    def _record_matching_the_mode(
        self,
        context: StepContext,
        target: ResolvedTarget,
        workspace: TerraformWorkspace,
        directory: Path,
        recorded: RenderWorkspaceInput,
        live: RenderWorkspaceInput,
        adopted: set[str],
        snapshot: InventorySnapshot,
        step_name: str,
    ) -> tuple[RenderWorkspaceInput, list[str]] | StepOutcome:
        """The record to compare with, brought in line with the environment's mode first when it is not.

        After a switch of mode the record covers what the other mode covered:

        * Direct mode records every resource. Live resources the record lacks (a switch from Terraform
          mode, where only adopted resources are recorded) join the record from the snapshot in one
          `Record estate` commit, rather than each becoming a presence finding.
        * Terraform mode records what is under Terraform only. Recorded addresses that are not adopted (a
          switch from direct mode) leave the record and the files in one `Record adopted resources` commit;
          what stays keeps its recorded values, so its drift stays visible.

        Both commits run the checkout guard first and revert the files when git refuses the commit, as the
        first record of the estate does.

        Returns:
            `(record input, addresses that joined or left)`, or the failed outcome (diagnostic recorded).
        """
        recorded_addresses = resource_addresses(recorded)
        if target.is_direct:
            changed = sorted(resource_addresses(live) - recorded_addresses)
            title = f"Record estate: {target.environment_slug} ({context.operation.created_by_operator_name})"
            body = "Resources the record did not hold yet:\n" + "\n".join(f"- {a}" for a in changed)
        else:
            changed = sorted(recorded_addresses - adopted)
            title = (
                f"Record adopted resources: {target.environment_slug} "
                f"({context.operation.created_by_operator_name})"
            )
            body = "Resources that are not under Terraform left the record:\n" + "\n".join(
                f"- {a}" for a in changed
            )
        if not changed:
            return recorded, []
        updated = recorded
        nothing = recorded.model_copy(update={section: [] for section in RESOURCE_SECTIONS.values()})
        for address in changed:
            updated = replace_resource(updated, address, live if target.is_direct else nothing)
        updated = with_references_resolved(
            updated, {**resource_identifiers(recorded), **resource_identifiers(live)}
        )
        failure = self._record_estate(
            context, target, workspace, directory, updated, snapshot, step_name, title=title, body=body
        )
        if failure is not None:
            return failure
        return updated, changed

    def _record_estate(
        self,
        context: StepContext,
        target: ResolvedTarget,
        workspace: TerraformWorkspace,
        directory: Path,
        estate: RenderWorkspaceInput,
        snapshot: InventorySnapshot,
        step_name: str,
        title: str | None = None,
        body: str | None = None,
    ) -> StepOutcome | None:
        """Write `estate` as the target's record and commit it; None on success.

        A direct-mode environment has no adoption to create the record, so the first detection does it:
        the Terraform files and `console-record.json` are rendered from `snapshot` and committed as
        `Record estate: <environment slug> (<operator>)`. The console commits but never pushes. A switch of
        mode commits the record brought in line with the mode the same way, under `title` and `body`.

        This runs unattended (the scheduler starts detection), so it first runs the checkout guard that every
        other committer runs and writes nothing when the guard refuses: a hand edit under the workspace, files
        staged elsewhere, a checkout behind its upstream. When git refuses the commit, the written files
        are reverted, so no uncommitted baseline is left for later runs to compare against and the next
        detection records the estate again.

        Args:
            context: The step context.
            target: The direct-mode target being recorded.
            workspace: The target's workspace row; its last applied commit becomes the record commit.
            directory: The target's workspace directory.
            estate: The renderer input for every resource of the target, built from `snapshot`.
            snapshot: The inventory snapshot the record is rendered from.
            step_name: `detect:<target_key>`.
            title: The commit subject; `Record estate: <environment slug> (<operator>)` when omitted.
            body: The commit body, if any.

        Returns:
            None when the record is written and committed; otherwise the failed outcome (diagnostic
            already recorded) for a checkout the guard refused or a commit that git refused.

        Example:
            >>> self._record_estate(  # doctest: +SKIP
            ...     context, target, workspace, directory, estate, snapshot, "detect:sandbox/0/eu-west-2"
            ... )
        """
        report = check_checkout(
            context.services.execution_recorder,
            context.settings.repo_root,
            Path(workspace.workspace_relative_dir),
            workspace.last_applied_commit_sha,
            context.settings.allow_checkout_without_upstream,
            operation_id=context.operation.id,
            step_name=step_name,
            target_key=target.target_key,
        )
        if not report.ok:
            logger.info(
                "record_bootstrap_refused_by_checkout_guard",
                target_key=target.target_key,
                operation_id=str(context.operation.id),
                problem_codes=[problem.code for problem in report.problems],
            )
            return self._fail(
                context,
                step_name,
                "checkout_guard",
                "Repository checkout is not ready to record the estate",
                report.summary(),
                "; ".join(problem.fix_suggestion for problem in report.problems),
            )
        files = render_workspace_files(estate, format_binary=context.settings.terraform_binary)
        write_workspace_files(directory, files, record=estate, snapshot_id=snapshot.id)
        operator_name = context.operation.created_by_operator_name
        message = title or f"Record estate: {target.environment_slug} ({operator_name})"
        if body:
            message = f"{message}\n\n{body}"
        try:
            commit_sha = commit_paths(
                context.services.execution_recorder,
                context.settings.repo_root,
                [Path(workspace.workspace_relative_dir)],
                message,
                context.settings.git_author_name,
                context.settings.git_author_email,
                operation_id=context.operation.id,
                step_name=step_name,
                target_key=target.target_key,
            )
        except GitCommitError as error:
            reverted = revert_workspace(context, workspace, step_name)
            relative_dir = Path(workspace.workspace_relative_dir).as_posix()
            return self._fail(
                context,
                step_name,
                "commit_failed",
                "Committing the first record of the estate failed",
                str(error),
                "open the execution record for the git error (identity, hooks, repository state) and fix it; "
                + (
                    "the written files were reverted, so the next Detect drift records the estate again"
                    if reverted
                    else f"git could not revert the written files: run `git checkout -- {relative_dir}` by "
                    "hand, then run Detect drift to record the estate again"
                ),
            )
        workspace.last_applied_commit_sha = commit_sha
        context.session.commit()
        logger.info(
            "record_bootstrapped",
            target_key=target.target_key,
            operation_id=str(context.operation.id),
            snapshot_id=snapshot.id,
            commit_sha=commit_sha,
        )
        return None

    def _detect_state_drift(
        self,
        context: StepContext,
        target: ResolvedTarget,
        directory: Path,
        step_name: str,
        arns: dict[str, str],
    ) -> FindingAttributes | StepOutcome:
        """Findings from `plan`, or the failed outcome (diagnostic already recorded) when the plan failed."""
        run = context.services.terraform_runner.plan(
            directory,
            target,
            context.settings,
            operation_id=context.operation.id,
            step_name=step_name,
            plan_file_name=STATE_DRIFT_PLAN_FILE_NAME,
        )
        if run.result.exit_code not in PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN:
            return self._fail(
                context,
                step_name,
                "terraform_plan_failed",
                "Terraform plan failed while checking for state drift",
                run.result.standard_error,
                "open the execution record; fix the reported credential, backend or configuration "
                "problem, then run Detect drift again",
            )
        state_drift: FindingAttributes = {}
        for change in summarise_plan_json(run.plan_json).resource_changes:
            if change.actions == ["no-op"] or change.is_import:
                continue  # an import is an adoption in flight, not drift
            attributes = _state_drift_attributes(change)
            if attributes:
                state_drift[arns.get(change.address, change.address)] = [
                    attribute.model_dump(mode="json") for attribute in attributes
                ]
        return state_drift

    def _fail(
        self,
        context: StepContext,
        step_name: str,
        diagnostic_kind: str,
        title: str,
        detail: str,
        fix_suggestion: str,
    ) -> StepOutcome:
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="error",
            diagnostic_kind=diagnostic_kind,
            title=title,
            detail=detail,
            fix_suggestion=fix_suggestion,
        )
        context.session.commit()
        return StepOutcome(
            status=StepOutcomeStatus.FAILED,
            detail=detail,
            fix_suggestion=fix_suggestion,
            diagnostic_recorded=True,
        )
