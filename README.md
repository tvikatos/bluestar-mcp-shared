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

## Neo4j driver

**Every server must build its driver with `make_driver`** — never call `AsyncGraphDatabase.driver(...)` directly.

```python
from bluestar_mcp_shared.neo4j import make_driver, mark_commit_started, with_retry

driver = make_driver(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD)

records = await with_retry(run_query, cypher, params)
```

Why: Aura and the load balancer in front of it close idle connections without telling the client. With the driver's default pool settings (1-hour connection lifetime, no liveness check) the pool keeps those dead sockets and hands one to the next query, which fails; the retry then succeeds on a fresh connection. `make_driver` sets the pool up so dead connections are dropped before they are used:

| Setting | Default | Env override | Effect |
|---|---|---|---|
| `max_connection_lifetime` | 300 | `NEO4J_MAX_CONNECTION_LIFETIME` | connections are recycled after 5 minutes instead of 1 hour |
| `liveness_check_timeout` | 30 | `NEO4J_LIVENESS_CHECK_TIMEOUT` | a connection idle for more than 30 s is tested before it is handed out |
| `keep_alive` | true | `NEO4J_KEEP_ALIVE` | TCP keep-alive on the socket |
| `connection_acquisition_timeout` | 30 | `NEO4J_CONNECTION_ACQUISITION_TIMEOUT` | fail after 30 s rather than hang waiting for a connection |
| `max_transaction_retry_time` | 15 | `NEO4J_MAX_TRANSACTION_RETRY_TIME` | managed transactions retry for at most 15 s |

Times are in seconds. Precedence is keyword argument to `make_driver`, then the env var, then the default. Any other keyword argument is passed to the driver unchanged. An env value that does not parse raises `ValueError` at startup.

Requires `neo4j>=5.16,<6` (`liveness_check_timeout` does not exist before 5.16). This package declares that range itself, and each server pins the same range.

### Retrying

For the failures that still get through:

- `is_transient_neo4j_error(exc)` — true for `ServiceUnavailable`, `SessionExpired`, `TransientError`, and any exception whose message contains "defunct connection" or "Unable to retrieve routing information" (case-insensitive).
- `await with_retry(fn, *args, attempts=2, backoff=1.0, idempotent=True, **kwargs)` — awaits `fn(*args, **kwargs)`; on a transient error waits `backoff` seconds and tries again, up to `attempts` calls in total. Each retry is logged at WARNING. Any other error, and the last transient error, is re-raised.

A retry runs `fn` again from the start. If the connection dropped after a commit was applied but before its acknowledgement arrived, that applies the write twice. So:

| What `fn` does | How to wrap it |
|---|---|
| Reads | `with_retry(fn, ...)` |
| Writes that are safe to apply twice — `MERGE` by key, `SET` | `with_retry(fn, ...)` |
| A transaction containing `CREATE` or a counter increment | `with_retry(fn, ..., idempotent=False)`, with `mark_commit_started()` just before `tx.commit()` |

```python
async def write_tx():
    async with driver.session() as session:
        tx = await session.begin_transaction()
        try:
            await tx.run(create_cypher, params)
            mark_commit_started()
            await tx.commit()
        finally:
            await tx.close()

await with_retry(write_tx, idempotent=False)
```

With `idempotent=False`, a transient error is retried only if it arrived before `mark_commit_started()` was called (connection dropped on `begin`/`run`, nothing committed). From that call on the error is re-raised without a retry and logged at WARNING: the write may or may not have been applied, and the caller has to check. If `fn` never calls `mark_commit_started()`, `idempotent=False` gives no protection.

## Environment variables

| Variable | Used by | Description |
|---|---|---|
| `AUTH_MODE` | auth middleware | `internal_token` or `passthrough` |
| `INTERNAL_TOKEN` | auth middleware (`internal_token`) | shared secret for dev auth |
| `ADMIN_EMAILS` | admin allowlist | comma-separated admin emails (trimmed, case-insensitive); unset/empty disables the allowlist |
| `ENTRA_TENANT_ID` | admin allowlist | Entra tenant ID; required when `ADMIN_EMAILS` is set |
| `ENTRA_AUDIENCE` | admin allowlist | expected `aud` claim of the access token; required when `ADMIN_EMAILS` is set |
| `NEO4J_MAX_CONNECTION_LIFETIME`, `NEO4J_LIVENESS_CHECK_TIMEOUT`, `NEO4J_KEEP_ALIVE`, `NEO4J_CONNECTION_ACQUISITION_TIMEOUT`, `NEO4J_MAX_TRANSACTION_RETRY_TIME` | `make_driver` | optional overrides for the driver pool settings — see [Neo4j driver](#neo4j-driver) |

## Tests

```
uv run --group dev pytest
```
