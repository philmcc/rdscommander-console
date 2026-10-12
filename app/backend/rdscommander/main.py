"""Application factory and process entry point."""

from __future__ import annotations

import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from rdscommander.api.attention_routes import router as attention_router
from rdscommander.api.bootstrap_routes import router as bootstrap_router
from rdscommander.api.catalogue_routes import router as catalogue_router
from rdscommander.api.csrf import LocalOriginMiddleware, generate_csrf_token
from rdscommander.api.environment_routes import router as environment_router
from rdscommander.api.history_routes import router as history_router
from rdscommander.api.lifecycle_routes import router as lifecycle_router
from rdscommander.api.operation_routes import router as operation_router
from rdscommander.api.parameter_routes import router as parameter_router
from rdscommander.api.resource_routes import router as resource_router
from rdscommander.api.simulated_routes import router as simulated_router
from rdscommander.api.simulated_routes import seed_all_simulated_targets
from rdscommander.api.terraform_routes import router as terraform_router
from rdscommander.api.validation_problems import request_validation_problem_response
from rdscommander.aws.sessions import AwsSessionFactory
from rdscommander.catalogue.catalogue import validate_catalogue
from rdscommander.config.models import ConsoleConfig
from rdscommander.config.settings import OperatorSettings
from rdscommander.config.store import ConfigStore, ConfigStoreProtocol, StaticConfigStore
from rdscommander.control_db.session import (
    ControlDbSessionFactory,
    create_control_db_engine,
    make_session_factory,
    run_migrations,
)
from rdscommander.drift.detect_executor import DriftDetectExecutor
from rdscommander.drift.resolve_executor import DriftResolveExecutor
from rdscommander.environments.import_executor import EnvironmentImportExecutor
from rdscommander.execution_records.service import ExecutionRecorder
from rdscommander.export_openapi import APPLICATION_TITLE, APPLICATION_VERSION
from rdscommander.inventory.refresh_executor import InventoryRefreshExecutor
from rdscommander.lifecycle.actions import (
    ApplyPendingMaintenanceExecutor,
    ClusterFailoverExecutor,
    DeleteSnapshotExecutor,
    PromoteReplicaExecutor,
    RebootInstanceExecutor,
    TakeSnapshotExecutor,
)
from rdscommander.lifecycle.creates import (
    ClusterCreateExecutor,
    ClusterReaderCreateExecutor,
    InstanceCreateExecutor,
    ReadReplicaCreateExecutor,
)
from rdscommander.lifecycle.removals import DecommissionExecutor, RemoveReaderExecutor
from rdscommander.lifecycle.restores import PointInTimeRestoreExecutor, SnapshotRestoreExecutor
from rdscommander.lifecycle.settings_changes import (
    BackupSettingsChangeExecutor,
    DeletionProtectionChangeExecutor,
    InstanceClassChangeExecutor,
    MaintenanceWindowChangeExecutor,
    MonitoringChangeExecutor,
    MultiAzChangeExecutor,
    ServerlessCapacityChangeExecutor,
    StorageChangeExecutor,
    TagsChangeExecutor,
)
from rdscommander.logging_setup import configure_structured_logging, get_logger
from rdscommander.operations.executor import StepExecutor
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.scheduler import SCHEDULER_OPERATOR_NAME, OperationScheduler
from rdscommander.parameters.executors import (
    ParameterGroupAttachExecutor,
    ParameterGroupCreateExecutor,
    ParameterGroupDeleteExecutor,
    ParameterGroupEditExecutor,
    UpgradeGroupBuilderExecutor,
)
from rdscommander.services import ServiceBundle
from rdscommander.terraform.adoption_executor import TerraformAdoptionExecutor
from rdscommander.terraform.runner import TerraformRunner
from rdscommander.upgrades.cleanup import BlueGreenCleanupExecutor
from rdscommander.upgrades.major import MajorUpgradeExecutor
from rdscommander.upgrades.minor import AutoMinorUpgradeChangeExecutor, MinorUpgradeExecutor

logger = get_logger(__name__)


def frontend_dist_directory() -> Path:
    """Directory holding the built frontend (`frontend/dist`); it may not exist before the first build."""
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _register_frontend_fallback(app: FastAPI, frontend_dist: Path) -> None:
    """Serve the built frontend, falling back to `index.html` so every page URL can be reloaded.

    Registered after every router so it never shadows an API route. Paths under `api/` that no
    router matched stay 404 instead of returning the page, a path that would resolve outside
    `frontend_dist` is served the index page rather than the file, and a path the filesystem cannot
    represent (for example an embedded null byte) answers 404.

    Args:
        app: The application to register the assets mount and the catch-all route on.
        frontend_dist: The built frontend directory; nothing is registered when it does not exist.
    """
    if not frontend_dist.is_dir():
        return
    frontend_root = frontend_dist.resolve()
    index_file = frontend_root / "index.html"
    assets_directory = frontend_root / "assets"
    if assets_directory.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_directory), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="no such API route")
        try:
            candidate = (frontend_root / full_path).resolve()
        except (OSError, ValueError) as path_error:
            # e.g. an embedded null byte: no such file can exist, and this is not a page URL either
            logger.warning(
                "frontend_path_rejected",
                full_path=full_path,
                error=str(path_error),
                fix_suggestion="clients should not request paths with control characters",
            )
            raise HTTPException(status_code=404, detail="no such file") from path_error
        if full_path and candidate.is_file() and candidate.is_relative_to(frontend_root):
            return FileResponse(candidate)
        return FileResponse(index_file)


