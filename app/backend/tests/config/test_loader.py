import socket
from pathlib import Path

import pytest

from rdscommander.config.loader import ConfigurationError, load_console_config
from rdscommander.config.settings import OperatorSettings
from rdscommander.main import runner_identity_for

EXAMPLE_DIR = Path(__file__).resolve().parents[3] / "config.example"


def test_loader_reads_the_shipped_example_configuration() -> None:
    config = load_console_config(EXAMPLE_DIR)
    assert config.company.company_slug == "example"
    assert config.environment("sandbox").targets[0].simulated is True


def test_loader_names_the_missing_file(tmp_path: Path) -> None:
    (tmp_path / "company.yaml").write_text("company_display_name: X\ncompany_slug: x\n")
    with pytest.raises(ConfigurationError, match="environments.yaml"):
        load_console_config(tmp_path)


def test_loader_names_the_invalid_field(tmp_path: Path) -> None:
    (tmp_path / "company.yaml").write_text("company_display_name: X\ncompany_slug: x\n")
    (tmp_path / "terraform.yaml").write_text("required_opentofu_version: '~> 1.10'\n")
    (tmp_path / "environments.yaml").write_text(
        "environments:\n  - slug: prod\n    display_name: P\n    targets:\n"
        "      - aws_account_id: '12'\n        aws_region: eu-west-2\n        aws_profile_name: p\n"
    )
    with pytest.raises(ConfigurationError, match="aws_account_id"):
        load_console_config(tmp_path)


def test_operator_settings_read_rdsc_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("RDSC_OPERATOR_NAME", "phil")
    monkeypatch.setenv("RDSC_HTTP_PORT", "9000")
    monkeypatch.setenv("RDSC_CONTROL_DATABASE_URL", "postgresql+psycopg://u:secret@db/x")
    settings = OperatorSettings()
    assert settings.operator_name == "phil"
    assert settings.http_port == 9000
    assert "secret" not in repr(settings)
    assert settings.control_database_url.get_secret_value().endswith("@db/x")


def test_the_runner_identity_is_the_configured_name_rather_than_the_container_hostname() -> None:
    """A re-created container gets a new hostname; a configured identity name keeps the identity stable."""
    configured = OperatorSettings(operator_name="phil", runner_identity_name="rdscommander-console")
    assert runner_identity_for(configured) == "rdscommander-console:phil"
    unconfigured = OperatorSettings(operator_name="phil")
    assert runner_identity_for(unconfigured) == f"{socket.gethostname()}:phil"


def test_the_runner_identity_name_is_read_from_the_rdsc_environment(monkeypatch) -> None:
    monkeypatch.setenv("RDSC_RUNNER_IDENTITY_NAME", "rdscommander-console")
    assert OperatorSettings().runner_identity_name == "rdscommander-console"
