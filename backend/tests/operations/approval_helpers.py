"""Approve an operation as the review page does: for the stage it shows, with the identifier typed."""

from __future__ import annotations

import uuid

from rdscommander.control_db.models import Operation
from rdscommander.operations.service import APPROVAL_STAGE_PLAN_KEY, OperationService


def approve_as_reviewed(service: OperationService, operation_id: uuid.UUID, operator_name: str) -> Operation:
    """Approve the stage the operation waits at, typing the identifier it asks for (if any).

    Example:
        >>> approve_as_reviewed(service, operation.id, "sam").state  # doctest: +SKIP
        'QUEUED'
    """
    waiting = service.get(operation_id)
    return service.approve(
        operation_id,
        operator_name,
        approval_stage=waiting.plan.get(APPROVAL_STAGE_PLAN_KEY),
        confirmation_text=waiting.approval_required_text,
    )