def build_services(settings: OperatorSettings, session_factory: ControlDbSessionFactory) -> ServiceBundle:
    """Construct the shared services."""
    recorder = ExecutionRecorder(session_factory)
    return ServiceBundle(
        execution_recorder=recorder,
        aws_sessions=AwsSessionFactory(settings, recorder),
        terraform_runner=TerraformRunner(settings.terraform_binary, recorder),
        session_factory=session_factory,
    )


def runner_identity_for(settings: OperatorSettings) -> str:
    """The identity this console's runner leases operations and owns checkout files under.

    It is `<runner_identity_name or hostname>:<operator_name>`. The checkout guard and the cleanup of a
    rejected operation compare it with the identity that rendered the files, so it has to survive a
    re-created container: `compose.yaml` fixes the container hostname, and `RDSC_RUNNER_IDENTITY_NAME`
    overrides the hostname altogether.

    Example:
        >>> runner_identity_for(OperatorSettings(operator_name="phil", runner_identity_name="console"))
        'console:phil'
    """
    return f"{settings.runner_identity_name or socket.gethostname()}:{settings.operator_name}"


def build_executors(services: ServiceBundle, config_store: ConfigStoreProtocol) -> dict[str, StepExecutor]:
    """All executors keyed by operation kind; each reads the live configuration from `config_store`."""
    return {
        InventoryRefreshExecutor.operation_kind: InventoryRefreshExecutor(config_store),
        TerraformAdoptionExecutor.operation_kind: TerraformAdoptionExecutor(config_store),
        DriftDetectExecutor.operation_kind: DriftDetectExecutor(config_store),
        DriftResolveExecutor.operation_kind: DriftResolveExecutor(config_store),
        EnvironmentImportExecutor.operation_kind: EnvironmentImportExecutor(config_store),
        ParameterGroupCreateExecutor.operation_kind: ParameterGroupCreateExecutor(config_store),
        ParameterGroupEditExecutor.operation_kind: ParameterGroupEditExecutor(config_store),
        ParameterGroupAttachExecutor.operation_kind: ParameterGroupAttachExecutor(config_store),
        ParameterGroupDeleteExecutor.operation_kind: ParameterGroupDeleteExecutor(config_store),
        UpgradeGroupBuilderExecutor.operation_kind: UpgradeGroupBuilderExecutor(config_store),
        RebootInstanceExecutor.operation_kind: RebootInstanceExecutor(config_store),
        InstanceClassChangeExecutor.operation_kind: InstanceClassChangeExecutor(config_store),
        StorageChangeExecutor.operation_kind: StorageChangeExecutor(config_store),
        MultiAzChangeExecutor.operation_kind: MultiAzChangeExecutor(config_store),
        BackupSettingsChangeExecutor.operation_kind: BackupSettingsChangeExecutor(config_store),
        MaintenanceWindowChangeExecutor.operation_kind: MaintenanceWindowChangeExecutor(config_store),
        MonitoringChangeExecutor.operation_kind: MonitoringChangeExecutor(config_store),
        DeletionProtectionChangeExecutor.operation_kind: DeletionProtectionChangeExecutor(config_store),
        ServerlessCapacityChangeExecutor.operation_kind: ServerlessCapacityChangeExecutor(config_store),
        TagsChangeExecutor.operation_kind: TagsChangeExecutor(config_store),
        ClusterFailoverExecutor.operation_kind: ClusterFailoverExecutor(config_store),
        ApplyPendingMaintenanceExecutor.operation_kind: ApplyPendingMaintenanceExecutor(config_store),
        TakeSnapshotExecutor.operation_kind: TakeSnapshotExecutor(config_store),
        DeleteSnapshotExecutor.operation_kind: DeleteSnapshotExecutor(config_store),
        PromoteReplicaExecutor.operation_kind: PromoteReplicaExecutor(config_store),
        RemoveReaderExecutor.operation_kind: RemoveReaderExecutor(config_store),
        DecommissionExecutor.operation_kind: DecommissionExecutor(config_store),
        ReadReplicaCreateExecutor.operation_kind: ReadReplicaCreateExecutor(config_store),
        ClusterReaderCreateExecutor.operation_kind: ClusterReaderCreateExecutor(config_store),
        SnapshotRestoreExecutor.operation_kind: SnapshotRestoreExecutor(config_store),
        PointInTimeRestoreExecutor.operation_kind: PointInTimeRestoreExecutor(config_store),
        InstanceCreateExecutor.operation_kind: InstanceCreateExecutor(config_store),
        ClusterCreateExecutor.operation_kind: ClusterCreateExecutor(config_store),
        AutoMinorUpgradeChangeExecutor.operation_kind: AutoMinorUpgradeChangeExecutor(config_store),
        MinorUpgradeExecutor.operation_kind: MinorUpgradeExecutor(config_store),
        MajorUpgradeExecutor.operation_kind: MajorUpgradeExecutor(config_store),
        BlueGreenCleanupExecutor.operation_kind: BlueGreenCleanupExecutor(config_store),
    }


