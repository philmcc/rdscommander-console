"""`inventory_refresh`: one idempotent step per target of the environment."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select

from rdscommander.aws.rds_reader import RdsReader
from rdscommander.config.models import ResolvedTarget
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import InventorySnapshot, Operation
from rdscommander.history.change_events import compute_change_events
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.inventory.retention import prune_execution_records, prune_inventory_snapshots
from rdscommander.inventory.snapshot_writer import discover_estate, write_snapshot
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome

logger = get_logger(__name__)

STEP_PREFIX = "refresh:"


def _refresh_target(
    context: StepContext, target: ResolvedTarget, step_name: str
) -> tuple[InventorySnapshot, int]:
    """Discover the estate of `target`, write the snapshot and its change events, commit, then prune.

    The target's snapshots beyond the retention and old execution records are pruned after the commit
    (see `inventory.retention`).
    """
    client = context.services.aws_sessions.client(
        target, "rds", operation_id=context.operation.id, step_name=step_name
    )
    discovered = discover_estate(RdsReader(client))
    previous_snapshot = latest_snapshot_for_target(context.session, target.target_key)
    snapshot = write_snapshot(context.session, target, context.operation.id, discovered)
    change_events = compute_change_events(
        context.session, target, previous_snapshot.id if previous_snapshot else None, snapshot.id
    )
    context.session.commit()
    prune_inventory_snapshots(
        context.session, target.target_key, context.settings.inventory_snapshot_retention_per_target
    )
    prune_execution_records(context.session, context.settings.execution_record_retention_days)
    return snapshot, len(change_events)


def refresh_target_now(
    context: StepContext, target: ResolvedTarget, step_name: str | None = None
) -> InventorySnapshot:
    """Take a new inventory snapshot of one target inside the operation that is running.

    This is the body of the refresh step, factored out so that an operation that changed AWS directly can
    look again straight away. The snapshot and the change events derived from it are committed together;
    the events are attributed to the operation that explains them, which is this one while it is running.

    Args:
        context: The running step's context.
        target: The target to read.
        step_name: Step the AWS calls are recorded under; defaults to the refresh step's own name.

    Returns:
        The snapshot that was written.

    Example:
        >>> snapshot = refresh_target_now(context, context.target, "refresh_resource")  # doctest: +SKIP
        >>> snapshot.target_key  # doctest: +SKIP
        'sandbox/000000000000/eu-west-2'
    """
    snapshot, _ = _refresh_target(context, target, step_name or f"{STEP_PREFIX}{target.target_key}")
    return snapshot


def refresh_after_terraform_commit(
    context: StepContext,
    refresh: Callable[[StepContext, ResolvedTarget, str | None], InventorySnapshot],
    step_name: str = "refresh_resource",
) -> StepOutcome:
    """The closing step of a committed Terraform-mode change: a new snapshot, and never a failure.

    The change is applied and committed, so a refresh that fails leaves it as it is: the step completes,
    says why in its output and detail, and logs a warning. The next change on the target waits until
    someone refreshes (the change strategy's `inventory_behind_change`).

    Args:
        context: The running step's context.
        refresh: Takes the snapshot (`refresh_target_now`; a parameter so callers and tests can replace it).
        step_name: Step the AWS calls are recorded under.

    Returns:
        Completed, with `{"refreshed": True, "snapshot_id": ...}` or `{"refreshed": False, "error": ...}`.

    Example:
        >>> refresh_after_terraform_commit(context, refresh_target_now).output["refreshed"]  # doctest: +SKIP
        True
    """
    try:
        snapshot = refresh(context, context.target, step_name)
    except Exception as error:  # noqa: BLE001 - the change is committed; a missed refresh must not fail it
        context.session.rollback()
        environment_slug = context.target.environment_slug
        fix_suggestion = f"run Refresh inventory on the {environment_slug} environment to see the change"
        logger.warning(
            "terraform_change_refresh_failed",
            operation_id=str(context.operation.id),
            target_key=context.target.target_key,
            error=f"{type(error).__name__}: {error}",
            fix_suggestion=fix_suggestion,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            detail=f"the change is committed but the inventory was not refreshed; {fix_suggestion}",
            output={"refreshed": False, "error": f"{type(error).__name__}: {error}"},
        )
    return StepOutcome(
        status=StepOutcomeStatus.COMPLETED, output={"refreshed": True, "snapshot_id": snapshot.id}
    )


class InventoryRefreshExecutor:
    """Refreshes every target in the operation's environment."""

    operation_kind = "inventory_refresh"

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store` each time steps are planned."""
        self.config_store = config_store

    def step_names(self, operation: Operation) -> list[str]:
        """`refresh:<target_key>` for each target to refresh.

        The plan's `options.target_keys` narrows the refresh to those targets; keys that are not targets
        of the operation's environment are ignored. An empty or absent list refreshes every target.

        Args:
            operation: The inventory refresh operation.

        Returns:
            One step name per target, in the environment's target order.

        Raises:
            ValueError: The operation has no environment, so there is nothing to refresh.

        Example:
            >>> executor.step_names(operation)  # plan {"options": {"target_keys": ["sandbox/0/eu-west-2"]}}
            ['refresh:sandbox/0/eu-west-2']
        """
        if operation.environment_slug is None:
            logger.error(
                "inventory_refresh_missing_environment",
                operation_id=str(operation.id),
                fix_suggestion="create the inventory refresh with an environment_slug",
            )
            raise ValueError(
                f"inventory refresh operation {operation.id} has no environment_slug; "
                "create it with an environment to refresh"
            )
        targets = self.config_store.current.targets_for_environment(operation.environment_slug)
        plan_options = (operation.plan or {}).get("options") or {}
        requested_target_keys = plan_options.get("target_keys") or []
        if requested_target_keys:
            targets = [t for t in targets if t.target_key in requested_target_keys]
        return [f"{STEP_PREFIX}{t.target_key}" for t in targets]

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """Satisfied when this operation already wrote a snapshot for the target."""
        target_key = step_name.removeprefix(STEP_PREFIX)
        existing = context.session.execute(
            select(InventorySnapshot.id).where(
                InventorySnapshot.operation_id == context.operation.id,
                InventorySnapshot.target_key == target_key,
            )
        ).first()
        return StepObservation(state=ObservationState.SATISFIED if existing else ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Discover and write one snapshot, then record what changed since the target's previous one.

        The snapshot and its change events are committed together, so a target never has a snapshot
        whose changes were not derived. The first snapshot of a target produces no events.

        Args:
            step_name: `refresh:<target_key>`.
            context: The step context.

        Returns:
            A completed outcome carrying the snapshot id, resource counts and `change_event_count`.
        """
        target = context.console_config.target(step_name.removeprefix(STEP_PREFIX))
        snapshot, change_event_count = _refresh_target(context, target, step_name)
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={
                "snapshot_id": snapshot.id,
                "instance_count": snapshot.instance_count,
                "cluster_count": snapshot.cluster_count,
                "change_event_count": change_event_count,
            },
        )
