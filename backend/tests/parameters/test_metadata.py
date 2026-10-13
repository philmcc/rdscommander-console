import json
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from botocore.exceptions import ClientError
from sqlalchemy import select

from rdscommander.aws.rds_reader import SimulatorUnsupportedAction
from rdscommander.control_db.models import EngineParameterFamily, EngineVersion
from rdscommander.logging_setup import configure_structured_logging
from rdscommander.parameters.metadata import (
    BUNDLED_FAMILIES,
    ParameterMetadata,
    UnknownParameterFamilyError,
    family_for_version,
    load_engine_versions,
    load_family_metadata,
    validate_parameter_value,
    validate_parameters,
)


def _meta(**overrides):  # noqa: ANN003, ANN202
    base = {
        "name": "work_mem",
        "default_value": "4096",
        "description": "",
        "data_type": "integer",
        "allowed_values": "64-2147483647",
        "is_modifiable": True,
        "apply_type": "dynamic",
        "apply_method_default": "immediate",
        "minimum_engine_version": None,
    }
    return ParameterMetadata(**{**base, **overrides})


WORK_MEM_AWS_ENTRY = {
    "ParameterName": "work_mem",
    "ParameterValue": "4096",
    "Description": "x",
    "DataType": "integer",
    "AllowedValues": "64-2147483647",
    "IsModifiable": True,
    "ApplyType": "dynamic",
}


def _access_denied(operation_name: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, operation_name)


def test_bundled_families_cover_postgres_and_aurora_15_to_17() -> None:
    assert set(BUNDLED_FAMILIES) == {
        "postgres15",
        "postgres16",
        "postgres17",
        "aurora-postgresql15",
        "aurora-postgresql16",
        "aurora-postgresql17",
    }


def test_load_family_metadata_uses_the_bundle_for_the_simulator_and_caches_it(session_factory) -> None:
    with session_factory() as session:
        loaded = load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
        session.commit()
        assert loaded.source == "bundled" and loaded.parameters["work_mem"].apply_type == "dynamic"
        assert loaded.parameters["shared_buffers"].apply_type == "static"
        assert "force_parallel_mode" not in loaded.parameters and "debug_parallel_query" in loaded.parameters
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        assert row is not None and len(row.parameters) == len(loaded.parameters)
        assert row.source == "bundled"


def test_load_family_metadata_reads_aws_for_real_accounts_and_refreshes_after_seven_days(
    session_factory,
) -> None:
    class Reader:
        calls = 0

        def describe_engine_default_parameters(self, family, scope):  # noqa: ANN001, ANN202
            Reader.calls += 1
            return [WORK_MEM_AWS_ENTRY]

    with session_factory() as session:
        first = load_family_metadata(session, "postgres17", Reader(), scope="instance", simulated=False)  # type: ignore[arg-type]
        session.commit()
        second = load_family_metadata(session, "postgres17", Reader(), scope="instance", simulated=False)  # type: ignore[arg-type]
        assert first.source == "aws" and second.source == "aws" and Reader.calls == 1
        session.get(EngineParameterFamily, ("postgres17", "instance")).fetched_at_utc = datetime.now(
            UTC
        ) - timedelta(days=8)
        session.commit()
        load_family_metadata(session, "postgres17", Reader(), scope="instance", simulated=False)  # type: ignore[arg-type]
        assert Reader.calls == 2


class SimulatorReader:
    """A reader whose account cannot answer, as the simulator cannot."""

    calls = 0

    def describe_engine_default_parameters(self, family, scope):  # noqa: ANN001, ANN202
        SimulatorReader.calls += 1
        raise SimulatorUnsupportedAction("describe_engine_default_parameters")

    def list_engine_versions(self, engine):  # noqa: ANN001, ANN202
        SimulatorReader.calls += 1
        raise SimulatorUnsupportedAction("describe_db_engine_versions")


def test_load_family_metadata_falls_back_to_the_bundle_when_the_account_cannot_answer(
    session_factory,
) -> None:
    with session_factory() as session:
        loaded = load_family_metadata(
            session, "postgres16", SimulatorReader(), scope="instance", simulated=False
        )  # type: ignore[arg-type]
        assert loaded.source == "bundled" and "work_mem" in loaded.parameters
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        assert row is not None and row.source == "bundled"


