"""Open record drift on the resources a change is about to touch.

A change renders or records the resources it touches from the latest inventory. When such a resource has
drifted from the record (an open `record` finding), the change would commit the outside value as part of an
approved change that nobody reviewed, and Detect drift would then close the finding as if it had been
resolved. So a change is refused while a resource it touches has open record drift: the operator resolves
the drift first (accept what AWS has into the record, or restore AWS to the record).

Example:
    >>> findings = open_record_drift_findings(  # doctest: +SKIP
    ...     session, "sandbox/000000000000/eu-west-2", addresses={"aws_db_parameter_group.demo"}
    ... )
    >>> resolve_drift_first(findings)  # doctest: +SKIP
    'Resolve drift on demo first'
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.control_db.models import DriftFinding

RECORD_FINDING_KIND = "record"
OPEN_DRIFT_DIAGNOSTIC_KIND = "open_drift_on_resource"


def open_record_drift_findings(
    session: Session,
    target_key: str,
    *,
    addresses: Collection[str] = (),
    resource_arns: Collection[str] = (),
) -> list[DriftFinding]:
    """The open record-drift findings of `target_key` about the given resources.

    A finding is about a resource when it is keyed by the resource's ARN or Terraform address, or when one of
    its attributes names the address.

    Args:
        session: Control database session.
        target_key: The target whose findings are searched.
        addresses: Terraform addresses of the resources.
        resource_arns: ARNs of the resources.

    Returns:
        The matching open findings, oldest first.

    Example:
        >>> open_record_drift_findings(session, target_key, resource_arns={group_arn})  # doctest: +SKIP
        [<DriftFinding ...>]
    """
    keys = {*addresses, *resource_arns}
    if not keys:
        return []
    findings = session.execute(
        select(DriftFinding)
        .where(
            DriftFinding.target_key == target_key,
            DriftFinding.finding_kind == RECORD_FINDING_KIND,
            DriftFinding.resolved_at_utc.is_(None),
        )
        .order_by(DriftFinding.detected_at_utc, DriftFinding.id)
    ).scalars()
    return [
        finding
        for finding in findings
        if finding.resource_arn in keys
        or any(isinstance(a, dict) and a.get("address") in addresses for a in finding.attributes)
    ]


def drift_subject(finding: DriftFinding) -> str:
    """The resource a finding is about, as the operator knows it: its AWS identifier, or its address.

    Example:
        >>> drift_subject(DriftFinding(resource_arn="arn:aws:rds:eu-west-2:0:pg:demo"))
        'demo'
    """
    if finding.resource_arn.startswith("arn:"):
        return finding.resource_arn.rsplit(":", 1)[-1]
    return finding.resource_arn


def drifted_attributes(finding: DriftFinding) -> list[str]:
    """The names of the attributes that drifted, in the order detection listed them."""
    return [str(a.get("attribute")) for a in finding.attributes if isinstance(a, dict) and a.get("attribute")]


def describe_open_drift(findings: Sequence[DriftFinding]) -> str:
    """One clause per finding: `<subject> has open drift on <attributes> (detected <time>)`."""
    return "; ".join(
        f"{drift_subject(finding)} has open drift on {', '.join(drifted_attributes(finding)) or 'presence'}"
        + (f" (detected {finding.detected_at_utc.isoformat()})" if finding.detected_at_utc else "")
        for finding in findings
    )


def resolve_drift_first(findings: Sequence[DriftFinding]) -> str:
    """`Resolve drift on <subject> first`, naming every drifted resource once.

    Example:
        >>> resolve_drift_first(findings)  # doctest: +SKIP
        'Resolve drift on demo-postgres16-tuned first'
    """
    subjects = list(dict.fromkeys(drift_subject(finding) for finding in findings))
    return f"Resolve drift on {' and '.join(subjects)} first"
