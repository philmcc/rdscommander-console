import json
from pathlib import Path

from rdscommander.export_openapi import build_openapi_document

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "openapi.json"


def test_committed_contract_matches_the_application() -> None:
    assert json.loads(CONTRACT.read_text()) == build_openapi_document(), (
        "run `uv run python -m rdscommander.export_openapi ../contracts/openapi.json` and commit"
    )


def test_contract_lists_every_route_of_this_plan() -> None:
    document = build_openapi_document()
    for path in (
        "/api/v1/catalogue",
        "/api/v1/operations",
        "/api/v1/resources/{resource_id}",
        "/api/v1/attention",
        "/api/v1/history/changes",
        "/api/v1/environments/compare",
        "/api/v1/environments/import/profiles",
        "/api/v1/parameters/families/{family}",
        "/api/v1/parameters/compare",
        "/api/v1/parameters/upgrade-preview",
        "/api/v1/lifecycle/instance-classes",
        "/api/v1/lifecycle/restore-window/{resource_id}",
    ):
        assert path in document["paths"], path


def test_contract_declares_the_application_version() -> None:
    assert build_openapi_document()["info"]["version"] == "0.5.0"


def test_package_version_matches_the_contract_version_the_bootstrap_route_reports() -> None:
    import rdscommander
    from rdscommander.export_openapi import APPLICATION_VERSION

    assert rdscommander.__version__ == APPLICATION_VERSION
