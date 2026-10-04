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

## Environment variables

| Variable | Mode | Description |
|---|---|---|
| `AUTH_MODE` | both | `internal_token` or `passthrough` |
| `INTERNAL_TOKEN` | internal_token | shared secret for dev auth |
