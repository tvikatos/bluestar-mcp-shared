import asyncio
import logging

import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable, SessionExpired, TransientError

from bluestar_mcp_shared import neo4j as neo4j_module
from bluestar_mcp_shared.neo4j import (
    is_transient_neo4j_error,
    make_driver,
    mark_commit_started,
    with_retry,
)

ENV_VARS = [env_var for env_var, _ in neo4j_module.DRIVER_DEFAULTS.values()]


@pytest.fixture
def driver_calls(monkeypatch):
    """Replaces AsyncGraphDatabase.driver with a recorder; yields the calls made."""
    for env_var in ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    calls = []

    def fake_driver(uri, **kwargs):
        calls.append((uri, kwargs))
        return "driver"

    monkeypatch.setattr(neo4j_module.AsyncGraphDatabase, "driver", fake_driver)
    return calls


@pytest.fixture
def sleeps(monkeypatch):
    """Replaces asyncio.sleep so retries do not wait; yields the delays requested."""
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(neo4j_module.asyncio, "sleep", fake_sleep)
    return delays


class Stub:
    """Coroutine function that raises the given errors in turn, then returns 'ok'."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def test_make_driver_defaults(driver_calls):
    assert make_driver("neo4j+s://db", "user", "pass") == "driver"
    assert driver_calls == [
        (
            "neo4j+s://db",
            {
                "auth": ("user", "pass"),
                "max_connection_lifetime": 300,
                "liveness_check_timeout": 30,
                "keep_alive": True,
                "connection_acquisition_timeout": 30,
                "max_transaction_retry_time": 15,
            },
        )
    ]


def test_make_driver_env_overrides_defaults(driver_calls, monkeypatch):
    monkeypatch.setenv("NEO4J_MAX_CONNECTION_LIFETIME", "120")
    monkeypatch.setenv("NEO4J_LIVENESS_CHECK_TIMEOUT", " 7.5 ")
    monkeypatch.setenv("NEO4J_KEEP_ALIVE", "false")
    monkeypatch.setenv("NEO4J_CONNECTION_ACQUISITION_TIMEOUT", "")
    make_driver("neo4j+s://db", "user", "pass")
    settings = driver_calls[0][1]
    assert settings["max_connection_lifetime"] == 120
    assert settings["liveness_check_timeout"] == 7.5
    assert settings["keep_alive"] is False
    assert settings["connection_acquisition_timeout"] == 30
    assert settings["max_transaction_retry_time"] == 15


def test_make_driver_kwargs_override_env(driver_calls, monkeypatch):
    monkeypatch.setenv("NEO4J_MAX_CONNECTION_LIFETIME", "120")
    make_driver("neo4j+s://db", "user", "pass", max_connection_lifetime=60, max_connection_pool_size=10)
    settings = driver_calls[0][1]
    assert settings["max_connection_lifetime"] == 60
    assert settings["max_connection_pool_size"] == 10


@pytest.mark.parametrize(
    "env_var, value",
    [("NEO4J_MAX_CONNECTION_LIFETIME", "five minutes"), ("NEO4J_KEEP_ALIVE", "maybe")],
)
def test_make_driver_invalid_env_raises(driver_calls, monkeypatch, env_var, value):
    monkeypatch.setenv(env_var, value)
    with pytest.raises(ValueError, match=env_var):
        make_driver("neo4j+s://db", "user", "pass")
    assert driver_calls == []


def test_make_driver_builds_real_driver(monkeypatch):
    for env_var in ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)

    async def build_and_close():
        driver = make_driver("neo4j+s://localhost:7687", "user", "pass")
        await driver.close()

    asyncio.run(build_and_close())  # no connection is opened until a query runs


@pytest.mark.parametrize(
    "exc",
    [
        ServiceUnavailable("down"),
        SessionExpired("expired"),
        TransientError("deadlock"),
        RuntimeError("Failed to read from defunct connection IPv4Address(('db', 7687))"),
        OSError("DEFUNCT CONNECTION"),
        ValueError("Unable to retrieve routing information"),
    ],
)
def test_is_transient(exc):
    assert is_transient_neo4j_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ClientError("syntax error"),
        ValueError("bad input"),
        RuntimeError("connection refused"),
        KeyError("defunct"),
    ],
)
def test_is_not_transient(exc):
    assert is_transient_neo4j_error(exc) is False


def test_with_retry_returns_without_retry(sleeps, caplog):
    stub = Stub()
    with caplog.at_level(logging.WARNING, logger=neo4j_module.__name__):
        assert asyncio.run(with_retry(stub, "q", limit=5)) == "ok"
    assert stub.calls == [(("q",), {"limit": 5})]
    assert sleeps == []
    assert caplog.records == []


def test_with_retry_retries_transient_error(sleeps, caplog):
    stub = Stub(ServiceUnavailable("connection dropped"))
    with caplog.at_level(logging.WARNING, logger=neo4j_module.__name__):
        assert asyncio.run(with_retry(stub, "q", limit=5)) == "ok"
    assert stub.calls == [(("q",), {"limit": 5})] * 2
    assert sleeps == [1.0]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "connection dropped" in caplog.text


def test_with_retry_retries_on_message_match(sleeps):
    stub = Stub(RuntimeError("Failed to write to defunct connection"))
    assert asyncio.run(with_retry(stub)) == "ok"
    assert len(stub.calls) == 2


def test_with_retry_reraises_last_transient_error(sleeps, caplog):
    last = SessionExpired("second")
    stub = Stub(ServiceUnavailable("first"), last)
    with caplog.at_level(logging.WARNING, logger=neo4j_module.__name__):
        with pytest.raises(SessionExpired) as excinfo:
            asyncio.run(with_retry(stub))
    assert excinfo.value is last
    assert len(stub.calls) == 2
    assert sleeps == [1.0]
    assert len(caplog.records) == 1


def test_with_retry_does_not_retry_other_errors(sleeps, caplog):
    stub = Stub(ClientError("syntax error"))
    with caplog.at_level(logging.WARNING, logger=neo4j_module.__name__):
        with pytest.raises(ClientError):
            asyncio.run(with_retry(stub))
    assert len(stub.calls) == 1
    assert sleeps == []
    assert caplog.records == []


def test_with_retry_attempts_and_backoff(sleeps):
    stub = Stub(TransientError("a"), TransientError("b"), TransientError("c"))
    assert asyncio.run(with_retry(stub, attempts=4, backoff=0.25)) == "ok"
    assert len(stub.calls) == 4
    assert stub.calls[0] == ((), {})
    assert sleeps == [0.25, 0.25, 0.25]


def test_with_retry_single_attempt_does_not_retry(sleeps):
    stub = Stub(ServiceUnavailable("down"))
    with pytest.raises(ServiceUnavailable):
        asyncio.run(with_retry(stub, attempts=1))
    assert len(stub.calls) == 1
    assert sleeps == []


def test_with_retry_rejects_zero_attempts():
    stub = Stub()
    with pytest.raises(ValueError, match="attempts"):
        asyncio.run(with_retry(stub, attempts=0))
    assert stub.calls == []


class TxStub:
    """
    Stand-in for a write transaction function: each call takes the next step
    from the script — an error to raise before commit, an error to raise from
    commit, or None to commit successfully.
    """

    def __init__(self, *, before_commit=(), on_commit=()):
        self.before_commit = list(before_commit)
        self.on_commit = list(on_commit)
        self.runs = 0
        self.commits = 0

    async def __call__(self):
        self.runs += 1
        if self.before_commit:
            raise self.before_commit.pop(0)
        mark_commit_started()
        self.commits += 1
        if self.on_commit:
            raise self.on_commit.pop(0)
        return "committed"


def test_non_idempotent_retries_error_before_commit(sleeps):
    tx = TxStub(before_commit=[ServiceUnavailable("dropped on begin")])
    assert asyncio.run(with_retry(tx, idempotent=False)) == "committed"
    assert (tx.runs, tx.commits) == (2, 1)
    assert sleeps == [1.0]


def test_non_idempotent_does_not_retry_after_commit_started(sleeps, caplog):
    lost_ack = ServiceUnavailable("dropped during commit")
    tx = TxStub(on_commit=[lost_ack])
    with caplog.at_level(logging.WARNING, logger=neo4j_module.__name__):
        with pytest.raises(ServiceUnavailable) as excinfo:
            asyncio.run(with_retry(tx, attempts=5, idempotent=False))
    assert excinfo.value is lost_ack
    assert (tx.runs, tx.commits) == (1, 1)
    assert sleeps == []
    assert len(caplog.records) == 1
    assert "not retrying" in caplog.text
    assert "dropped during commit" in caplog.text


def test_non_idempotent_flag_is_per_attempt(sleeps):
    # First run fails before commit, second fails during commit: one retry, then stop
    tx = TxStub(before_commit=[SessionExpired("early")], on_commit=[ServiceUnavailable("late")])
    with pytest.raises(ServiceUnavailable):
        asyncio.run(with_retry(tx, attempts=5, idempotent=False))
    assert (tx.runs, tx.commits) == (2, 1)


def test_non_idempotent_reraises_other_errors(sleeps):
    tx = TxStub(before_commit=[ClientError("constraint violation")])
    with pytest.raises(ClientError):
        asyncio.run(with_retry(tx, idempotent=False))
    assert tx.runs == 1
    assert sleeps == []


def test_idempotent_retries_even_after_commit_started(sleeps):
    tx = TxStub(on_commit=[ServiceUnavailable("dropped during commit")])
    assert asyncio.run(with_retry(tx)) == "committed"
    assert (tx.runs, tx.commits) == (2, 2)


def test_nested_read_retry_does_not_clear_commit_flag(sleeps):
    reads = Stub(ServiceUnavailable("read dropped"))
    runs = []

    async def tx():
        runs.append(1)
        await with_retry(reads)
        mark_commit_started()
        await with_retry(Stub())
        raise ServiceUnavailable("dropped during commit")

    with pytest.raises(ServiceUnavailable, match="during commit"):
        asyncio.run(with_retry(tx, attempts=5, idempotent=False))
    assert len(runs) == 1
    assert len(reads.calls) == 2


def test_commit_mark_inside_nested_read_does_not_block_outer_retry(sleeps):
    runs = []

    async def inner():
        mark_commit_started()

    async def tx():
        runs.append(1)
        await with_retry(inner)
        if len(runs) == 1:
            raise ServiceUnavailable("dropped before commit")
        return "committed"

    assert asyncio.run(with_retry(tx, idempotent=False)) == "committed"
    assert len(runs) == 2


def test_mark_commit_started_outside_with_retry_is_noop():
    mark_commit_started()
