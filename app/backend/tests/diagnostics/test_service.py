from rdscommander.control_db.models import DiagnosticReport
from rdscommander.diagnostics.service import record_diagnostic


def test_record_diagnostic_persists_with_fix_suggestion(session_factory) -> None:
    with session_factory() as session:
        report = record_diagnostic(
            session,
            operation_id=None,
            step_name=None,
            severity="warning",
            diagnostic_kind="foreign_owner",
            title="Owned elsewhere",
            detail="tagged by Terraform",
            fix_suggestion="manage it there",
        )
        session.commit()
        assert session.get(DiagnosticReport, report.id).fix_suggestion == "manage it there"
