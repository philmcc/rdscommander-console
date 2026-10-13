"""Workspaces, adoptions, and plan viewing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from rdscommander.api.catalogue_routes import OperationCreateRequest, create_operation_response
from rdscommander.api.schemas import OperationCreatedResponse
from rdscommander.control_db.models import ResourceAdoption, TerraformRun, TerraformWorkspace

router = APIRouter(prefix="/api/v1/terraform", tags=["terraform"])


class CreateAdoptionRequest(BaseModel):
    """Body of `POST /terraform/adoptions`."""

    target_key: str
    resource_arns: list[str] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class AdoptionSummary(BaseModel):
    """Adopted resource row."""

    resource_arn: str
    resource_identifier: str
    resource_type: str
    terraform_address: str
    adoption_status: str
    adopted_at_utc: datetime | None


class WorkspaceSummary(BaseModel):
    """Workspace row."""

    id: int
    target_key: str
    workspace_relative_dir: str
    state_bucket_name: str
    state_key: str
    last_applied_commit_sha: str | None
    last_plan_summary: dict[str, Any] | None
    last_applied_at_utc: datetime | None
    adoptions: list[AdoptionSummary]


class WorkspaceList(BaseModel):
    """List response."""

    workspaces: list[WorkspaceSummary]


class PlanView(BaseModel):
    """Summary plus redacted JSON for the viewer."""

    run_id: int
    run_kind: str
    plan_summary: dict[str, Any] | None
    plan_json: dict[str, Any] | None


class AdoptionProblems(BaseModel):
    """422 body when the selection cannot be adopted."""

    detail: str
    problems: list[str]


@router.get("/workspaces", response_model=WorkspaceList)
def list_workspaces(request: Request) -> WorkspaceList:
    """Every workspace with its adoptions."""
    with request.app.state.session_factory() as session:
        rows = (
            session.execute(select(TerraformWorkspace).order_by(TerraformWorkspace.target_key))
            .scalars()
            .all()
        )
        result = []
        for workspace in rows:
            adoptions = session.execute(
                select(ResourceAdoption)
                .where(ResourceAdoption.workspace_id == workspace.id)
                .order_by(ResourceAdoption.terraform_address)
            ).scalars()
            columns = {c: getattr(workspace, c) for c in WorkspaceSummary.model_fields if c != "adoptions"}
            result.append(
                WorkspaceSummary(
                    **columns,
                    adoptions=[AdoptionSummary.model_validate(a, from_attributes=True) for a in adoptions],
                )
            )
        return WorkspaceList(workspaces=result)


@router.post(
    "/adoptions",
    response_model=OperationCreatedResponse,
    status_code=202,
    responses={422: {"model": AdoptionProblems, "description": "selection problems"}},
)
def create_adoption(request: Request, body: CreateAdoptionRequest) -> OperationCreatedResponse | JSONResponse:
    """Validate the selection and queue a `terraform_adoption` operation through the operation catalogue."""
    console_config = request.app.state.config_store.current
    try:
        target = console_config.target(body.target_key)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return create_operation_response(
        request.app.state,
        OperationCreateRequest(
            kind="terraform_adoption",
            environment_slug=target.environment_slug,
            options={"target_key": body.target_key, "resource_arns": body.resource_arns},
            idempotency_key=body.idempotency_key,
        ),
    )


@router.get("/runs/{run_id}/plan", response_model=PlanView)
def read_plan(request: Request, run_id: int) -> PlanView:
    """Plan JSON (redacted) and summary of one run."""
    with request.app.state.session_factory() as session:
        run = session.get(TerraformRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"terraform run {run_id} not found")
        return PlanView(
            run_id=run.id, run_kind=run.run_kind, plan_summary=run.plan_summary, plan_json=run.plan_json
        )
