from pathlib import Path

import pytest

from rdscommander.config.models import EnvironmentConfig
from rdscommander.config.store import ConfigStore
from rdscommander.control_db.models import Environment

EXAMPLE = Path(__file__).resolve().parents[3] / "config.example"


def _copy_example(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("company.yaml", "environments.yaml", "terraform.yaml"):
        (config_dir / name).write_text((EXAMPLE / name).read_text())


def test_store_loads_defaults_for_mode_fields(session_factory, tmp_path: Path) -> None:
    _copy_example(tmp_path / "config")
    store = ConfigStore(tmp_path / "config", session_factory)
    sandbox = store.current.environment("sandbox")
    assert sandbox.change_management == "terraform"
    assert sandbox.allow_direct_changes_to_foreign_owned is False
    assert sandbox.inventory_refresh_minutes == 15
    assert sandbox.drift_check_hours == 24
    assert store.current.target("sandbox/000000000000/eu-west-2").is_direct is False


def test_write_environments_rewrites_the_file_reloads_and_syncs(session_factory, tmp_path: Path) -> None:
    _copy_example(tmp_path / "config")
    store = ConfigStore(tmp_path / "config", session_factory)
    production = EnvironmentConfig.model_validate(
        {
            "slug": "production",
            "display_name": "Production",
            "change_management": "direct",
            "targets": [
                {"aws_account_id": "123456789012", "aws_region": "eu-west-2", "aws_profile_name": "acme-prod"}
            ],
        }
    )
    written = store.write_environments([*store.current.environments, production])
    assert written == tmp_path / "config" / "environments.yaml"
    text = written.read_text()
    assert text.startswith((EXAMPLE / "environments.yaml").read_text()), "the file is edited, not regenerated"
    assert "change_management: direct" in text
    assert "aws_profile_name: null" not in text
    assert [e.slug for e in store.current.environments] == ["sandbox", "production"]
    with session_factory() as session:
        row = session.get(Environment, "production")
        assert row is not None and row.change_management == "direct" and row.inventory_refresh_minutes == 15


def test_write_environments_rejects_an_invalid_set_without_touching_the_file(
    session_factory, tmp_path: Path
) -> None:
    _copy_example(tmp_path / "config")
    store = ConfigStore(tmp_path / "config", session_factory)
    before = (tmp_path / "config" / "environments.yaml").read_text()
    duplicate = store.current.environments[0]
    with pytest.raises(ValueError, match="duplicate environment slug"):
        store.write_environments([duplicate, duplicate])
    assert (tmp_path / "config" / "environments.yaml").read_text() == before


def test_reload_picks_up_hand_edits_and_executors_follow_the_live_configuration(
    session_factory, tmp_path: Path
) -> None:
    from rdscommander.control_db.models import Operation
    from rdscommander.inventory.refresh_executor import InventoryRefreshExecutor

    _copy_example(tmp_path / "config")
    store = ConfigStore(tmp_path / "config", session_factory)
    executor = InventoryRefreshExecutor(store)
    operation = Operation(environment_slug="sandbox")
    assert executor.step_names(operation) == ["refresh:sandbox/000000000000/eu-west-2"]
    environments_path = tmp_path / "config" / "environments.yaml"
    environments_path.write_text(
        environments_path.read_text()
        + '      - aws_account_id: "000000000000"\n        aws_region: eu-west-1\n        simulated: true\n'
    )
    assert len(store.current.environment("sandbox").targets) == 1
    store.reload()
    assert executor.step_names(operation) == [
        "refresh:sandbox/000000000000/eu-west-2",
        "refresh:sandbox/000000000000/eu-west-1",
    ]


def test_static_store_serves_its_configuration_resyncs_on_reload_and_refuses_writes(
    session_factory, console_config
) -> None:
    from rdscommander.config.store import StaticConfigStore

    store = StaticConfigStore(console_config, session_factory)
    assert store.current is console_config
    with session_factory() as session:
        assert session.get(Environment, "sandbox") is None
    assert store.reload() is console_config
    with session_factory() as session:
        assert session.get(Environment, "sandbox") is not None
    with pytest.raises(RuntimeError, match="static configuration"):
        store.write_environments(console_config.environments)


def test_write_environments_changes_an_environment_in_place_and_keeps_comments_and_the_file_mode(
    session_factory, tmp_path: Path
) -> None:
    _copy_example(tmp_path / "config")
    environments_path = tmp_path / "config" / "environments.yaml"
    environments_path.chmod(0o664)
    store = ConfigStore(tmp_path / "config", session_factory)
    sandbox = store.current.environment("sandbox").model_copy(update={"change_management": "direct"})
    store.write_environments([sandbox])
    text = environments_path.read_text()
    assert "change_management: direct" in text
    assert "# How the console makes changes in this environment:" in text
    assert (
        text.replace("change_management: direct", "change_management: terraform")
        == (EXAMPLE / "environments.yaml").read_text()
    )
    assert environments_path.stat().st_mode & 0o777 == 0o664
    assert not [p.name for p in environments_path.parent.iterdir() if p.name.endswith(".tmp")]