def test_the_bundle_fallback_never_overwrites_cached_aws_data(session_factory) -> None:
    with session_factory() as session:
        session.add(
            EngineParameterFamily(
                family="postgres16",
                scope="instance",
                parameters=[WORK_MEM_AWS_ENTRY],
                source="aws",
                fetched_at_utc=datetime.now(UTC) - timedelta(days=8),
            )
        )
        session.commit()
        loaded = load_family_metadata(
            session, "postgres16", SimulatorReader(), scope="instance", simulated=False
        )  # type: ignore[arg-type]
        session.commit()
        assert loaded.source == "bundled" and len(loaded.parameters) > 1
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        assert row is not None and row.source == "aws" and row.parameters == [WORK_MEM_AWS_ENTRY]


def test_engine_versions_of_a_real_account_still_need_a_reader(session_factory) -> None:
    with session_factory() as session:
        with pytest.raises(ValueError, match="needs a reader"):
            load_engine_versions(session, "postgres", None, simulated=False)


def test_a_real_account_without_a_reader_answers_from_the_bundle_without_recording_it(
    session_factory,
) -> None:
    with session_factory() as session:
        loaded = load_family_metadata(session, "postgres16", None, scope="instance", simulated=False)
        session.commit()
        assert loaded.source == "bundled" and "work_mem" in loaded.parameters
        assert session.get(EngineParameterFamily, ("postgres16", "instance")) is None


def test_a_real_account_without_a_reader_uses_fresh_cached_aws_data(session_factory) -> None:
    with session_factory() as session:
        session.add(
            EngineParameterFamily(
                family="postgres16", scope="instance", parameters=[WORK_MEM_AWS_ENTRY], source="aws"
            )
        )
        session.commit()
        loaded = load_family_metadata(session, "postgres16", None, scope="instance", simulated=False)
        assert loaded.source == "aws" and list(loaded.parameters) == ["work_mem"]


def test_a_real_account_without_a_reader_ignores_stale_aws_data_and_leaves_the_row_alone(
    session_factory,
) -> None:
    stale_at = datetime.now(UTC) - timedelta(days=8)
    with session_factory() as session:
        session.add(
            EngineParameterFamily(
                family="postgres16",
                scope="instance",
                parameters=[WORK_MEM_AWS_ENTRY],
                source="aws",
                fetched_at_utc=stale_at,
            )
        )
        session.commit()
        loaded = load_family_metadata(session, "postgres16", None, scope="instance", simulated=False)
        session.commit()
        assert loaded.source == "bundled" and len(loaded.parameters) > 1
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        assert row is not None and row.source == "aws" and row.parameters == [WORK_MEM_AWS_ENTRY]
        assert row.fetched_at_utc == stale_at


def test_a_real_account_without_a_reader_ignores_a_bundled_row(session_factory) -> None:
    with session_factory() as session:
        load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
        session.commit()
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        fetched_at = row.fetched_at_utc
        loaded = load_family_metadata(session, "postgres16", None, scope="instance", simulated=False)
        session.commit()
        assert loaded.source == "bundled"
        assert session.get(EngineParameterFamily, ("postgres16", "instance")).fetched_at_utc == fetched_at


def test_a_real_account_without_a_reader_names_a_family_neither_the_cache_nor_the_bundle_knows(
    session_factory,
) -> None:
    with session_factory() as session:
        with pytest.raises(KeyError, match="postgres14"):
            load_family_metadata(session, "postgres14", None, scope="instance", simulated=False)
        assert session.get(EngineParameterFamily, ("postgres14", "instance")) is None


def test_load_family_metadata_does_not_swallow_a_real_aws_failure(session_factory, capsys) -> None:
    class DeniedReader:
        def describe_engine_default_parameters(self, family, scope):  # noqa: ANN001, ANN202
            raise _access_denied("DescribeEngineDefaultParameters")

    configure_structured_logging()
    capsys.readouterr()  # discard output written before JSON logging was configured
    try:
        with session_factory() as session:
            with pytest.raises(ClientError):
                load_family_metadata(session, "postgres16", DeniedReader(), scope="instance", simulated=False)  # type: ignore[arg-type]
            assert session.get(EngineParameterFamily, ("postgres16", "instance")) is None
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    finally:
        structlog.reset_defaults()
    [failure] = [e for e in events if e["event"] == "family_metadata_aws_read_failed"]
    assert failure["level"] == "error" and failure["family"] == "postgres16" and failure["fix_suggestion"]


