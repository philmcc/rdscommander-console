"""Write the OpenAPI document that the frontend client is generated from.

Usage: `uv run python -m rdscommander.export_openapi ../contracts/openapi.json`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from rdscommander.api.attention_routes import router as attention_router
from rdscommander.api.bootstrap_routes import router as bootstrap_router
from rdscommander.api.catalogue_routes import router as catalogue_router
from rdscommander.api.environment_routes import router as environment_router
from rdscommander.api.history_routes import router as history_router
from rdscommander.api.lifecycle_routes import router as lifecycle_router
from rdscommander.api.operation_routes import router as operation_router
from rdscommander.api.parameter_routes import router as parameter_router
from rdscommander.api.resource_routes import router as resource_router
from rdscommander.api.simulated_routes import router as simulated_router
from rdscommander.api.terraform_routes import router as terraform_router

APPLICATION_TITLE = "RDS Commander"
APPLICATION_VERSION = "0.4.0"
ALL_ROUTERS = (
    bootstrap_router,
    environment_router,
    simulated_router,
    operation_router,
    terraform_router,
    history_router,
    catalogue_router,
    resource_router,
    attention_router,
    parameter_router,
    lifecycle_router,
)


def build_openapi_document() -> dict[str, Any]:
    """Assemble the routers on a bare app and return its OpenAPI schema (no database needed)."""
    app = FastAPI(title=APPLICATION_TITLE, version=APPLICATION_VERSION)
    for router in ALL_ROUTERS:
        app.include_router(router)
    return app.openapi()


if __name__ == "__main__":
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_openapi_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output}")
