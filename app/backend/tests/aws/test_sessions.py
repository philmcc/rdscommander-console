import boto3
import pytest
from botocore.config import Config

from rdscommander.aws.sessions import AwsSessionFactory
from rdscommander.control_db.models import ExecutionRecord
from rdscommander.execution_records.service import ExecutionRecorder


def test_simulated_target_uses_endpoint_and_dummy_credentials(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    settings = operator_settings.model_copy(update={"simulated_aws_endpoint_url": "http://127.0.0.1:5000"})
    factory = AwsSessionFactory(settings, ExecutionRecorder(session_factory))
    client = factory.client(console_config.target("sandbox/000000000000/eu-west-2"), "rds")
    assert client.meta.endpoint_url == "http://127.0.0.1:5000"
    assert client.meta.region_name == "eu-west-2"


def test_every_call_is_recorded_with_read_calls_storing_metadata_only(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    factory = AwsSessionFactory(operator_settings, ExecutionRecorder(session_factory))
    target = console_config.target("sandbox/000000000000/eu-west-2")
    client = factory.client(target, "rds", step_name="probe")
    client.create_db_parameter_group(
        DBParameterGroupName="pg1", DBParameterGroupFamily="postgres16", Description="d"
    )
    client.describe_db_parameter_groups()
    with session_factory() as session:
        rows = session.query(ExecutionRecord).order_by(ExecutionRecord.id).all()
    assert [r.api_name for r in rows] == ["rds:CreateDBParameterGroup", "rds:DescribeDBParameterGroups"]
    assert rows[0].request_parameters["DBParameterGroupName"] == "pg1"
    assert "DBParameterGroup" in rows[0].response_payload
    assert set(rows[1].response_payload) == {"HTTPStatusCode", "RequestId"}
    assert all(r.target_key == target.target_key and r.step_name == "probe" and r.succeeded for r in rows)


def test_failed_calls_are_recorded_as_failures(
    session_factory, operator_settings, console_config, moto_aws
) -> None:
    factory = AwsSessionFactory(operator_settings, ExecutionRecorder(session_factory))
    client = factory.client(console_config.target("sandbox/000000000000/eu-west-2"), "rds")
    try:
        client.describe_db_instances(DBInstanceIdentifier="missing")
    except client.exceptions.DBInstanceNotFoundFault:
        pass
    with session_factory() as session:
        [row] = session.query(ExecutionRecord).all()
    assert row.succeeded is False and "DBInstanceNotFound" in (row.error_message or "")


def test_real_target_builds_a_profile_session(
    operator_settings, console_config, session_factory, monkeypatch
) -> None:
    captured = {}
    real_session_class = boto3.Session

    class FakeSession:
        def __init__(self, profile_name=None, region_name=None):
            captured["profile_name"] = profile_name
            captured["region_name"] = region_name

        def client(self, service_name, config=None):
            captured["service_name"] = service_name
            return real_session_class(
                region_name="eu-west-2", aws_access_key_id="x", aws_secret_access_key="y"
            ).client(service_name)

    monkeypatch.setattr("rdscommander.aws.sessions.boto3.Session", FakeSession)
    AwsSessionFactory(operator_settings, ExecutionRecorder(session_factory)).client(
        console_config.target("production/123456789012/eu-west-2"), "rds"
    )
    assert captured == {"profile_name": "testco-prod", "region_name": "eu-west-2", "service_name": "rds"}


SIMULATOR_INTERNAL_ERROR_PAGE = (
    b"<!doctype html>\n<html lang=en>\n<title>500 Internal Server Error</title>\n"
    b"<h1>Internal Server Error</h1>\n<p>The server encountered an internal error and was unable to "
    b"complete your request. Either the server is overloaded or there is an error in the application.</p>\n"
)


class _RawBody:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def stream(self, **_: object):
        yield self.body


def _answer_every_call_with(client, status_code: int, body: bytes) -> None:
    """Stand in for the HTTP layer: every call gets this answer, as the simulator container gives it."""
    from botocore.awsrequest import AWSResponse

    def answer(request, **_: object) -> AWSResponse:
        return AWSResponse(request.url, status_code, {}, _RawBody(body))

    client.meta.events.register("before-send", answer)


def test_simulated_client_names_the_action_the_simulator_server_has_not_implemented(
    session_factory, operator_settings, console_config
) -> None:
    """The simulator container answers an unimplemented action with a bare HTML 500 page."""
    from rdscommander.aws.rds_reader import RdsReader, SimulatorUnsupportedAction

    settings = operator_settings.model_copy(
        update={"simulated_aws_endpoint_url": "http://simulated-aws:5000"}
    )
    factory = AwsSessionFactory(settings, ExecutionRecorder(session_factory))
    client = factory.client(console_config.target("sandbox/000000000000/eu-west-2"), "rds")
    _answer_every_call_with(client, 500, SIMULATOR_INTERNAL_ERROR_PAGE)
    reader = RdsReader(client)
    assert reader.list_pending_maintenance_actions() == []
    with pytest.raises(SimulatorUnsupportedAction):
        reader.describe_engine_default_parameters("postgres16", "instance")
    with pytest.raises(SimulatorUnsupportedAction):
        reader.list_engine_versions("postgres")
    with pytest.raises(Exception, match="reset_db_parameter_group action has not been implemented"):
        client.reset_db_parameter_group(
            DBParameterGroupName="g", Parameters=[{"ParameterName": "work_mem", "ApplyMethod": "immediate"}]
        )
    with session_factory() as session:
        recorded = (
            session.query(ExecutionRecord).filter_by(api_name="rds:DescribePendingMaintenanceActions").all()
        )
        assert recorded, "the failed call is recorded"
        assert all(
            not row.succeeded and "has not been implemented" in (row.error_message or "") for row in recorded
        )


def test_real_client_leaves_an_html_error_page_as_an_error(
    operator_settings, console_config, session_factory, monkeypatch
) -> None:
    """Only the simulated account's internal-error page means "not implemented"; a real one stays an error."""
    from botocore.parsers import ResponseParserError

    from rdscommander.aws.rds_reader import RdsReader

    real_session_class = boto3.Session

    class FakeSession:
        def __init__(self, profile_name=None, region_name=None):
            pass

        def client(self, service_name, config=None):
            return real_session_class(
                region_name="eu-west-2", aws_access_key_id="x", aws_secret_access_key="y"
            ).client(service_name, config=Config(retries={"max_attempts": 1}))

    monkeypatch.setattr("rdscommander.aws.sessions.boto3.Session", FakeSession)
    client = AwsSessionFactory(operator_settings, ExecutionRecorder(session_factory)).client(
        console_config.target("production/123456789012/eu-west-2"), "rds"
    )
    _answer_every_call_with(client, 500, SIMULATOR_INTERNAL_ERROR_PAGE)
    with pytest.raises(ResponseParserError):
        RdsReader(client).list_pending_maintenance_actions()
