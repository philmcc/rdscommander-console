from pathlib import Path

from rdscommander.config.aws_profiles import list_aws_profiles


def test_profiles_are_read_from_config_and_credentials_files(tmp_path: Path) -> None:
    (tmp_path / "config").write_text(
        "[default]\nregion = eu-west-2\n\n[profile acme-prod]\nsso_session = acme\n\n"
        "[profile acme-staging]\nregion = eu-west-1\n\n[sso-session acme]\nsso_start_url = https://x\n"
    )
    (tmp_path / "credentials").write_text(
        "[legacy-keys]\naws_access_key_id = AKIA\naws_secret_access_key = x\n"
    )
    assert list_aws_profiles(tmp_path / "config", tmp_path / "credentials") == [
        "acme-prod",
        "acme-staging",
        "default",
        "legacy-keys",
    ]


def test_missing_files_yield_no_profiles(tmp_path: Path) -> None:
    assert list_aws_profiles(tmp_path / "config", tmp_path / "credentials") == []
