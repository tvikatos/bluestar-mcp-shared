"""
Bluestar MCP Shared Authentication Middleware

Supports two authentication modes controlled by AUTH_MODE env var:
- internal_token: validates X-Internal-Token header (personal GCP / development)
- okta: validates Okta Bearer JWT token (Ciena GCP / production)
"""
import os
import json
import urllib.request
from starlette.responses import Response
from starlette.types import ASGIApp, Scope, Receive, Send

try:
    import jwt
    HAS_JWT = True
except ImportError:
    HAS_JWT = False


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


class OktaMiddleware:
    """
    Validates Okta Bearer JWT token from Authorization header.
    Fetches JWKS from Okta to verify token signature.
    Extracts email and groups from token claims.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.issuer = os.environ.get("OKTA_ISSUER", "https://ciena.okta.com/oauth2/default")
        self.audience = os.environ.get("OKTA_AUDIENCE", "0oa28cfmbzbYy8NYZ0h8")
        self._jwks = None

    def _get_jwks(self):
        if self._jwks is None:
            jwks_url = f"{self.issuer}/v1/keys"
            with urllib.request.urlopen(jwks_url) as response:
                self._jwks = json.loads(response.read())
        return self._jwks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()

            if not auth_header.startswith("Bearer "):
                response = Response("Unauthorized — Bearer token required", status_code=401)
                await response(scope, receive, send)
                return

            token = auth_header[7:]

            try:
                if not HAS_JWT:
                    raise ImportError("pyjwt not installed")

                jwks = self._get_jwks()
                jwks_client = jwt.PyJWKClient.__new__(jwt.PyJWKClient)
                signing_key = jwt.PyJWKClient(f"{self.issuer}/v1/keys").get_signing_key_from_jwt(token)

                claims = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=["RS256"],
                    audience=self.audience,
                    issuer=self.issuer,
                )

                # Inject user identity into headers for downstream tools
                scope["state"] = scope.get("state", {})
                scope["state"]["user_email"] = claims.get("email", "")
                scope["state"]["user_groups"] = claims.get("Bluestar", [])

            except Exception as e:
                response = Response(f"Unauthorized — {str(e)}", status_code=401)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


def get_auth_middleware() -> type:
    """
    Returns the appropriate middleware class based on AUTH_MODE env var.
    AUTH_MODE=internal_token (default) → InternalTokenMiddleware
    AUTH_MODE=okta → OktaMiddleware
    """
    mode = os.environ.get("AUTH_MODE", "internal_token")
    if mode == "okta":
        return OktaMiddleware
    return InternalTokenMiddleware
