"""
Bluestar MCP Shared Admin Allowlist Middleware

Restricts an MCP server to a fixed list of administrators, identified by the
email claim of their Entra ID access token. Controlled by env vars:
- ADMIN_EMAILS: comma-separated allowlist; unset/empty disables the check
- ENTRA_TENANT_ID, ENTRA_AUDIENCE: required when ADMIN_EMAILS is set
"""
import logging
import os

import jwt
from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Scope, Receive, Send

logger = logging.getLogger(__name__)

FORBIDDEN_MESSAGE = "Forbidden — this server is restricted to administrators"
EMAIL_CLAIMS = ("preferred_username", "upn", "unique_name", "email")


class AdminAllowlistMiddleware:
    """
    Verifies the Entra ID bearer token (X-Forwarded-Authorization, else
    Authorization) and rejects any request whose email is not in ADMIN_EMAILS.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.admin_emails = frozenset(
            email.strip().lower()
            for email in os.environ.get("ADMIN_EMAILS", "").split(",")
            if email.strip()
        )
        if not self.admin_emails:
            logger.info("Admin allowlist disabled (ADMIN_EMAILS not set)")
            return

        tenant = os.environ.get("ENTRA_TENANT_ID", "").strip()
        self.audience = os.environ.get("ENTRA_AUDIENCE", "").strip()
        missing = [
            name
            for name, value in (("ENTRA_TENANT_ID", tenant), ("ENTRA_AUDIENCE", self.audience))
            if not value
        ]
        if missing:
            raise ValueError(
                f"ADMIN_EMAILS is set but {' and '.join(missing)} is missing — "
                "both ENTRA_TENANT_ID and ENTRA_AUDIENCE are required"
            )

        self.issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        self.jwks_client = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
            cache_keys=True,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.admin_emails:
            # Threadpool: a signing-key cache miss does a blocking JWKS fetch
            reason = await run_in_threadpool(self._denial_reason, scope)
            if reason is not None:
                logger.warning("Admin allowlist denied request: %s", reason)
                response = PlainTextResponse(FORBIDDEN_MESSAGE, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _denial_reason(self, scope: Scope) -> str | None:
        """Returns why the request is denied, or None if it is allowed."""
        token = _bearer_token(scope)
        if not token:
            return "missing bearer token"
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp"]},
            )
        except jwt.PyJWTError as exc:
            return f"invalid token ({type(exc).__name__}: {exc})"

        email = _token_email(claims)
        if not email:
            return "token has no email claim"
        if email not in self.admin_emails:
            return f"{email} is not in ADMIN_EMAILS"
        return None


def _bearer_token(scope: Scope) -> str:
    headers = dict(scope.get("headers", []))
    for name in (b"x-forwarded-authorization", b"authorization"):
        value = headers.get(name, b"").decode("latin-1").strip()
        if value[:7].lower() == "bearer ":
            value = value[7:].strip()
        if value:
            return value
    return ""


def _token_email(claims: dict) -> str:
    for claim in EMAIL_CLAIMS:
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""