class AwsReader:
    """A real account that answers with a single parameter and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def describe_engine_default_parameters(self, family, scope):  # noqa: ANN001, ANN202
        self.calls += 1
        return [WORK_MEM_AWS_ENTRY]


def test_a_real_account_never_sees_a_bundled_row_even_after_the_bundle_changed(session_factory) -> None:
    reader = AwsReader()
    with session_factory() as session:
        bundled = load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
        session.commit()
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        assert row is not None and row.source == "bundled"
        # The bundled file changed since the row was written: its content no longer equals the file.
        row.parameters = [{**entry, "ParameterValue": "edited"} for entry in row.parameters]
        session.commit()
        real = load_family_metadata(session, "postgres16", reader, scope="instance", simulated=False)  # type: ignore[arg-type]
        session.commit()
        assert bundled.source == "bundled" and len(bundled.parameters) > 1
        assert real.source == "aws" and list(real.parameters) == ["work_mem"] and reader.calls == 1
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        assert row is not None and row.source == "aws"
        again = load_family_metadata(session, "postgres16", reader, scope="instance", simulated=False)  # type: ignore[arg-type]
        assert again.source == "aws" and reader.calls == 1


def test_a_simulated_environment_still_gets_the_bundle_after_a_real_one_filled_the_row(
    session_factory,
) -> None:
    reader = AwsReader()
    with session_factory() as session:
        real = load_family_metadata(session, "postgres16", reader, scope="instance", simulated=False)  # type: ignore[arg-type]
        session.commit()
        simulated = load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
        session.commit()
        assert real.source == "aws" and list(real.parameters) == ["work_mem"]
        assert simulated.source == "bundled" and len(simulated.parameters) > 1
        assert "debug_parallel_query" in simulated.parameters
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        assert row is not None and row.source == "aws" and row.parameters == [WORK_MEM_AWS_ENTRY]


def test_a_stale_bundled_row_is_refreshed_by_the_simulator(session_factory) -> None:
    with session_factory() as session:
        load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
        row = session.get(EngineParameterFamily, ("postgres16", "instance"))
        stale_at = datetime.now(UTC) - timedelta(days=8)
        row.fetched_at_utc = stale_at
        session.commit()
        loaded = load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
        session.commit()
        assert loaded.source == "bundled" and loaded.fetched_at_utc > stale_at
        assert session.get(EngineParameterFamily, ("postgres16", "instance")).fetched_at_utc > stale_at


def test_load_family_metadata_raises_key_error_for_unknown_families(session_factory) -> None:
    class EmptyReader:
        def describe_engine_default_parameters(self, family, scope):  # noqa: ANN001, ANN202
            return []

    with session_factory() as session:
        with pytest.raises(KeyError, match="postgres9"):
            load_family_metadata(session, "postgres9", None, scope="instance", simulated=True)
        with pytest.raises(KeyError, match="postgres9"):
            load_family_metadata(session, "postgres9", EmptyReader(), scope="instance", simulated=False)  # type: ignore[arg-type]
        assert session.get(EngineParameterFamily, ("postgres9", "instance")) is None


@pytest.mark.parametrize(
    ("metadata", "value", "expected"),
    [
        (_meta(), "65536", None),
        (_meta(), "abc", "work_mem must be an integer"),
        (_meta(), "4.5", "work_mem must be an integer"),
        (_meta(), "10", "work_mem must be between 64 and 2147483647"),
        (_meta(), "-5", "work_mem must be between 64 and 2147483647"),
        (_meta(allowed_values="0,1", data_type="boolean", name="rds.force_ssl"), "1", None),
        (
            _meta(allowed_values="0,1", data_type="boolean", name="rds.force_ssl"),
            "yes",
            "rds.force_ssl must be one of 0, 1",
        ),
        (
            _meta(allowed_values=None, data_type="boolean", name="rds.force_ssl"),
            "2",
            "rds.force_ssl must be one of 0, 1",
        ),
        (
            _meta(name="shared_buffers", allowed_values="16-1073741823", data_type="integer"),
            "{DBInstanceClassMemory/32768}",
            None,
        ),
        (
            _meta(name="shared_buffers", allowed_values="16-1073741823", data_type="integer"),
            "LEAST({DBInstanceClassMemory/9531392},5000)",
            None,
        ),
        (_meta(name="log_statement", allowed_values="none,ddl,mod,all", data_type="string"), "mod", None),
        (
            _meta(name="log_statement", allowed_values="none,ddl,mod,all", data_type="string"),
            "everything",
            "log_statement must be one of none, ddl, mod, all",
        ),
        (_meta(name="random_page_cost", data_type="float", allowed_values="0-1.79769e+308"), "1.1", None),
        (
            _meta(name="random_page_cost", data_type="float", allowed_values="0-1.79769e+308"),
            "cheap",
            "random_page_cost must be a number",
        ),
        (
            _meta(name="random_page_cost", data_type="float", allowed_values="0-1.79769e+308"),
            "nan",
            "random_page_cost must be a number",
        ),
        (
            _meta(name="random_page_cost", data_type="float", allowed_values="0-1.79769e+308"),
            "-1",
            "random_page_cost must be between 0 and 1.79769e+308",
        ),
        (
            _meta(name="log_min_duration_statement", allowed_values="-1-2147483647"),
            "-1",
            None,
        ),
        (
            _meta(name="log_min_duration_statement", allowed_values="-1-2147483647"),
            "-2",
            "log_min_duration_statement must be between -1 and 2147483647",
        ),
        (
            _meta(
                name="shared_preload_libraries",
                data_type="list",
                allowed_values="pgaudit,pg_cron,pg_stat_statements",
            ),
            "pg_stat_statements,pgaudit",
            None,
        ),
        (
            _meta(
                name="shared_preload_libraries",
                data_type="list",
                allowed_values="pgaudit,pg_cron,pg_stat_statements",
            ),
            "pgaudit,timescaledb",
            "shared_preload_libraries must be a comma-separated list of "
            "pgaudit, pg_cron, pg_stat_statements (timescaledb is not allowed)",
        ),
        (
            _meta(name="rds.logical_replication", is_modifiable=False),
            "1",
            "rds.logical_replication cannot be modified",
        ),
        (
            _meta(name="rds.logical_replication", is_modifiable=False),
            "{DBInstanceClassMemory/1}",
            "rds.logical_replication cannot be modified",
        ),
    ],
)
def test_validate_parameter_value(metadata: ParameterMetadata, value: str, expected: str | None) -> None:
    assert validate_parameter_value(metadata, value) == expected


def test_validate_parameters_reports_unknown_names_and_bad_values(session_factory) -> None:
    with session_factory() as session:
        family = load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
    problems = validate_parameters(
        family, {"work_mem": "abc", "no_such_parameter": "1", "log_min_duration_statement": "500"}
    )
    assert {p.name: p.message for p in problems} == {
        "work_mem": "work_mem must be an integer",
        "no_such_parameter": "no_such_parameter is not a parameter of postgres16",
    }


@pytest.mark.parametrize(
    ("family_name", "scope"),
    [(family, "instance") for family in BUNDLED_FAMILIES]
    + [(family, "cluster") for family in BUNDLED_FAMILIES if family.startswith("aurora-")],
)
def test_every_bundled_family_loads_and_accepts_its_own_modifiable_defaults(
    session_factory, family_name: str, scope: str
) -> None:
    with session_factory() as session:
        family = load_family_metadata(session, family_name, None, scope=scope, simulated=True)
    modifiable_defaults = {
        name: metadata.default_value
        for name, metadata in family.parameters.items()
        if metadata.is_modifiable and metadata.default_value is not None
    }
    assert {"work_mem", "log_statement"} <= set(modifiable_defaults)
    assert validate_parameters(family, modifiable_defaults) == []


def test_engine_versions_from_the_bundle_know_families_and_major_targets(session_factory) -> None:
    with session_factory() as session:
        versions = load_engine_versions(session, "postgres", None, simulated=True)
    sixteen = next(v for v in versions if v.engine_version.startswith("16."))
    assert sixteen.parameter_group_family == "postgres16"
    assert any(t.is_major and t.engine_version.startswith("17.") for t in sixteen.valid_upgrade_targets)


def test_engine_versions_for_aurora_come_from_the_aurora_bundles_only(session_factory) -> None:
    with session_factory() as session:
        versions = load_engine_versions(session, "aurora-postgresql", None, simulated=True)
    assert {v.parameter_group_family for v in versions} == {
        "aurora-postgresql15",
        "aurora-postgresql16",
        "aurora-postgresql17",
    }
    assert {v.engine for v in versions} == {"aurora-postgresql"}


def test_engine_versions_for_real_accounts_come_from_aws_and_are_cached_for_seven_days(
    session_factory,
) -> None:
    class Reader:
        calls = 0

        def list_engine_versions(self, engine):  # noqa: ANN001, ANN202
            Reader.calls += 1
            return [
                {
                    "Engine": engine,
                    "EngineVersion": "16.9",
                    "DBParameterGroupFamily": "postgres16",
                    "ValidUpgradeTarget": [
                        {"EngineVersion": "17.5", "IsMajorVersionUpgrade": True},
                        {"EngineVersion": "16.10", "IsMajorVersionUpgrade": False},
                    ],
                }
            ]

    with session_factory() as session:
        real = load_engine_versions(session, "postgres", Reader(), simulated=False)  # type: ignore[arg-type]
        session.commit()
        again = load_engine_versions(session, "postgres", Reader(), simulated=False)  # type: ignore[arg-type]
        assert [v.engine_version for v in real] == ["16.9"] and again == real and Reader.calls == 1
        assert [(t.engine_version, t.is_major) for t in real[0].valid_upgrade_targets] == [
            ("17.5", True),
            ("16.10", False),
        ]
        stale_after = datetime.now(UTC) - timedelta(days=8)
        for row in session.execute(select(EngineVersion)).scalars().all():
            row.fetched_at_utc = stale_after
        session.commit()
        load_engine_versions(session, "postgres", Reader(), simulated=False)  # type: ignore[arg-type]
        assert Reader.calls == 2


def test_engine_versions_for_the_simulator_ignore_and_never_write_the_cache(session_factory) -> None:
    with session_factory() as session:
        session.add(
            EngineVersion(
                engine="postgres",
                engine_version="16.9",
                parameter_group_family="postgres16",
                valid_upgrade_targets=[],
            )
        )
        session.commit()
        simulated = load_engine_versions(session, "postgres", None, simulated=True)
        session.commit()
        assert "16.9" not in {v.engine_version for v in simulated} and len(simulated) > 1
        assert [row.engine_version for row in session.execute(select(EngineVersion)).scalars()] == ["16.9"]


def test_engine_versions_fall_back_to_the_bundle_without_recording_it(session_factory) -> None:
    with session_factory() as session:
        loaded = load_engine_versions(session, "postgres", SimulatorReader(), simulated=False)  # type: ignore[arg-type]
        session.commit()
        assert {v.parameter_group_family for v in loaded} == {"postgres15", "postgres16", "postgres17"}
        assert session.execute(select(EngineVersion)).first() is None


def test_engine_versions_do_not_swallow_a_real_aws_failure(session_factory) -> None:
    class DeniedReader:
        def list_engine_versions(self, engine):  # noqa: ANN001, ANN202
            raise _access_denied("DescribeDBEngineVersions")

    with session_factory() as session:
        with pytest.raises(ClientError):
            load_engine_versions(session, "postgres", DeniedReader(), simulated=False)  # type: ignore[arg-type]


def test_family_for_version_uses_the_listed_family_and_falls_back_to_engine_and_major(
    session_factory,
) -> None:
    with session_factory() as session:
        versions = load_engine_versions(session, "aurora-postgresql", None, simulated=True)
    assert family_for_version("aurora-postgresql", "16.4", versions) == "aurora-postgresql16"
    assert family_for_version("aurora-postgresql", "16.99", versions) == "aurora-postgresql16"
    assert family_for_version("postgres", "17.4", []) == "postgres17"


INSTANCE_ONLY_AWS_ENTRY = {
    "ParameterName": "max_connections",
    "ParameterValue": "LEAST({DBInstanceClassMemory/9531392},5000)",
    "Description": "Sets the maximum number of concurrent connections.",
    "DataType": "integer",
    "AllowedValues": "6-262143",
    "IsModifiable": True,
    "ApplyType": "static",
}
CLUSTER_ONLY_AWS_ENTRY = {
    "ParameterName": "rds.logical_replication",
    "ParameterValue": "0",
    "Description": "Enables logical decoding.",
    "DataType": "boolean",
    "AllowedValues": "0,1",
    "IsModifiable": True,
    "ApplyType": "static",
}


def test_load_family_metadata_keeps_the_instance_and_cluster_defaults_of_an_aurora_family_apart(
    session_factory,
) -> None:
    class ScopedReader:
        def __init__(self) -> None:
            self.requested: list[tuple[str, str]] = []

        def describe_engine_default_parameters(self, family, scope):  # noqa: ANN001, ANN202
            self.requested.append((family, scope))
            return [
                WORK_MEM_AWS_ENTRY,
                INSTANCE_ONLY_AWS_ENTRY if scope == "instance" else CLUSTER_ONLY_AWS_ENTRY,
            ]

    reader = ScopedReader()
    with session_factory() as session:
        instance = load_family_metadata(
            session,
            "aurora-postgresql16",
            reader,
            scope="instance",
            simulated=False,  # type: ignore[arg-type]
        )
        cluster = load_family_metadata(
            session,
            "aurora-postgresql16",
            reader,
            scope="cluster",
            simulated=False,  # type: ignore[arg-type]
        )
        session.commit()
        again = load_family_metadata(
            session,
            "aurora-postgresql16",
            reader,
            scope="instance",
            simulated=False,  # type: ignore[arg-type]
        )
        rows = session.execute(select(EngineParameterFamily).order_by(EngineParameterFamily.scope)).scalars()
        cached = [(row.family, row.scope, row.source) for row in rows]
    assert reader.requested == [("aurora-postgresql16", "instance"), ("aurora-postgresql16", "cluster")]
    assert instance.scope == "instance" and set(instance.parameters) == {"work_mem", "max_connections"}
    assert cluster.scope == "cluster" and set(cluster.parameters) == {"work_mem", "rds.logical_replication"}
    assert set(again.parameters) == set(instance.parameters)
    assert cached == [("aurora-postgresql16", "cluster", "aws"), ("aurora-postgresql16", "instance", "aws")]
    assert [
        p.message
        for p in validate_parameters(instance, {"max_connections": "500", "rds.logical_replication": "1"})
    ] == ["rds.logical_replication is not a parameter of aurora-postgresql16 instance groups"]


@pytest.mark.parametrize("family_name", ["aurora-postgresql15", "aurora-postgresql16", "aurora-postgresql17"])
def test_bundled_aurora_families_keep_instance_and_cluster_parameters_apart(
    session_factory, family_name: str
) -> None:
    with session_factory() as session:
        instance = load_family_metadata(session, family_name, None, scope="instance", simulated=True)
        cluster = load_family_metadata(session, family_name, None, scope="cluster", simulated=True)
    assert {"shared_buffers", "max_connections", "work_mem"} <= set(instance.parameters)
    assert not {"rds.force_ssl", "rds.logical_replication"} & set(instance.parameters)
    assert {"rds.force_ssl", "rds.logical_replication", "work_mem"} <= set(cluster.parameters)
    assert not {"shared_buffers", "max_connections"} & set(cluster.parameters)


def test_a_bundled_postgres_family_has_no_cluster_parameters(session_factory) -> None:
    with session_factory() as session, pytest.raises(UnknownParameterFamilyError) as raised:
        load_family_metadata(session, "postgres16", None, scope="cluster", simulated=True)
    assert "cluster" in str(raised.value)
