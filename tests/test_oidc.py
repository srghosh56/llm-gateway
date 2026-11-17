"""OIDC admin auth, tested with locally generated RSA keys and a fake JWKS endpoint."""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from gateway.auth import ChainAdminAuthenticator, StaticKeyAdminAuthenticator
from gateway.oidc import OIDCAdminAuthenticator
from tests.conftest import ADMIN_KEY

ISSUER = "http://keycloak.test/realms/llm-gateway"
AUDIENCE = "llm-gateway"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


class FakeIdP:
    """Holds signing keys and serves them as a JWKS; counts fetches to test caching."""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.fetches = 0
        self.rotate("key-1")

    def rotate(self, kid: str) -> None:
        self.keys = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048)}

    def jwks(self) -> dict[str, Any]:
        out = []
        for kid, key in self.keys.items():
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            out.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": out}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        self.fetches += 1
        return httpx.Response(200, json=self.jwks())

    def token(self, roles: list[str] | None = None, kid: str | None = None, **claims: Any) -> str:
        kid = kid or next(iter(self.keys))
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user-id-123",
            "preferred_username": "alice",
            "iat": now,
            "exp": now + 300,
            "realm_access": {"roles": roles if roles is not None else ["platform-admin"]},
            **claims,
        }
        return jwt.encode(payload, self.keys[kid], algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


def make_oidc(idp: FakeIdP, jwks_url: str | None = JWKS_URL) -> OIDCAdminAuthenticator:
    http = httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
    return OIDCAdminAuthenticator(
        ISSUER, AUDIENCE, jwks_url=jwks_url, http_client=http, min_refresh_seconds=0
    )


@pytest.fixture
def oidc_app(app: FastAPI, idp: FakeIdP) -> Iterator[FastAPI]:
    original = app.state.admin_auth
    app.state.admin_auth = ChainAdminAuthenticator(
        StaticKeyAdminAuthenticator(ADMIN_KEY), make_oidc(idp)
    )
    yield app
    app.state.admin_auth = original


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- through the API --------------------------------------------------------------------


async def test_admin_role_grants_access_and_is_audited(
    oidc_app: FastAPI, anon: httpx.AsyncClient, idp: FakeIdP
) -> None:
    r = await anon.post("/admin/tenants", json={"name": "sso"}, headers=bearer(idp.token()))
    assert r.status_code == 201
    audit = (await anon.get("/admin/audit", headers=bearer(idp.token()))).json()
    assert audit[0]["actor"] == "oidc:alice"


async def test_valid_token_without_admin_role_is_403(
    oidc_app: FastAPI, anon: httpx.AsyncClient, idp: FakeIdP
) -> None:
    r = await anon.get("/admin/tenants", headers=bearer(idp.token(roles=["viewer"])))
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"


async def test_static_key_still_works_alongside_oidc(
    oidc_app: FastAPI, anon: httpx.AsyncClient
) -> None:
    assert (await anon.get("/admin/tenants", headers=bearer(ADMIN_KEY))).status_code == 200


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 3600},  # expired (beyond the 30 s leeway)
        {"iss": "http://evil.test/realms/llm-gateway"},  # wrong issuer
        {"aud": "some-other-app"},  # token minted for another client
    ],
    ids=["expired", "wrong-issuer", "wrong-audience"],
)
async def test_invalid_claims_are_401(
    oidc_app: FastAPI, anon: httpx.AsyncClient, idp: FakeIdP, claims: dict[str, Any]
) -> None:
    r = await anon.get("/admin/tenants", headers=bearer(idp.token(**claims)))
    assert r.status_code == 401


async def test_tenant_api_key_is_not_an_admin_token(
    oidc_app: FastAPI, client: httpx.AsyncClient
) -> None:
    assert (await client.get("/admin/tenants")).status_code == 401


# --- authenticator (unit) ---------------------------------------------------------------


async def test_signature_from_unknown_key_is_rejected(idp: FakeIdP) -> None:
    attacker = FakeIdP()  # same kid, different private key
    assert await make_oidc(idp).authenticate(attacker.token()) is None


async def test_alg_none_and_hs256_confusion_are_rejected(idp: FakeIdP) -> None:
    oidc = make_oidc(idp)
    claims = {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300}
    unsigned = jwt.encode(claims, key=None, algorithm="none", headers={"kid": "key-1"})
    # HS256 "signed" with the public key as the HMAC secret: the classic key-confusion attack.
    # Built by hand, the way an attacker would (PyJWT itself refuses to create it).
    pem = (
        idp.keys["key-1"]
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    head = b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "key-1"}).encode())
    body = b64url(json.dumps(claims).encode())
    sig = b64url(hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest())
    hs256 = f"{head}.{body}.{sig}"
    assert await oidc.authenticate(unsigned) is None
    assert await oidc.authenticate(hs256) is None


async def test_non_jwt_tokens_are_ignored_without_fetching_keys(idp: FakeIdP) -> None:
    oidc = make_oidc(idp)
    assert await oidc.authenticate("gw_not-a-jwt") is None
    assert idp.fetches == 0


async def test_jwks_is_cached_and_refetched_on_key_rotation(idp: FakeIdP) -> None:
    oidc = make_oidc(idp)
    assert await oidc.authenticate(idp.token()) is not None
    assert await oidc.authenticate(idp.token()) is not None
    assert idp.fetches == 1  # second call served from cache

    idp.rotate("key-2")  # provider rotates its signing key
    principal = await oidc.authenticate(idp.token())
    assert principal is not None and principal.subject == "oidc:alice"
    assert idp.fetches == 2  # unknown kid triggered exactly one refetch


async def test_unknown_kid_refetch_is_rate_limited(idp: FakeIdP) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
    oidc = OIDCAdminAuthenticator(
        ISSUER, AUDIENCE, jwks_url=JWKS_URL, http_client=http, min_refresh_seconds=60
    )
    assert await oidc.authenticate(idp.token()) is not None
    for i in range(5):  # garbage kids must not each cause a fetch
        bogus = jwt.encode({"x": i}, idp.keys["key-1"], algorithm="RS256", headers={"kid": f"k{i}"})
        assert await oidc.authenticate(bogus) is None
    assert idp.fetches == 1


async def test_discovery_used_when_no_jwks_url(idp: FakeIdP) -> None:
    oidc = make_oidc(idp, jwks_url=None)
    assert await oidc.authenticate(idp.token()) is not None


async def test_idp_outage_keeps_cached_keys(idp: FakeIdP) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return idp.handler(request) if calls["n"] == 1 else httpx.Response(503)

    http = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    oidc = OIDCAdminAuthenticator(
        ISSUER,
        AUDIENCE,
        jwks_url=JWKS_URL,
        http_client=http,
        cache_seconds=0,
        min_refresh_seconds=0,
    )
    assert await oidc.authenticate(idp.token()) is not None
    assert await oidc.authenticate(idp.token()) is not None  # refresh failed, old keys used
    assert calls["n"] == 2
