from pathlib import Path

import yaml
from botocore.exceptions import ClientError

from rdscommander.config.store import ConfigStore
from rdscommander.main import build_executors, build_services
from rdscommander.operations.enums import OperationState
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from tests.terraform.test_adoption_executor import _git, _prepare_repo


def _write_config(operator_settings, console_config, session_factory) -> ConfigStore:
    config_dir = operator_settings.config_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "terraform.yaml").write_text(
        yaml.safe_dump(console_config.terraform.model_dump(mode="json"))
    )
    (config_dir / "environments.yaml").write_text(
        yaml.safe_dump({"environments": [e.model_dump(mode="json") for e in console_config.environments]})
    )
    return ConfigStore(config_dir, session_factory)


def _import(session_factory, operator_settings, store, options: dict, key: str):
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory, build_executors(services, store), "r", operator_settings, store, services
    )
    operation = OperationService(session_factory).create(
        "environment_import",
        None,
        None,
        {"options": options},
        key,
        "phil",
        title=f"Import environment: {options['slug']}",
    )
    runner.run_once()
    return OperationService(session_factory).get(operation.id), runner


def test_import_writes_commits_reloads_and_queues_a_refresh(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    final, runner = _import(
        session_factory,
        operator_settings,
        store,
        {
            "slug": "staging",
            "display_name": "Staging",
            "aws_profile_name": None,
            "simulated": True,
            "regions": ["eu-west-2"],
            "change_management": "direct",
            "is_production": False,
        },
        "import-1",
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    written = yaml.safe_load((operator_settings.config_dir / "environments.yaml").read_text())
    staging = next(e for e in written["environments"] if e["slug"] == "staging")
    assert staging["change_management"] == "direct" and staging["targets"] == [
        {"aws_account_id": "123456789012", "aws_region": "eu-west-2", "simulated": True}
    ]
    assert store.current.environment("staging").display_name == "Staging"
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == "Import environment: staging (phil)"
    )
    with session_factory() as session:
        from rdscommander.control_db.models import Operation

        queued = (
            session.query(Operation)
            .filter_by(operation_kind="inventory_refresh", environment_slug="staging")
            .one()
        )
        assert queued.state == "QUEUED"


def test_import_refuses_a_duplicate_slug(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    final, _ = _import(
        session_factory,
        operator_settings,
        store,
        {
            "slug": "sandbox",
            "display_name": "Again",
            "aws_profile_name": None,
            "simulated": True,
            "regions": ["eu-west-2"],
            "change_management": "terraform",
            "is_production": False,
        },
        "import-2",
    )
    assert final.state == OperationState.FAILED
    assert any(d.diagnostic_kind == "environment_exists" for d in final.diagnostic_reports)


def test_import_with_an_expired_sso_session_explains_how_to_log_in(
    session_factory, operator_settings, console_config, moto_aws, monkeypatch, tmp_path: Path
) -> None:
    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    from rdscommander.aws import profiles

    aws_config = tmp_path / "aws-config"
    aws_config.write_text("[profile example-prod]\nregion = eu-west-2\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))

    def expired(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ClientError(
            {
                "Error": {
                    "Code": "ExpiredToken",
                    "Message": "The security token included in the request is expired",
                }
            },
            "GetCallerIdentity",
        )

    monkeypatch.setattr(profiles, "caller_identity", expired)
    environments_before = (operator_settings.config_dir / "environments.yaml").read_text()
    final, _ = _import(
        session_factory,
        operator_settings,
        store,
        {
            "slug": "prod2",
            "display_name": "Prod 2",
            "aws_profile_name": "example-prod",
            "simulated": False,
            "regions": ["eu-west-2"],
            "change_management": "terraform",
            "is_production": True,
        },
        "import-3",
    )
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "aws_credentials_expired"]
    assert "aws sso login --profile example-prod" in diagnostic.fix_suggestion
    assert "prod2" not in (operator_settings.config_dir / "environments.yaml").read_text()
    assert (operator_settings.config_dir / "environments.yaml").read_text() == environments_before
    assert [e.slug for e in store.current.environments] == ["sandbox", "production"]


def _simulated_import_options(slug: str, regions: list[str]) -> dict:
    return {
        "slug": slug,
        "display_name": slug.title(),
        "aws_profile_name": None,
        "simulated": True,
        "regions": regions,
        "change_management": "terraform",
        "is_production": False,
    }


def _create_postgres_instance(region: str, identifier: str) -> None:
    import boto3

    boto3.client("rds", region_name=region).create_db_instance(
        DBInstanceIdentifier=identifier,
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.t4g.micro",
        AllocatedStorage=20,
        MasterUsername="u",
        MasterUserPassword="passw0rd!",
    )


def test_import_without_regions_keeps_only_the_regions_that_hold_postgresql(
    session_factory, operator_settings, console_config, moto_aws, monkeypatch
) -> None:
    from rdscommander.aws import profiles

    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    _create_postgres_instance("eu-west-2", "orders")
    monkeypatch.setattr(profiles, "enabled_regions", lambda account_client: ["eu-west-1", "eu-west-2"])
    final, _ = _import(
        session_factory, operator_settings, store, _simulated_import_options("scanned", []), "import-scan"
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [t.aws_region for t in store.current.environment("scanned").targets] == ["eu-west-2"]
    [scan] = [a for a in final.step_attempts if a.step_name == "scan_regions"]
    assert scan.output["region_counts"] == {
        "eu-west-1": {"instances": 0, "clusters": 0},
        "eu-west-2": {"instances": 1, "clusters": 0},
    }


def test_import_that_finds_no_postgresql_anywhere_fails_and_leaves_the_configuration_alone(
    session_factory, operator_settings, console_config, moto_aws, monkeypatch
) -> None:
    from rdscommander.aws import profiles

    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    environments_before = (operator_settings.config_dir / "environments.yaml").read_text()
    monkeypatch.setattr(profiles, "enabled_regions", lambda account_client: ["eu-west-1", "eu-west-2"])
    final, _ = _import(
        session_factory, operator_settings, store, _simulated_import_options("empty", []), "import-empty"
    )
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "no_postgres_resources_found"]
    assert "Regions field" in diagnostic.fix_suggestion
    assert (operator_settings.config_dir / "environments.yaml").read_text() == environments_before


def test_import_with_a_profile_that_is_not_on_this_machine_names_the_profile(
    session_factory, operator_settings, console_config, moto_aws, monkeypatch, tmp_path: Path
) -> None:
    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    aws_config = tmp_path / "aws-config"
    aws_config.write_text("[profile somebody-else]\nregion = eu-west-2\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
    options = {
        **_simulated_import_options("ghost", ["eu-west-2"]),
        "simulated": False,
        "aws_profile_name": "ghost",
    }
    final, _ = _import(session_factory, operator_settings, store, options, "import-ghost")
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "aws_profile_not_found"]
    assert "ghost" in diagnostic.fix_suggestion
    assert "ghost" not in (operator_settings.config_dir / "environments.yaml").read_text()


def test_import_through_a_static_configuration_explains_that_it_cannot_write(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    from rdscommander.config.store import StaticConfigStore

    _prepare_repo(operator_settings)
    store = StaticConfigStore(console_config, session_factory)
    store.reload()
    final, _ = _import(
        session_factory,
        operator_settings,
        store,
        _simulated_import_options("staging", ["eu-west-2"]),
        "import-static",
    )
    assert final.state == OperationState.FAILED
    assert any(d.diagnostic_kind == "configuration_not_writable" for d in final.diagnostic_reports)


def _install_failing_pre_commit_hook(repo_root: Path) -> Path:
    hook = repo_root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    return hook


def test_a_failed_commit_rolls_the_import_back_and_a_second_import_of_the_slug_succeeds(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    environments_file = operator_settings.config_dir / "environments.yaml"
    bytes_before = environments_file.read_bytes()
    hook = _install_failing_pre_commit_hook(operator_settings.repo_root)
    options = _simulated_import_options("staging", ["eu-west-2"])

    failed, _ = _import(session_factory, operator_settings, store, options, "import-rollback-1")

    assert failed.state == OperationState.FAILED
    [diagnostic] = [d for d in failed.diagnostic_reports if d.diagnostic_kind == "commit_failed"]
    assert "hook says no" in diagnostic.detail
    assert (
        "rolled back" in diagnostic.fix_suggestion
        and "create a new import of 'staging'" in diagnostic.fix_suggestion
    )
    assert environments_file.read_bytes() == bytes_before
    assert [e.slug for e in store.current.environments] == ["sandbox", "production"]
    assert _git("diff", "--cached", "--name-only", cwd=operator_settings.repo_root) == ""

    hook.unlink()
    succeeded, _ = _import(session_factory, operator_settings, store, options, "import-rollback-2")

    assert succeeded.state == OperationState.SUCCEEDED, [d.detail for d in succeeded.diagnostic_reports]
    assert store.current.environment("staging").display_name == "Staging"
    written = yaml.safe_load(environments_file.read_text())
    assert [e["slug"] for e in written["environments"]].count("staging") == 1
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == "Import environment: staging (phil)"
    )


def _commit_step_context(session_factory, operator_settings, store, slug: str, write_output: dict):
    """A context for `commit_configuration` of an import whose earlier steps completed with these outputs."""
    from rdscommander.control_db.models import Operation, OperationStepAttempt
    from rdscommander.operations.enums import StepOutcomeStatus
    from rdscommander.operations.executor import StepContext

    options = _simulated_import_options(slug, ["eu-west-2"])
    operation = OperationService(session_factory).create(
        "environment_import", None, None, {"options": options}, f"commit-step-{slug}", "phil"
    )
    session = session_factory()
    for step_name, output in (
        ("resolve_account", {"aws_account_id": "123456789012"}),
        ("scan_regions", {"chosen_regions": ["eu-west-2"], "region_counts": {}}),
        ("write_configuration", write_output),
    ):
        session.add(
            OperationStepAttempt(
                operation_id=operation.id,
                step_name=step_name,
                attempt_number=1,
                observation_state="ABSENT",
                outcome_status=StepOutcomeStatus.COMPLETED.value,
                output=output,
            )
        )
    session.commit()
    return StepContext(
        session=session,
        operation=session.get(Operation, operation.id),
        settings=operator_settings,
        console_config=store.current,
        services=build_services(operator_settings, session_factory),
    )


def test_a_commit_that_fails_without_the_captured_file_text_explains_the_manual_recovery(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    from rdscommander.environments.import_executor import EnvironmentImportExecutor

    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    final, _ = _import(
        session_factory,
        operator_settings,
        store,
        _simulated_import_options("staging", ["eu-west-2"]),
        "import-first",
    )
    assert final.state == OperationState.SUCCEEDED
    # A resumed run whose write step was satisfied by observation, so no earlier file text was captured.
    environments_file = operator_settings.config_dir / "environments.yaml"
    environments_file.write_text(environments_file.read_text() + "# edited by hand\n")
    _install_failing_pre_commit_hook(operator_settings.repo_root)
    context = _commit_step_context(session_factory, operator_settings, store, "staging", {})

    outcome = EnvironmentImportExecutor(store).execute("commit_configuration", context)

    assert outcome.status.value == "failed" and outcome.diagnostic_recorded
    assert "could not be rolled back" in outcome.fix_suggestion
    assert "git checkout -- config/environments.yaml" in outcome.fix_suggestion


def test_the_commit_step_refuses_when_the_environment_is_no_longer_in_the_configuration(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    from rdscommander.environments.import_executor import EnvironmentImportExecutor

    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    context = _commit_step_context(
        session_factory, operator_settings, store, "ghost", {"previous_file_text": "x"}
    )

    outcome = EnvironmentImportExecutor(store).execute("commit_configuration", context)

    assert outcome.status.value == "failed"
    assert "create a new import of 'ghost'" in outcome.fix_suggestion


def test_write_configuration_is_satisfied_when_the_environment_is_already_in_the_configuration(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    from rdscommander.control_db.models import OperationStepAttempt
    from rdscommander.environments.import_executor import EnvironmentImportExecutor
    from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
    from rdscommander.operations.executor import StepContext

    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    options = _simulated_import_options("staging", ["eu-west-2"])
    final, _ = _import(session_factory, operator_settings, store, options, "import-observe")
    assert final.state == OperationState.SUCCEEDED
    # A crashed run: the environment is in the configuration, but the write step never recorded completing.
    retry = OperationService(session_factory).create(
        "environment_import", None, None, {"options": options}, "import-observe-retry", "phil"
    )
    with session_factory() as session:
        for step_name, output in (
            ("resolve_account", {"aws_account_id": "123456789012"}),
            ("scan_regions", {"chosen_regions": ["eu-west-2"], "region_counts": {}}),
        ):
            session.add(
                OperationStepAttempt(
                    operation_id=retry.id,
                    step_name=step_name,
                    attempt_number=1,
                    observation_state="ABSENT",
                    outcome_status=StepOutcomeStatus.COMPLETED.value,
                    output=output,
                )
            )
        session.commit()
        context = StepContext(
            session=session,
            operation=session.get(type(retry), retry.id),
            settings=operator_settings,
            console_config=store.current,
            services=build_services(operator_settings, session_factory),
        )
        executor = EnvironmentImportExecutor(store)
        assert executor.observe("write_configuration", context).state == ObservationState.SATISFIED
        assert executor.observe("commit_configuration", context).state == ObservationState.ABSENT


class _RdsReaderRejectedInOneRegion:
    """Builds an `RdsReader` stand-in for `error_code` in eu-west-1; every other region answers normally."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code

    def __call__(self, rds_client):  # noqa: ANN001, ANN204
        from rdscommander.aws.rds_reader import RdsReader

        error_code = self.error_code

        class RejectingReader(RdsReader):
            def list_postgres_instances(self):  # noqa: ANN202
                if self.rds.meta.region_name == "eu-west-1":
                    raise ClientError(
                        {"Error": {"Code": error_code, "Message": "rejected"}}, "DescribeDBInstances"
                    )
                return super().list_postgres_instances()

        return RejectingReader(rds_client)


def test_an_automatic_scan_skips_a_region_that_rejects_the_credentials_and_records_it(
    session_factory, operator_settings, console_config, moto_aws, monkeypatch
) -> None:
    from rdscommander.aws import profiles
    from rdscommander.environments import import_executor

    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    _create_postgres_instance("eu-west-2", "orders")
    monkeypatch.setattr(profiles, "enabled_regions", lambda account_client: ["eu-west-1", "eu-west-2"])
    monkeypatch.setattr(
        import_executor, "RdsReader", _RdsReaderRejectedInOneRegion("UnrecognizedClientException")
    )
    final, _ = _import(
        session_factory, operator_settings, store, _simulated_import_options("scanned", []), "import-skip"
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    [scan] = [a for a in final.step_attempts if a.step_name == "scan_regions"]
    assert scan.output["skipped_regions"] == ["eu-west-1"]
    assert scan.output["skipped_region_error_codes"] == {"eu-west-1": "UnrecognizedClientException"}
    assert scan.output["chosen_regions"] == ["eu-west-2"]
    assert [t.aws_region for t in store.current.environment("scanned").targets] == ["eu-west-2"]


def test_an_explicit_region_that_rejects_the_credentials_fails_the_import(
    session_factory, operator_settings, console_config, moto_aws, monkeypatch
) -> None:
    from rdscommander.environments import import_executor

    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    environments_before = (operator_settings.config_dir / "environments.yaml").read_text()
    monkeypatch.setattr(
        import_executor, "RdsReader", _RdsReaderRejectedInOneRegion("UnrecognizedClientException")
    )
    final, _ = _import(
        session_factory,
        operator_settings,
        store,
        _simulated_import_options("explicit", ["eu-west-1", "eu-west-2"]),
        "import-explicit",
    )
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "aws_credentials_expired"]
    assert diagnostic.detail.startswith("eu-west-1: ")
    assert (operator_settings.config_dir / "environments.yaml").read_text() == environments_before


def test_an_automatic_scan_still_fails_on_a_region_that_denies_access(
    session_factory, operator_settings, console_config, moto_aws, monkeypatch
) -> None:
    from rdscommander.aws import profiles
    from rdscommander.environments import import_executor

    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    monkeypatch.setattr(profiles, "enabled_regions", lambda account_client: ["eu-west-1", "eu-west-2"])
    monkeypatch.setattr(import_executor, "RdsReader", _RdsReaderRejectedInOneRegion("AccessDenied"))
    final, _ = _import(
        session_factory, operator_settings, store, _simulated_import_options("denied", []), "import-denied"
    )
    assert final.state == OperationState.FAILED
    assert any(d.diagnostic_kind == "aws_access_denied" for d in final.diagnostic_reports)


STAGING_IMPORT = {
    "slug": "staging",
    "display_name": "Staging",
    "aws_profile_name": None,
    "simulated": True,
    "regions": ["eu-west-2"],
    "change_management": "direct",
    "is_production": False,
}
EXAMPLE_ENVIRONMENTS = Path(__file__).resolve().parents[3] / "config.example" / "environments.yaml"


def test_an_import_keeps_the_comments_and_order_of_environments_yaml(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    """The comments operators copy from config.example explain every field; an import must not drop them."""
    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    environments_path = operator_settings.config_dir / "environments.yaml"
    example_text = EXAMPLE_ENVIRONMENTS.read_text()
    environments_path.write_text(example_text)
    store.reload()
    final, _ = _import(session_factory, operator_settings, store, STAGING_IMPORT, "import-comments")
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    written = environments_path.read_text()
    assert written.startswith(example_text), "the existing text, comments and order included, is kept"
    parsed = yaml.safe_load(written)
    assert [e["slug"] for e in parsed["environments"]] == ["sandbox", "staging"]
    assert parsed["environments"][1]["change_management"] == "direct"
    assert [e.slug for e in store.current.environments] == ["sandbox", "staging"]
    assert _git("show", "HEAD:config/environments.yaml", cwd=operator_settings.repo_root) == written.strip()


def test_an_import_is_refused_when_environments_yaml_changed_since_it_was_loaded_and_nothing_is_committed(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    """A hand edit saved after the console loaded the file would otherwise be overwritten and committed."""
    _prepare_repo(operator_settings)
    store = _write_config(operator_settings, console_config, session_factory)
    environments_path = operator_settings.config_dir / "environments.yaml"
    hand_edited = environments_path.read_text() + "# a note saved by hand after the console loaded the file\n"
    environments_path.write_text(hand_edited)
    head_before = _git("rev-parse", "HEAD", cwd=operator_settings.repo_root)
    final, _ = _import(session_factory, operator_settings, store, STAGING_IMPORT, "import-concurrent")
    assert final.state == OperationState.FAILED
    [diagnostic] = [
        d for d in final.diagnostic_reports if d.diagnostic_kind == "configuration_changed_since_load"
    ]
    assert (
        diagnostic.title
        == "config/environments.yaml changed since it was loaded; reload and retry the import"
    )
    assert environments_path.read_text() == hand_edited
    assert _git("rev-parse", "HEAD", cwd=operator_settings.repo_root) == head_before
    assert [e.slug for e in store.current.environments] == ["sandbox", "production"]
