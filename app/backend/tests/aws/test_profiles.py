from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ProfileNotFound

from rdscommander.aws.profiles import caller_identity, client_for_profile, enabled_regions
from rdscommander.control_db.models import ExecutionRecord
from rdscommander.execution_records.service import ExecutionRecorder


def test_simulated_client_uses_the_fixed_keys_and_records_its_calls_under_the_import_target(
    session_factory, operator_settings, moto_aws
) -> None:
    recorder = ExecutionRecorder(session_factory)
    sts = client_for_profile(
        None,
        "eu-west-2",
        "sts",
        operator_settings,
        recorder,
        simulated=True,
        operation_id=None,
        step_name="probe",
    )
    assert caller_identity(sts)["Account"] == "123456789012"
    with session_factory() as session:
        [record] = session.query(ExecutionRecord).all()
    assert record.api_name == "sts:GetCallerIdentity"
    assert (
        record.target_key == "import/simulated/eu-west-2" and record.step_name == "probe" and record.succeeded
    )


def test_simulated_client_points_at_the_simulator_endpoint(
    session_factory, operator_settings, moto_aws
) -> None:
    settings = operator_settings.model_copy(update={"simulated_aws_endpoint_url": "http://127.0.0.1:5000"})
    client = client_for_profile(
        None,
        "eu-west-2",
        "rds",
        settings,
        ExecutionRecorder(session_factory),
        simulated=True,
        operation_id=None,
        step_name=None,
    )
    assert client.meta.endpoint_url == "http://127.0.0.1:5000"


def test_profile_client_reads_the_profile_from_the_aws_config(
    session_factory, operator_settings, moto_aws, monkeypatch, tmp_path: Path
) -> None:
    aws_config = tmp_path / "aws-config"
    aws_config.write_text("[profile acme-dev]\nregion = eu-west-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
    client = client_for_profile(
        "acme-dev",
        "eu-west-2",
        "rds",
        operator_settings,
        ExecutionRecorder(session_factory),
        simulated=False,
        operation_id=None,
        step_name=None,
    )
    assert client.meta.region_name == "eu-west-2"
    with pytest.raises(ProfileNotFound):
        client_for_profile(
            "ghost",
            "eu-west-2",
            "rds",
            operator_settings,
            ExecutionRecorder(session_factory),
            simulated=False,
            operation_id=None,
            step_name=None,
        )


class _PagedAccountClient:
    """Stands in for the `account` client: two pages of regions."""

    def get_paginator(self, operation_name: str):  # noqa: ANN201
        assert operation_name == "list_regions"
        return self

    def paginate(self, **kwargs):  # noqa: ANN003, ANN201
        assert kwargs["RegionOptStatusContains"] == ["ENABLED", "ENABLED_BY_DEFAULT"]
        return iter([{"Regions": [{"RegionName": "eu-west-2"}]}, {"Regions": [{"RegionName": "eu-west-1"}]}])


class _DeniedAccountClient:
    """Stands in for the `account` client of an identity that may not list regions."""

    def get_paginator(self, operation_name: str):  # noqa: ANN201
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListRegions")


def test_enabled_regions_reads_every_page_and_sorts() -> None:
    assert enabled_regions(_PagedAccountClient()) == ["eu-west-1", "eu-west-2"]


def test_enabled_regions_falls_back_to_the_partition_regions_when_listing_is_denied() -> None:
    regions = enabled_regions(_DeniedAccountClient())
    assert "eu-west-2" in regions and regions == sorted(regions)


def test_a_very_long_profile_name_still_fits_the_execution_record_target_column(
    session_factory, operator_settings, moto_aws, monkeypatch, tmp_path: Path
) -> None:
    long_profile_name = "acme-" + "very-long-profile-name-" * 4
    aws_config = tmp_path / "aws-config"
    aws_config.write_text(
        f"[profile {long_profile_name}]\nregion = eu-west-2\n"
        "aws_access_key_id = testing\naws_secret_access_key = testing\n"
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_config))
    recorder = ExecutionRecorder(session_factory)
    sts = client_for_profile(
        long_profile_name,
        "ap-southeast-2",
        "sts",
        operator_settings,
        recorder,
        simulated=False,
        operation_id=None,
        step_name="probe",
    )
    caller_identity(sts)
    with session_factory() as session:
        [record] = session.query(ExecutionRecord).all()
    assert record.target_key.startswith("import/acme-very-long") and len(record.target_key) == 80
