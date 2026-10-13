"""Local-origin enforcement and CSRF token checks for a console bound to 127.0.0.1."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

CSRF_HEADER_NAME = "X-CSRF-Token"
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "testserver"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def generate_csrf_token() -> str:
    """A per-process random token handed out by `/bootstrap`."""
    return secrets.token_urlsafe(48)


def _hostname_is_local(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlsplit(value if "://" in value else f"http://{value}")
    return (parsed.hostname or "") in LOCAL_HOSTNAMES


class LocalOriginMiddleware(BaseHTTPMiddleware):
    """Reject any request whose Host or Origin is not local; check CSRF on mutating methods."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Apply the origin and CSRF checks before the route runs."""
        origin = request.headers.get("origin")
        if origin is not None and not _hostname_is_local(origin):
            return JSONResponse({"detail": "requests must come from this machine"}, status_code=403)
        if not _hostname_is_local(request.headers.get("host")):
            return JSONResponse({"detail": "console only serves local requests"}, status_code=403)
        if request.method in MUTATING_METHODS:
            expected = request.app.state.csrf_token
            provided = request.headers.get(CSRF_HEADER_NAME)
            if not provided or not secrets.compare_digest(provided, expected):
                return JSONResponse(
                    {"detail": "missing or invalid CSRF token; reload the console to fetch a new one"},
                    status_code=403,
                )
        return await call_next(request)


def require_csrf_token(request: Request) -> None:
    """Dependency form of the CSRF check for routes that want it explicit in OpenAPI."""
    provided = request.headers.get(CSRF_HEADER_NAME)
    if not provided or not secrets.compare_digest(provided, request.app.state.csrf_token):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
