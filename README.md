# bluestar-mcp-shared

Shared authentication middleware for Bluestar MCP servers.

## Auth modes

Controlled by `AUTH_MODE` environment variable:

- `internal_token` (default) — validates `X-Internal-Token` header. Used for personal GCP / development with Cloudflare.
- `passthrough` — requires an `Authorization` (or `X-Forwarded-Authorization`) header but does not validate it. Used for Ciena GCP / production, where API Gateway validates the Entra ID JWT.

Any other value raises `ValueError` at startup.

## Usage

```python
from bluestar_mcp_shared import get_auth_middleware

middleware = get_auth_middleware()
http_app.add_middleware(middleware)
```

## Admin allowlist

`AdminAllowlistMiddleware` restricts a server to a fixed list of administrators. It is independent of `AUTH_MODE` and is added alongside the auth middleware:

```python
from bluestar_mcp_shared import AdminAllowlistMiddleware, get_auth_middleware

http_app.add_middleware(get_auth_middleware())
http_app.add_middleware(AdminAllowlistMiddleware)
```

**It must be added to `mcp-kg-manager` and `mcp-ontology-manager` whenever they are exposed through Entra**, with `ADMIN_EMAILS` set. Without it, any user in the tenant who can obtain a token for the API can call the admin tools.

Behaviour, decided once at startup from the environment:

- `ADMIN_EMAILS` unset or empty — the allowlist is disabled and every request passes through. Logged once at INFO: `Admin allowlist disabled (ADMIN_EMAILS not set)`.
- `ADMIN_EMAILS` set but `ENTRA_TENANT_ID` or `ENTRA_AUDIENCE` missing — raises `ValueError` at startup.
- Otherwise every HTTP request must carry an Entra ID access token as a bearer token in `X-Forwarded-Authorization` (preferred) or `Authorization`. The token is verified with PyJWT: RS256 signature against the tenant's JWKS (`https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys`, keys cached), audience `ENTRA_AUDIENCE`, issuer `https://login.microsoftonline.com/<tenant>/v2.0`, and expiry.
- The caller's email is the first of the `preferred_username`, `upn`, `unique_name`, `email` claims, trimmed and lowercased.
- A missing or invalid token, or an email not in `ADMIN_EMAILS`, gets `403` with the plain-text body `Forbidden — this server is restricted to administrators`. The reason (and the email, when the token verified) is logged at WARNING; the token itself is never logged. Allowed requests are not logged.

## Environment variables

| Variable | Used by | Description |
|---|---|---|
| `AUTH_MODE` | auth middleware | `internal_token` or `passthrough` |
| `INTERNAL_TOKEN` | auth middleware (`internal_token`) | shared secret for dev auth |
| `ADMIN_EMAILS` | admin allowlist | comma-separated admin emails (trimmed, case-insensitive); unset/empty disables the allowlist |
| `ENTRA_TENANT_ID` | admin allowlist | Entra tenant ID; required when `ADMIN_EMAILS` is set |
| `ENTRA_AUDIENCE` | admin allowlist | expected `aud` claim of the access token; required when `ADMIN_EMAILS` is set |

## Tests

```
uv run --group dev pytest
```
