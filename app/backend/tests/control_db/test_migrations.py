from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from rdscommander.control_db.models import Base, BlueGreenDeployment, Operation
from rdscommander.control_db.session import ControlDbSessionFactory, create_control_db_engine


def test_migrated_schema_matches_models_exactly(control_db_url: str) -> None:
    engine = create_control_db_engine(control_db_url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_every_table_and_column_has_a_comment(control_db_url: str) -> None:
    engine = create_control_db_engine(control_db_url)
    inspector = inspect(engine)
    for table_name in inspector.get_table_names():
        if table_name == "alembic_version":
            continue
        assert inspector.get_table_comment(table_name)["text"], f"{table_name} has no table comment"
        for column in inspector.get_columns(table_name):
            assert column.get("comment"), f"{table_name}.{column['name']} has no comment"


def test_jsonb_is_used_for_json_columns(control_db_url: str) -> None:
    engine = create_control_db_engine(control_db_url)
    with engine.connect() as connection:
        data_type = connection.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='operations' AND column_name='plan'"
            )
        ).scalar_one()
    assert data_type == "jsonb"


def test_engine_parameter_family_source_is_required_and_defaults_to_aws(control_db_url: str) -> None:
    engine = create_control_db_engine(control_db_url)
    columns = {column["name"]: column for column in inspect(engine).get_columns("engine_parameter_families")}
    assert columns["source"]["nullable"] is False
    assert "aws" in str(columns["source"]["default"])
    assert columns["source"]["comment"] == "aws or bundled"


def test_blue_green_deployment_row_round_trips_through_the_migrated_table(
    session_factory: ControlDbSessionFactory,
) -> None:
    with session_factory() as session:
        operation = Operation(
            operation_kind="major_upgrade",
            state="RUNNING",
            environment_slug=None,
            plan={},
            idempotency_key="blue-green-round-trip",
            created_by_operator_name="phil",
        )
        session.add(operation)
        session.commit()
        session.add(
            BlueGreenDeployment(
                deployment_id="bgd-roundtrip0000001",
                operation_id=operation.id,
                resource_arn="arn:aws:rds:eu-west-2:000000000000:db:demo-orders",
                environment_slug="sandbox",
                target_key="sandbox/000000000000/eu-west-2",
                status="PROVISIONING",
                target_engine_version="17.2",
            )
        )
        session.commit()

    with session_factory() as session:
        stored_deployment = session.get(BlueGreenDeployment, "bgd-roundtrip0000001")
        assert stored_deployment is not None
        assert stored_deployment.operation_id == operation.id
        assert stored_deployment.status == "PROVISIONING"
        assert stored_deployment.target_engine_version == "17.2"
        assert stored_deployment.green_endpoint is None
        assert stored_deployment.switched_over_at_utc is None
        assert stored_deployment.deleted_at_utc is None
        assert stored_deployment.created_at_utc is not None
        assert stored_deployment.updated_at_utc is not None
