import json

import structlog

from rdscommander.logging_setup import configure_structured_logging, get_logger


def test_logs_are_json_with_event_name_and_context(capsys) -> None:
    configure_structured_logging()
    get_logger("test").info("inventory_refresh_started", environment_slug="sandbox")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "inventory_refresh_started"
    assert payload["environment_slug"] == "sandbox"
    assert payload["level"] == "info"
    assert "timestamp" in payload
    structlog.reset_defaults()