def create_application(
    settings: OperatorSettings | None = None,
    console_config: ConsoleConfig | None = None,
    session_factory: ControlDbSessionFactory | None = None,
    start_runner: bool = False,
) -> FastAPI:
    """Build the FastAPI app with services, the config store, the runner and the scheduler on `app.state`.

    Args:
        settings: Operator settings; read from the environment when omitted.
        console_config: A ready configuration (tests); when omitted the config directory is loaded
            through a reloadable `ConfigStore`.
        session_factory: Control database sessions; created and migrated from settings when omitted.
        start_runner: Whether the lifespan starts the background threads: the operation runner (when
            `runner_enabled`) and the scheduler (when `scheduler_enabled`).
    """
    settings = settings or OperatorSettings()
    configure_structured_logging(settings.log_level)
    if session_factory is None:
        run_migrations(settings.control_database_url.get_secret_value())
        session_factory = make_session_factory(
            create_control_db_engine(settings.control_database_url.get_secret_value())
        )
    config_store: ConfigStoreProtocol
    if console_config is None:
        config_store = ConfigStore(settings.config_dir, session_factory)
    else:
        config_store = StaticConfigStore(console_config, session_factory)
        config_store.reload()
    services = build_services(settings, session_factory)
    executors = build_executors(services, config_store)
    validate_catalogue(executors)
    runner = OperationRunner(
        session_factory,
        executors,
        runner_identity_for(settings),
        settings,
        config_store,
        services,
        lease_seconds=settings.lease_seconds,
    )
    scheduler = OperationScheduler(
        session_factory,
        config_store,
        SCHEDULER_OPERATOR_NAME,
        settings.scheduler_interval_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if config_store.current.has_simulated_targets:
            _seed_simulated_targets_with_retries(settings, config_store.current, services)
        if start_runner and settings.runner_enabled:
            runner.start()
        if start_runner and settings.scheduler_enabled:
            scheduler.start()
        yield
        scheduler.stop()
        runner.stop()

    app = FastAPI(title=APPLICATION_TITLE, version=APPLICATION_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.config_store = config_store
    app.state.session_factory = session_factory
    app.state.services = services
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.csrf_token = generate_csrf_token()
    app.add_middleware(LocalOriginMiddleware)
    app.add_exception_handler(RequestValidationError, request_validation_problem_response)
    app.include_router(bootstrap_router)
    app.include_router(environment_router)
    app.include_router(simulated_router)
    app.include_router(operation_router)
    app.include_router(terraform_router)
    app.include_router(history_router)
    app.include_router(catalogue_router)
    app.include_router(resource_router)
    app.include_router(attention_router)
    app.include_router(parameter_router)
    app.include_router(lifecycle_router)
    _register_frontend_fallback(app, frontend_dist_directory())
    logger.info(
        "application_created",
        start_runner=start_runner,
        environment_count=len(config_store.current.environments),
    )
    return app


def _seed_simulated_targets_with_retries(
    settings: OperatorSettings, console_config: ConsoleConfig, services: ServiceBundle, attempts: int = 15
) -> None:
    """Seed simulated targets at startup; the moto container may still be starting, so retry briefly."""
    for attempt in range(1, attempts + 1):
        try:
            seed_all_simulated_targets(settings, console_config, services)
            return
        except Exception as error:  # noqa: BLE001 - startup must not die because the simulator is slow
            if attempt == attempts:
                logger.error(
                    "simulated_seed_failed",
                    error=str(error),
                    fix_suggestion="check the simulated-aws container is healthy, then use the reset route",
                )
                return
            logger.warning(
                "simulated_seed_retry",
                attempt=attempt,
                error=str(error),
                fix_suggestion="wait for the simulated-aws container to become healthy",
            )
            time.sleep(2)


def run() -> None:
    """Entry point used by the container."""
    import uvicorn

    settings = OperatorSettings()
    uvicorn.run(
        lambda: create_application(settings=settings, start_runner=True),
        factory=True,
        host="0.0.0.0" if settings.listen_on_all_interfaces_inside_container else "127.0.0.1",  # noqa: S104
        port=settings.http_port,
        log_config=None,
    )


if __name__ == "__main__":
    run()
