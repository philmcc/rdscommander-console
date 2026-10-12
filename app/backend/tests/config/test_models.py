import pytest
from pydantic import ValidationError

from rdscommander.config.models import ConsoleConfig, EnvironmentTarget, ResolvedTarget


def _config(**overrides) -> dict:
    base = {
        "company": {"company_display_name": "Acme Ltd", "company_slug": "acme"},
        "terraform": {
            "required_opentofu_version": "~> 1.10",
            "aws_provider_version": "~> 6.0",
            "state_bucket_name_template": "{company_slug}-rdscommander-state-{aws_account_id}-{aws_region}",
        },
        "environments": [
            {
                "slug": "production",
                "display_name": "Production",
                "targets": [
                    {
                        "aws_account_id": "123456789012",
                        "aws_region": "eu-west-2",
                        "aws_profile_name": "acme-prod",
                    },
                    {
                        "aws_account_id": "123456789012",
                        "aws_region": "eu-west-1",
                        "aws_profile_name": "acme-prod",
                    },
                ],
            },
            {
                "slug": "sandbox",
                "display_name": "Sandbox",
                "targets": [{"aws_account_id": "000000000000", "aws_region": "eu-west-2", "simulated": True}],
            },
        ],
    }
    base.update(overrides)
    return base


def test_console_config_resolves_targets_with_keys_and_workspace_dirs() -> None:
    config = ConsoleConfig.model_validate(_config())
    target = config.target("production/123456789012/eu-west-1")
    assert isinstance(target, ResolvedTarget)
    assert target.environment_display_name == "Production"
    assert target.workspace_relative_dir.as_posix() == "terraform/production/123456789012/eu-west-1"
    assert target.state_key == "rdscommander/production/123456789012/eu-west-1/terraform.tfstate"
    assert target.state_bucket_name(config.terraform.state_bucket_name_template, "acme") == (
        "acme-rdscommander-state-123456789012-eu-west-1"
    )
    assert [t.target_key for t in config.all_targets()] == [
        "production/123456789012/eu-west-2",
        "production/123456789012/eu-west-1",
        "sandbox/000000000000/eu-west-2",
    ]
    assert config.has_simulated_targets is True


def test_real_target_requires_profile_and_simulated_target_forbids_it() -> None:
    with pytest.raises(ValidationError, match="aws_profile_name is required"):
        EnvironmentTarget(aws_account_id="123456789012", aws_region="eu-west-2")
    with pytest.raises(ValidationError, match="must not set aws_profile_name"):
        EnvironmentTarget(
            aws_account_id="000000000000", aws_region="eu-west-2", simulated=True, aws_profile_name="x"
        )


def test_duplicate_environment_slugs_and_duplicate_targets_are_rejected() -> None:
    duplicated = _config()
    duplicated["environments"].append(duplicated["environments"][1])
    with pytest.raises(ValidationError, match="duplicate environment slug"):
        ConsoleConfig.model_validate(duplicated)
    same_target = _config()
    same_target["environments"][0]["targets"].append(same_target["environments"][0]["targets"][0])
    with pytest.raises(ValidationError, match="duplicate target"):
        ConsoleConfig.model_validate(same_target)


def test_state_bucket_name_is_validated_against_s3_rules() -> None:
    config = ConsoleConfig.model_validate(
        _config(terraform={"state_bucket_name_template": "{company_slug}_BAD_{aws_account_id}-{aws_region}"})
    )
    with pytest.raises(ValueError, match="S3 bucket name"):
        config.all_targets()[0].state_bucket_name(config.terraform.state_bucket_name_template, "acme")


def test_unknown_target_key_names_known_keys() -> None:
    config = ConsoleConfig.model_validate(_config())
    with pytest.raises(KeyError, match="sandbox/000000000000/eu-west-2"):
        config.target("nope")


def test_requires_approval_cannot_be_disabled_in_this_release() -> None:
    disabled = _config()
    disabled["environments"][0]["requires_approval"] = False
    with pytest.raises(ValidationError, match="requires_approval"):
        ConsoleConfig.model_validate(disabled)


def test_environment_mode_fields_validate() -> None:
    raw = _config()
    raw["environments"][0]["change_management"] = "direct"
    raw["environments"][0]["inventory_refresh_minutes"] = 5
    config = ConsoleConfig.model_validate(raw)
    target = config.target("production/123456789012/eu-west-2")
    assert target.change_management == "direct" and target.is_direct
    bad = _config()
    bad["environments"][0]["change_management"] = "manual"
    with pytest.raises(ValidationError, match="change_management"):
        ConsoleConfig.model_validate(bad)
