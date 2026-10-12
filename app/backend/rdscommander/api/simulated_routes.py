"""Reset the simulated account for an environment."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from rdscommander.config.models import ConsoleConfig
from rdscommander.config.settings import OperatorSettings
from rdscommander.services import ServiceBundle
from rdscommander.simulated.estate import load_simulated_estate
from rdscommander.simulated.seeder import SeedReport, seed_simulated_estate

router = APIRouter(prefix="/api/v1", tags=["simulated"])


class SeedReportResponse(BaseModel):
    """Seed report per target."""

    reports: dict[str, dict[str, list[str]]]


def seed_all_simulated_targets(
    settings: OperatorSettings,
    console_config: ConsoleConfig,
    services: ServiceBundle,
    environment_slug: str | None = None,
) -> dict[str, SeedReport]:
    """Seed every simulated target, optionally only those in one environment."""
    estate = load_simulated_estate(settings.config_dir)
    reports: dict[str, SeedReport] = {}
    for target in console_config.all_targets():
        if not target.simulated or (environment_slug and target.environment_slug != environment_slug):
            continue
        rds = services.aws_sessions.client(target, "rds", step_name="simulated_seed")
        ec2 = services.aws_sessions.client(target, "ec2", step_name="simulated_seed")
        reports[target.target_key] = seed_simulated_estate(rds, ec2, estate)
    return reports


@router.post("/environments/{slug}/simulated/reset", response_model=SeedReportResponse)
def reset_simulated_environment(request: Request, slug: str) -> SeedReportResponse:
    """Seed the simulated targets of `slug`; 422 when the environment has none."""
    console_config = request.app.state.config_store.current
    try:
        environment = console_config.environment(slug)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not any(t.simulated for t in environment.targets):
        raise HTTPException(status_code=422, detail=f"environment {slug} has no simulated targets")
    reports = seed_all_simulated_targets(
        request.app.state.settings, console_config, request.app.state.services, environment_slug=slug
    )
    return SeedReportResponse(
        reports={k: {"created": v.created, "existing": v.existing} for k, v in reports.items()}
    )
