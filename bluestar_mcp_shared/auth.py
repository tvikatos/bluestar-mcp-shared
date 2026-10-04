"""
Bluestar MCP Shared Authentication Middleware

Supports two authentication modes controlled by AUTH_MODE env var:
- internal_token: validates X-Internal-Token header (personal GCP / development)
- passthrough: requires an Authorization header, no token validation (Ciena GCP / production)
"""
import os
from starlette.responses import Response
from starlette.types import ASGIApp, Scope, Receive, Send


class InternalTokenMiddleware:
    """Validates X-Internal-Token header against INTERNAL_TOKEN env var."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.token = os.environ.get("INTERNAL_TOKEN")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            token = headers.get(b"x-internal-token", b"").decode()
            if token != self.token:
                response = Response("Forbidden", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class PassthroughMiddleware:
    """
    Checks that an Authorization or X-Forwarded-Authorization header is
    present, without validating its contents. No token/signature checks.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            has_auth = b"authorization" in headers or b"x-forwarded-authorization" in headers
            if not has_auth:
                response = Response("Unauthorized — Authorization header required", status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def get_auth_middleware() -> type:
    """
    Returns the appropriate middleware class based on AUTH_MODE env var.
    AUTH_MODE=internal_token (default) → InternalTokenMiddleware
    AUTH_MODE=passthrough → PassthroughMiddleware
    Any other value raises ValueError.
    """
    mode = os.environ.get("AUTH_MODE", "internal_token")
    if mode == "internal_token":
        return InternalTokenMiddleware
    if mode == "passthrough":
        return PassthroughMiddleware
    raise ValueError(
        f"Unknown AUTH_MODE {mode!r} — expected 'internal_token' or 'passthrough'"
    )
