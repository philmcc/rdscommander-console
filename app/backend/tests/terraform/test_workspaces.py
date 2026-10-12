from pathlib import Path

from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.terraform.workspaces import ensure_workspace_record, workspace_directory


def test_ensure_workspace_record_is_idempotent_and_computes_paths(session_factory, console_config) -> None:
    sync_configuration_to_control_db(session_factory, console_config)
    target = console_config.target("sandbox/000000000000/eu-west-2")
    with session_factory() as session:
        first = ensure_workspace_record(session, target, console_config)
        session.commit()
        second = ensure_workspace_record(session, target, console_config)
        assert first.id == second.id
        assert second.state_bucket_name == "testco-rdscommander-state-000000000000-eu-west-2"
        assert second.state_key == "rdscommander/sandbox/000000000000/eu-west-2/terraform.tfstate"
        assert workspace_directory(Path("/workspace"), second) == Path(
            "/workspace/terraform/sandbox/000000000000/eu-west-2"
        )
