# bluestar-mcp-shared

Shared authentication middleware for Bluestar MCP servers.

## Auth modes

Controlled by `AUTH_MODE` environment variable:

- `internal_token` (default) — validates `X-Internal-Token` header. Used for personal GCP / development with Cloudflare.
- `okta` — validates Okta Bearer JWT token. Used for Ciena GCP / production with API Gateway.

## Usage

```python
from bluestar_mcp_shared import get_auth_middleware

middleware = get_auth_middleware()
http_app.add_middleware(middleware)
```

## Environment variables

| Variable | Mode | Description |
|---|---|---|
| `AUTH_MODE` | both | `internal_token` or `okta` |
| `INTERNAL_TOKEN` | internal_token | shared secret for dev auth |
| `OKTA_ISSUER` | okta | Okta issuer URL |
| `OKTA_AUDIENCE` | okta | Okta client ID |
