"""Mirror environments and targets from config into the control database at startup and on reload."""

from __future__ import annotations

from rdscommander.config.models import ConsoleConfig
from rdscommander.control_db.models import Environment, EnvironmentTargetRecord, utc_now
from rdscommander.control_db.session import ControlDbSessionFactory


def sync_configuration_to_control_db(
    session_factory: ControlDbSessionFactory, console_config: ConsoleConfig
) -> None:
    """Upsert every environment and target; nothing is deleted so history keeps its references."""
    with session_factory() as session:
        for environment in console_config.environments:
            row = session.get(Environment, environment.slug) or Environment(
                environment_slug=environment.slug,
                display_name=environment.display_name,
                requires_approval=environment.requires_approval,
            )
            row.display_name = environment.display_name
            row.requires_approval = environment.requires_approval
            row.change_management = environment.change_management
            row.allow_direct_changes_to_foreign_owned = environment.allow_direct_changes_to_foreign_owned
            row.inventory_refresh_minutes = environment.inventory_refresh_minutes
            row.drift_check_hours = environment.drift_check_hours
            row.synced_at_utc = utc_now()
            session.merge(row)
        for target in console_config.all_targets():
            row = session.get(EnvironmentTargetRecord, target.target_key) or EnvironmentTargetRecord(
                target_key=target.target_key,
                environment_slug=target.environment_slug,
                aws_account_id=target.aws_account_id,
                aws_region=target.aws_region,
                aws_profile_name=target.aws_profile_name,
                is_simulated=target.simulated,
            )
            row.aws_profile_name = target.aws_profile_name
            row.is_simulated = target.simulated
            session.merge(row)
        session.commit()
