"""Shared bearer-token authentication for inter-service calls.

Every service loads RFSN_SERVICE_TOKEN from env.  The middleware rejects any
request without a matching ``Authorization: Bearer <token>`` header.

This ensures that the executor is ONLY reachable via the tool_gateway, making
the gateway the true final authority on what runs.
"""
from __future__ import annotations

import os

from fastapi import Request  # type: ignore[import-not-found]
from fastapi.responses import JSONResponse  # type: ignore[import-not-found]
from starlette.middleware.base import (  # type: ignore[import-not-found]
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.types import ASGIApp  # type: ignore[import-not-found]

# Explicit dev mode only. In non-dev mode, token is mandatory.
RFSN_DEV_MODE: bool = os.getenv("RFSN_DEV_MODE", "0") == "1"
RFSN_SERVICE_TOKEN: str = os.getenv("RFSN_SERVICE_TOKEN", "")

if not RFSN_SERVICE_TOKEN and not RFSN_DEV_MODE:
    raise RuntimeError(
        "RFSN_SERVICE_TOKEN is required unless RFSN_DEV_MODE=1",
    )

# Paths that bypass auth (docs only)
_PUBLIC_PATHS = frozenset({
    "/docs", "/openapi.json",
})


def get_service_token() -> str:
    """Return configured service token (may be empty in dev mode)."""
    return RFSN_SERVICE_TOKEN


class ServiceAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid Bearer token.
    """

    def __init__(self, app: ASGIApp, *, token: str | None = None):
        super().__init__(app)
        self.token = token or RFSN_SERVICE_TOKEN

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        if not self.token:
            if RFSN_DEV_MODE:
                return await call_next(request)
            return JSONResponse(
                status_code=500,
                content={
                    "error": "server_misconfigured",
                    "detail": (
                        "RFSN_SERVICE_TOKEN is required"
                        " unless RFSN_DEV_MODE=1"
                    ),
                },
            )

        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {self.token}":
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={
                "error": "unauthorized",
                "detail": "missing or invalid service token",
            },
        )


def auth_headers() -> dict[str, str]:
    """Return headers dict to attach to outgoing inter-service HTTP calls."""
    if RFSN_SERVICE_TOKEN:
        return {"Authorization": f"Bearer {RFSN_SERVICE_TOKEN}"}
    if RFSN_DEV_MODE:
        return {}
    raise RuntimeError(
        "RFSN_SERVICE_TOKEN is required unless RFSN_DEV_MODE=1",
    )
