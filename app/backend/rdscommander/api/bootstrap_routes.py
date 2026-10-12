"""`GET /api/v1/bootstrap`: what the frontend needs before anything else."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from rdscommander.config.models import ChangeManagementMode

router = APIRouter(prefix="/api/v1", tags=["bootstrap"])


class EnvironmentTargetSummary(BaseModel):
    """One AWS account and region inside an environment."""

    target_key: str
    aws_account_id: str
    aws_region: str
    aws_profile_name: str | None
    simulated: bool


class EnvironmentSummary(BaseModel):
    """One environment with its targets.

    Attributes:
        slug: The environment's short name used in URLs and target keys.
        display_name: The name the console shows.
        is_production: Whether the console treats the environment as production (marked in the shell).
        requires_approval: Whether changes wait for an approver.
        change_management: `terraform` changes AWS through reviewed Terraform; `direct` changes it through
            the AWS API and records the result in the repository.
        allow_direct_changes_to_foreign_owned: Whether a direct change may touch a resource another tool
            owns (the review then warns who owns it).
        targets: The AWS accounts and regions of the environment.
    """

    slug: str
    display_name: str
    is_production: bool
    requires_approval: bool
    change_management: ChangeManagementMode
    allow_direct_changes_to_foreign_owned: bool
    targets: list[EnvironmentTargetSummary]


class BootstrapResponse(BaseModel):
    """Identity and configuration summary.

    Attributes:
        csrf_token: Token the browser sends with every change.
        operator_name: Who the console acts as.
        theme_default: The theme the shell starts in before the operator picks one.
        company_display_name: The company name for the shell.
        company_slug: The company's short name.
        engine_version: The running console version.
        environments: Every configured environment.
    """

    csrf_token: str
    operator_name: str
    theme_default: Literal["system", "light", "dark"]
    company_display_name: str
    company_slug: str
    engine_version: str
    environments: list[EnvironmentSummary]


@router.get("/bootstrap", response_model=BootstrapResponse)
def read_bootstrap(request: Request) -> BootstrapResponse:
    """Return the CSRF token, operator identity, and configured environments."""
    from rdscommander import __version__

    settings = request.app.state.settings
    console_config = request.app.state.config_store.current
    environments: list[EnvironmentSummary] = []
    if console_config is not None:
        for environment in console_config.environments:
            environments.append(
                EnvironmentSummary(
                    slug=environment.slug,
                    display_name=environment.display_name,
                    is_production=environment.is_production,
                    requires_approval=environment.requires_approval,
                    change_management=environment.change_management,
                    allow_direct_changes_to_foreign_owned=environment.allow_direct_changes_to_foreign_owned,
                    targets=[
                        EnvironmentTargetSummary(
                            target_key=f"{environment.slug}/{t.aws_account_id}/{t.aws_region}",
                            aws_account_id=t.aws_account_id,
                            aws_region=t.aws_region,
                            aws_profile_name=t.aws_profile_name,
                            simulated=t.simulated,
                        )
                        for t in environment.targets
                    ],
                )
            )
    company = getattr(console_config, "company", None)
    return BootstrapResponse(
        csrf_token=request.app.state.csrf_token,
        operator_name=getattr(settings, "operator_name", "unknown-operator"),
        theme_default="system",
        company_display_name=getattr(company, "company_display_name", ""),
        company_slug=getattr(company, "company_slug", ""),
        engine_version=__version__,
        environments=environments,
    )
