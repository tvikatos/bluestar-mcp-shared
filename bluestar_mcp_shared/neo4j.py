"""
Bluestar MCP Shared Neo4j Driver Helpers

Aura and the load balancer in front of it close idle connections silently.
A driver built with default pool settings keeps those connections for an hour
and hands a dead socket to the next query, which then fails.

- make_driver: the only place drivers are built; recycles and liveness-checks
  pooled connections so dead sockets are not handed out
- is_transient_neo4j_error / with_retry: retry the failures that still get through
- mark_commit_started: lets with_retry(idempotent=False) refuse to re-run a
  transaction whose commit may already have been applied
"""
import asyncio
import contextvars
import logging
import os

from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

logger = logging.getLogger(__name__)

# Driver setting → (env var, default)
DRIVER_DEFAULTS = {
    "max_connection_lifetime": ("NEO4J_MAX_CONNECTION_LIFETIME", 300),
    "liveness_check_timeout": ("NEO4J_LIVENESS_CHECK_TIMEOUT", 30),
    "keep_alive": ("NEO4J_KEEP_ALIVE", True),
    "connection_acquisition_timeout": ("NEO4J_CONNECTION_ACQUISITION_TIMEOUT", 30),
    "max_transaction_retry_time": ("NEO4J_MAX_TRANSACTION_RETRY_TIME", 15),
}

TRANSIENT_EXCEPTIONS = (ServiceUnavailable, SessionExpired, TransientError)
TRANSIENT_MESSAGES = ("defunct connection", "unable to retrieve routing information")

# Commit flag of the with_retry(idempotent=False) attempt currently running
_attempt: contextvars.ContextVar[dict | None] = contextvars.ContextVar("_attempt", default=None)


def make_driver(uri: str, username: str, password: str, **overrides) -> AsyncDriver:
    """
    Builds the AsyncDriver. Each setting in DRIVER_DEFAULTS is taken from
    overrides, else its env var, else the default. Other overrides are passed
    to the driver unchanged.
    """
    settings = {}
    for name, (env_var, default) in DRIVER_DEFAULTS.items():
        raw = os.environ.get(env_var, "").strip()
        settings[name] = _parse_env(env_var, raw, default) if raw else default
    settings.update(overrides)
    return AsyncGraphDatabase.driver(uri, auth=(username, password), **settings)


def _parse_env(env_var: str, raw: str, default: bool | int) -> bool | float:
    if isinstance(default, bool):
        if raw.lower() in ("1", "true", "yes", "on"):
            return True
        if raw.lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"Invalid {env_var} {raw!r} — expected true or false")
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Invalid {env_var} {raw!r} — expected a number of seconds") from None


def is_transient_neo4j_error(exc: BaseException) -> bool:
    """True if the error is a dropped connection or other failure worth retrying."""
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    message = str(exc).lower()
    return any(text in message for text in TRANSIENT_MESSAGES)


def mark_commit_started() -> None:
    """
    Call immediately before tx.commit() in a function run by
    with_retry(idempotent=False). An error from that point on may mean the
    commit was applied, so the function is not run again. No-op elsewhere.
    """
    attempt = _attempt.get()
    if attempt is not None:
        attempt["commit_started"] = True


async def with_retry(
    fn, *args, attempts: int = 2, backoff: float = 1.0, idempotent: bool = True, **kwargs
):
    """
    Awaits fn(*args, **kwargs). On a transient Neo4j error waits backoff
    seconds and retries, up to attempts calls in total. Any other error, and
    the last transient error, is re-raised.

    idempotent=True (default) is for reads and for writes that are safe to
    apply twice (MERGE by key, SET). Use idempotent=False for a transaction
    that is not (CREATE, counter increments): fn must call
    mark_commit_started() just before tx.commit(), and a transient error is
    then retried only if it arrived before that call — never after, when the
    commit may have been applied and only its acknowledgement lost.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")
    for attempt in range(1, attempts + 1):
        # Idempotent calls get None so a nested read cannot set an outer transaction's flag
        state = None if idempotent else {"commit_started": False}
        token = _attempt.set(state)
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            if not is_transient_neo4j_error(exc):
                raise
            if state is not None and state["commit_started"]:
                logger.warning(
                    "Transient Neo4j error after commit was sent, not retrying — "
                    "the write may or may not have been applied: %s",
                    exc,
                )
                raise
            if attempt == attempts:
                raise
            logger.warning(
                "Transient Neo4j error (attempt %d of %d), retrying in %ss: %s",
                attempt, attempts, backoff, exc,
            )
        finally:
            _attempt.reset(token)
        await asyncio.sleep(backoff)
