"""`environment_import`: resolve the account, scan regions, write and commit the configuration, then refresh.

Nothing about the new environment is written until the account has been confirmed and the regions have
been scanned, so a failed login or a typo leaves `config/environments.yaml` exactly as it was. The write
validates the whole file before it replaces it, and the commit names only that file.

Example:
    >>> OperationService(session_factory).create(  # doctest: +SKIP
    ...     "environment_import", None, None, {"options": {"slug": "staging", ...}}, "import-1", "phil",
    ...     title="Import environment: staging",
    ... )
    <Operation ...>
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from rdscommander.aws import profiles
from rdscommander.aws.errors import diagnose_exception
from rdscommander.aws.rds_reader import RdsReader
from rdscommander.catalogue.options import EnvironmentImportOptions
from rdscommander.config.loader import ConfigurationError
from rdscommander.config.models import EnvironmentConfig, EnvironmentTarget
from rdscommander.config.store import (
    ENVIRONMENTS_FILE_NAME,
    ConfigStoreProtocol,
    EnvironmentsFileChangedError,
)
from rdscommander.control_db.models import Operation
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.operations.service import OperationService
from rdscommander.repo.commit import GitCommitError, commit_paths
from rdscommander.repo.git_runner import run_git_recorded

logger = get_logger(__name__)

STEP_NAMES = [
    "resolve_account",
    "scan_regions",
    "write_configuration",
    "commit_configuration",
    "reload_configuration",
    "queue_refresh",
]
AWS_FAILURE_TITLES = {
    "resolve_account": "The AWS account could not be confirmed",
    "scan_regions": "The AWS regions could not be scanned",
}
AUTOMATIC_SCAN_SKIPPABLE_ERROR_CODES = frozenset(
    {
        "OptInRequired",
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "AuthFailure",
        "UnauthorizedOperation",
    }
)
"""What a region answers when it is not enabled for the account (or rejects the credentials there). A scan of
every region skips such a region; a region the operator named explicitly fails the import instead."""
ACCOUNT_LOOKUP_REGION = "us-east-1"
"""Region used for the account-level calls (`account:ListRegions`) when the operator named no region."""


class EnvironmentImportExecutor:
    """Adds an environment to `config/environments.yaml` from a profile or the simulated account."""

    operation_kind = "environment_import"

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read and replace the live configuration through `config_store`."""
        self.config_store = config_store

    def step_names(self, operation: Operation) -> list[str]:
        """The fixed steps: `resolve_account` ... `queue_refresh`."""
        return list(STEP_NAMES)

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """A step that already completed is satisfied and hands its earlier output on to the later steps.

        `write_configuration` is also satisfied when the live configuration already holds exactly the
        environment this import would write, which is what a crash between the write and the end of the
        step leaves behind.
        """
        if context.step_already_completed(step_name):
            return StepObservation(
                state=ObservationState.SATISFIED, output=context.completed_step_output(step_name) or {}
            )
        if step_name == "write_configuration" and self._configuration_already_written(context):
            return StepObservation(
                state=ObservationState.SATISFIED, detail="the environment is already in the configuration"
            )
        return StepObservation(state=ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Run one step.

        Args:
            step_name: One of `STEP_NAMES`.
            context: The step context; its session is committed when a diagnostic is recorded.

        Returns:
            Completed with the step's output, or failed with a diagnostic that says how to fix the cause.
        """
        logger.info(
            "environment_import_step_started",
            step_name=step_name,
            operation_id=str(context.operation.id),
            environment_slug=self._options(context).slug,
        )
        outcome: StepOutcome = getattr(self, f"_execute_{step_name}")(context)
        logger.info(
            "environment_import_step_finished",
            step_name=step_name,
            operation_id=str(context.operation.id),
            outcome_status=outcome.status.value,
        )
        return outcome

    def _options(self, context: StepContext) -> EnvironmentImportOptions:
        """The validated options the import was created with."""
        return EnvironmentImportOptions.model_validate(context.operation.plan["options"])

    def _fail(
        self, context: StepContext, step_name: str, kind: str, title: str, detail: str, fix_suggestion: str
    ) -> StepOutcome:
        """Record a diagnostic and return the failed outcome that tells the runner it is already recorded."""
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="error",
            diagnostic_kind=kind,
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

    def _fail_for_aws_error(
        self,
        context: StepContext,
        step_name: str,
        error: ClientError | BotoCoreError,
        options: EnvironmentImportOptions,
        detail_prefix: str = "",
    ) -> StepOutcome:
        """Turn an AWS failure into the specific diagnostic (expired login, denied, throttled, ...)."""
        kind, detail, fix_suggestion = diagnose_exception(
            error, options.aws_profile_name, traceback.format_exc()
        )
        return self._fail(
            context,
            step_name,
            kind,
            AWS_FAILURE_TITLES[step_name],
            f"{detail_prefix}{detail}",
            fix_suggestion,
        )

    def _client(
        self, context: StepContext, options: EnvironmentImportOptions, region: str, service: str, step: str
    ) -> BaseClient:
        """A recorded client for the account being imported."""
        return profiles.client_for_profile(
            options.aws_profile_name,
            region,
            service,
            context.settings,
            context.services.execution_recorder,
            simulated=options.simulated,
            operation_id=context.operation.id,
            step_name=step,
        )

    def _execute_resolve_account(self, context: StepContext) -> StepOutcome:
        options = self._options(context)
        if any(environment.slug == options.slug for environment in self.config_store.current.environments):
            return self._fail(
                context,
                "resolve_account",
                "environment_exists",
                "An environment with this slug exists",
                f"slug {options.slug!r} is already configured",
                "choose another slug, or open the existing environment",
            )
        try:
            region = options.regions[0] if options.regions else ACCOUNT_LOOKUP_REGION
            sts_client = self._client(context, options, region, "sts", "resolve_account")
            aws_account_id = str(profiles.caller_identity(sts_client)["Account"])
        except (ClientError, BotoCoreError) as error:
            return self._fail_for_aws_error(context, "resolve_account", error, options)
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"aws_account_id": aws_account_id})

    def _execute_scan_regions(self, context: StepContext) -> StepOutcome:
        options = self._options(context)
        scanned_regions = list(options.regions)
        if not scanned_regions:
            try:
                account_client = self._client(
                    context, options, ACCOUNT_LOOKUP_REGION, "account", "scan_regions"
                )
            except (ClientError, BotoCoreError) as error:
                return self._fail_for_aws_error(context, "scan_regions", error, options)
            scanned_regions = profiles.enabled_regions(account_client)
        is_automatic_scan = not options.regions
        resource_counts_by_region: dict[str, dict[str, int]] = {}
        skipped_region_error_codes: dict[str, str] = {}
        for region in scanned_regions:
            try:
                reader = RdsReader(self._client(context, options, region, "rds", "scan_regions"))
                resource_counts_by_region[region] = {
                    "instances": len(reader.list_postgres_instances()),
                    "clusters": len(reader.list_postgres_clusters()),
                }
            except (ClientError, BotoCoreError) as error:
                error_code = (
                    error.response.get("Error", {}).get("Code", "") if isinstance(error, ClientError) else ""
                )
                if is_automatic_scan and error_code in AUTOMATIC_SCAN_SKIPPABLE_ERROR_CODES:
                    skipped_region_error_codes[region] = error_code
                    logger.info(
                        "environment_import_region_skipped",
                        operation_id=str(context.operation.id),
                        region=region,
                        error_code=error_code,
                        reason="the region is not enabled for this account or rejects its credentials; "
                        "name the region explicitly to make this an error",
                    )
                    continue
                return self._fail_for_aws_error(context, "scan_regions", error, options, f"{region}: ")
        chosen_regions = options.regions or [
            region
            for region, counts in resource_counts_by_region.items()
            if counts["instances"] or counts["clusters"]
        ]
        if not chosen_regions:
            skipped_note = (
                f"; {len(skipped_region_error_codes)} regions rejected the credentials or are not enabled "
                f"({', '.join(sorted(skipped_region_error_codes))})"
                if skipped_region_error_codes
                else ""
            )
            return self._fail(
                context,
                "scan_regions",
                "no_postgres_resources_found",
                "No PostgreSQL instances or clusters were found",
                f"scanned {len(resource_counts_by_region)} regions and found no PostgreSQL instance "
                f"or cluster{skipped_note}",
                "name the regions to import in the Regions field (an environment needs at least one "
                "region), then create the import again",
            )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={
                "region_counts": resource_counts_by_region,
                "chosen_regions": chosen_regions,
                "skipped_regions": sorted(skipped_region_error_codes),
                "skipped_region_error_codes": skipped_region_error_codes,
            },
        )

    def _environment_to_write(self, context: StepContext) -> EnvironmentConfig | None:
        """The environment this import adds, from the options and the outputs of the first two steps."""
        account_output = context.completed_step_output("resolve_account")
        scan_output = context.completed_step_output("scan_regions")
        if account_output is None or scan_output is None:
            return None
        options = self._options(context)
        return EnvironmentConfig(
            slug=options.slug,
            display_name=options.display_name,
            is_production=options.is_production,
            change_management=options.change_management,
            targets=[
                EnvironmentTarget(
                    aws_account_id=account_output["aws_account_id"],
                    aws_region=region,
                    aws_profile_name=None if options.simulated else options.aws_profile_name,
                    simulated=options.simulated,
                )
                for region in scan_output["chosen_regions"]
            ],
        )

    def _configuration_already_written(self, context: StepContext) -> bool:
        """Whether the live configuration holds exactly the environment this import would write."""
        desired_environment = self._environment_to_write(context)
        if desired_environment is None:
            return False
        try:
            return self.config_store.current.environment(desired_environment.slug) == desired_environment
        except KeyError:
            return False

    def _execute_write_configuration(self, context: StepContext) -> StepOutcome:
        environment = self._environment_to_write(context)
        if environment is None:
            return self._fail(
                context,
                "write_configuration",
                "import_steps_missing",
                "The earlier import steps left no result to write",
                "resolve_account or scan_regions has no completed attempt on this operation",
                "create the import again",
            )
        environments_path = Path(context.settings.config_dir) / ENVIRONMENTS_FILE_NAME
        previous_file_text = (
            environments_path.read_bytes().decode("utf-8") if environments_path.exists() else None
        )
        try:
            path = self.config_store.write_environments(
                [*self.config_store.current.environments, environment]
            )
        except EnvironmentsFileChangedError as error:
            return self._fail(
                context,
                "write_configuration",
                error.diagnostic_kind,
                error.title,
                str(error),
                error.fix_suggestion,
            )
        except ValueError as error:
            return self._fail(
                context,
                "write_configuration",
                "configuration_invalid",
                "The new configuration did not validate",
                str(error),
                "fix the import options and create the import again; the configuration file was not changed",
            )
        except RuntimeError as error:
            return self._fail(
                context,
                "write_configuration",
                "configuration_not_writable",
                "This console cannot write its configuration",
                str(error),
                "start the console with a config directory (RDSC_CONFIG_DIR) to be able to add environments",
            )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"path": str(path), "previous_file_text": previous_file_text},
        )

    def _roll_back_configuration(self, context: StepContext, relative_path: Path | None) -> str | None:
        """Put `environments.yaml` back as it was before this import, unstage it and reload the configuration.

        The previous content is the text the write step captured; the live configuration and the control
        database sync then no longer hold the new environment, so the import can be created again.

        Args:
            context: The step context.
            relative_path: The file relative to the repository root, or None when it is outside the
                repository (nothing is staged then).

        Returns:
            None when the rollback worked, else what went wrong.
        """
        previous_file_text = (context.completed_step_output("write_configuration") or {}).get(
            "previous_file_text"
        )
        if previous_file_text is None:
            return "the previous content of the file was not recorded"
        try:
            (Path(context.settings.config_dir) / ENVIRONMENTS_FILE_NAME).write_bytes(
                previous_file_text.encode("utf-8")
            )
            if relative_path is not None:
                unstage = run_git_recorded(
                    context.services.execution_recorder,
                    context.settings.repo_root,
                    ["reset", "-q", "--", relative_path.as_posix()],
                    operation_id=context.operation.id,
                    step_name="commit_configuration",
                    target_key=None,
                )
                if not unstage.succeeded:
                    return f"unstaging the file failed: {unstage.standard_error.strip()}"
            self.config_store.reload()
        except (OSError, ConfigurationError) as error:
            return str(error)
        return None

    def _fail_and_roll_back(
        self,
        context: StepContext,
        relative_path: Path | None,
        kind: str,
        title: str,
        detail: str,
        how_to_fix_the_cause: str,
    ) -> StepOutcome:
        """Fail the commit step after undoing the write, so that the import leaves nothing half done."""
        slug = self._options(context).slug
        environments_file = (relative_path or Path(ENVIRONMENTS_FILE_NAME)).as_posix()
        problem = self._roll_back_configuration(context, relative_path)
        if problem is None:
            logger.info(
                "environment_import_rolled_back",
                operation_id=str(context.operation.id),
                environment_slug=slug,
            )
            fix_suggestion = (
                f"the import was rolled back: {environments_file} is as it was and {slug!r} is not "
                f"configured. {how_to_fix_the_cause}, then create a new import of {slug!r}"
            )
        else:
            logger.error(
                "environment_import_rollback_failed",
                operation_id=str(context.operation.id),
                environment_slug=slug,
                problem=problem,
                fix_suggestion=f"restore {environments_file} with `git checkout -- {environments_file}` and "
                "restart the console",
            )
            fix_suggestion = (
                f"the import could not be rolled back ({problem}): {environments_file} may still contain "
                f"{slug!r}. Restore it with `git checkout -- {environments_file}` (and `git reset -- "
                f"{environments_file}` if it is staged), restart the console so that it reloads its "
                f"configuration, then {how_to_fix_the_cause}"
            )
        return self._fail(context, "commit_configuration", kind, title, detail, fix_suggestion)

    def _execute_commit_configuration(self, context: StepContext) -> StepOutcome:
        options = self._options(context)
        settings = context.settings
        if not self._configuration_already_written(context):
            return self._fail(
                context,
                "commit_configuration",
                "environment_missing_from_configuration",
                "The environment is not in the configuration any more",
                f"{options.slug!r} is not in the live configuration, so there is nothing to commit",
                f"create a new import of {options.slug!r}",
            )
        try:
            relative_path = Path(settings.config_dir).relative_to(settings.repo_root) / ENVIRONMENTS_FILE_NAME
        except ValueError:
            return self._fail_and_roll_back(
                context,
                None,
                "configuration_outside_repository",
                "The configuration directory is not inside the repository",
                f"{settings.config_dir} is not under {settings.repo_root}",
                "set RDSC_CONFIG_DIR to the config directory of the repository checkout",
            )
        try:
            commit_sha = commit_paths(
                context.services.execution_recorder,
                settings.repo_root,
                [relative_path],
                f"Import environment: {options.slug} ({context.operation.created_by_operator_name})",
                settings.git_author_name,
                settings.git_author_email,
                operation_id=context.operation.id,
                step_name="commit_configuration",
                target_key=None,
            )
        except GitCommitError as error:
            return self._fail_and_roll_back(
                context,
                relative_path,
                "commit_failed",
                "The configuration change could not be committed",
                str(error),
                "fix the git problem named in the detail (a failing hook, a locked index, no identity)",
            )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"commit_sha": commit_sha})

    def _execute_reload_configuration(self, context: StepContext) -> StepOutcome:
        configuration = self.config_store.reload()
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED, output={"environment_count": len(configuration.environments)}
        )

    def _execute_queue_refresh(self, context: StepContext) -> StepOutcome:
        options = self._options(context)
        refresh_options: dict[str, Any] = {"target_keys": []}
        refresh_operation = OperationService(context.services.session_factory).create(
            "inventory_refresh",
            options.slug,
            None,
            {"options": refresh_options, "resource_arn": None, "resource_kind": None},
            f"inventory_refresh:import:{context.operation.id}",
            context.operation.created_by_operator_name,
            title=f"Refresh inventory: {options.slug}",
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED, output={"refresh_operation_id": str(refresh_operation.id)}
        )
