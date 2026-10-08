import asyncio
import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from bluestar_mcp_shared import AdminAllowlistMiddleware
from bluestar_mcp_shared import admin as admin_module

TENANT = "11111111-2222-3333-4444-555555555555"
AUDIENCE = "api://bluestar-admin"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
KID = "test-key"
ADMIN = "admin@example.com"
FORBIDDEN = "Forbidden — this server is restricted to administrators"


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_clients(monkeypatch, private_key):
    """Replaces PyJWKClient with one serving a local JWKS; yields the instances created."""
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwks = {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}
    created = []

    class FakePyJWKClient:
        def __init__(self, uri, **kwargs):
            self.uri = uri
            self.kwargs = kwargs
            self.jwk_set = jwt.PyJWKSet.from_dict(jwks)
            created.append(self)

        def get_signing_key_from_jwt(self, token):
            kid = jwt.get_unverified_header(token).get("kid")
            for key in self.jwk_set.keys:
                if key.key_id == kid:
                    return key
            raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')

    monkeypatch.setattr(admin_module.jwt, "PyJWKClient", FakePyJWKClient)
    return created


@pytest.fixture
def env(monkeypatch, jwks_clients):
    monkeypatch.setenv("ADMIN_EMAILS", f" {ADMIN.upper()} , second.admin@example.com,")
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("ENTRA_AUDIENCE", AUDIENCE)


def make_token(key, **overrides):
    claims = {
        "aud": AUDIENCE,
        "iss": ISSUER,
        "exp": int(time.time()) + 300,
        "preferred_username": ADMIN,
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def request(headers=None):
    """Builds the middleware around a stub app and sends one GET; returns (status, body)."""

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = AdminAllowlistMiddleware(downstream)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], body.decode()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_unset_passes_everything(monkeypatch, jwks_clients, caplog):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
    monkeypatch.delenv("ENTRA_AUDIENCE", raising=False)
    with caplog.at_level(logging.INFO, logger=admin_module.__name__):
        assert request() == (200, "ok")
    assert caplog.messages == ["Admin allowlist disabled (ADMIN_EMAILS not set)"]
    assert jwks_clients == []


def test_empty_passes_everything(monkeypatch, jwks_clients):
    monkeypatch.setenv("ADMIN_EMAILS", " , ")
    assert request() == (200, "ok")


def test_valid_admin_passes(env, jwks_clients, private_key, caplog):
    with caplog.at_level(logging.INFO, logger=admin_module.__name__):
        assert request(bearer(make_token(private_key))) == (200, "ok")
    assert caplog.messages == []
    assert [c.uri for c in jwks_clients] == [JWKS_URL]


def test_email_is_trimmed_and_lowercased(env, private_key):
    token = make_token(private_key, preferred_username="  Admin@Example.COM ")
    assert request(bearer(token)) == (200, "ok")


@pytest.mark.parametrize("claim", ["upn", "unique_name", "email"])
def test_email_claim_fallbacks(env, private_key, claim):
    token = make_token(private_key, preferred_username=None, **{claim: ADMIN})
    assert request(bearer(token)) == (200, "ok")


def test_email_claim_order(env, private_key):
    token = make_token(private_key, preferred_username="user@example.com", email=ADMIN)
    assert request(bearer(token)) == (403, FORBIDDEN)


def test_valid_non_admin_forbidden(env, private_key, caplog):
    token = make_token(private_key, preferred_username="user@example.com")
    with caplog.at_level(logging.WARNING, logger=admin_module.__name__):
        assert request(bearer(token)) == (403, FORBIDDEN)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "user@example.com" in caplog.text
    assert token not in caplog.text


def test_no_email_claim_forbidden(env, private_key):
    token = make_token(private_key, preferred_username=None)
    assert request(bearer(token)) == (403, FORBIDDEN)


def test_expired_forbidden(env, private_key, caplog):
    token = make_token(private_key, exp=int(time.time()) - 300)
    with caplog.at_level(logging.WARNING, logger=admin_module.__name__):
        assert request(bearer(token)) == (403, FORBIDDEN)
    assert "ExpiredSignatureError" in caplog.text
    assert token not in caplog.text


def test_missing_exp_forbidden(env, private_key):
    assert request(bearer(make_token(private_key, exp=None))) == (403, FORBIDDEN)


def test_wrong_audience_forbidden(env, private_key):
    token = make_token(private_key, aud="api://something-else")
    assert request(bearer(token)) == (403, FORBIDDEN)


def test_wrong_issuer_forbidden(env, private_key):
    token = make_token(private_key, iss="https://login.microsoftonline.com/other-tenant/v2.0")
    assert request(bearer(token)) == (403, FORBIDDEN)


def test_bad_signature_forbidden(env, other_private_key):
    assert request(bearer(make_token(other_private_key))) == (403, FORBIDDEN)


def test_no_token_forbidden(env, caplog):
    with caplog.at_level(logging.WARNING, logger=admin_module.__name__):
        assert request() == (403, FORBIDDEN)
    assert "missing bearer token" in caplog.text


def test_malformed_token_forbidden(env):
    assert request({"Authorization": "Bearer not-a-jwt"}) == (403, FORBIDDEN)


def test_forwarded_authorization_preferred(env, private_key):
    admin_token = make_token(private_key)
    user_token = make_token(private_key, preferred_username="user@example.com")
    assert request(
        {"X-Forwarded-Authorization": f"Bearer {admin_token}", "Authorization": f"Bearer {user_token}"}
    ) == (200, "ok")
    assert request(
        {"X-Forwarded-Authorization": f"Bearer {user_token}", "Authorization": f"Bearer {admin_token}"}
    ) == (403, FORBIDDEN)


@pytest.mark.parametrize("missing", ["ENTRA_TENANT_ID", "ENTRA_AUDIENCE"])
def test_missing_entra_config_raises_at_startup(env, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        AdminAllowlistMiddleware(lambda scope, receive, send: None)
