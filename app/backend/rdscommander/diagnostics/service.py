"""Append diagnostic reports; they are never deleted."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from rdscommander.control_db.models import DiagnosticReport
from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)


def record_diagnostic(
    session: Session,
    *,
    operation_id: uuid.UUID | None,
    step_name: str | None,
    severity: str,
    diagnostic_kind: str,
    title: str,
    detail: str,
    fix_suggestion: str,
) -> DiagnosticReport:
    """Add a diagnostic to `session` (caller commits) and log it."""
    report = DiagnosticReport(
        operation_id=operation_id,
        step_name=step_name,
        severity=severity,
        diagnostic_kind=diagnostic_kind,
        title=title,
        detail=detail,
        fix_suggestion=fix_suggestion,
    )
    session.add(report)
    session.flush()
    log = logger.error if severity == "error" else logger.warning if severity == "warning" else logger.info
    log(
        "diagnostic_recorded",
        diagnostic_kind=diagnostic_kind,
        title=title,
        operation_id=str(operation_id) if operation_id else None,
        fix_suggestion=fix_suggestion,
    )
    return report
