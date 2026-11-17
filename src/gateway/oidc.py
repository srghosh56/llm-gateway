"""OIDC admin authentication: validate JWT access tokens issued by Keycloak (or any OIDC
provider) against the issuer's published signing keys (JWKS).

What is checked, and why:
* signature, with keys fetched from the issuer's JWKS endpoint (never from the token itself);
* algorithm on an allow-list (RS256 by default), which blocks "alg: none" and the classic
  RS256 -> HS256 key-confusion attack;
* ``exp`` (with small clock-skew leeway), ``iss`` (exact match) and ``aud`` (this gateway),
  so a token minted for another application is rejected even if signed by the same realm.

Roles come from Keycloak's ``realm_access.roles`` claim; the realm role ``platform-admin``
is the same name the endpoints check (auth.ADMIN_ROLE), so the mapping is 1:1.

JWKS caching: keys are cached for ``cache_seconds``. A token with an unknown ``kid`` (the
provider rotated its keys) triggers a refetch, but at most once per ``min_refresh_seconds``,
so random ``kid`` values can't be used to hammer the identity provider.
"""

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt

from gateway.auth import Principal

log = logging.getLogger("gateway.oidc")


class OIDCAdminAuthenticator:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        algorithms: tuple[str, ...] = ("RS256",),
        cache_seconds: float = 300.0,
        min_refresh_seconds: float = 30.0,
        leeway_seconds: float = 30.0,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        # In docker/k8s the issuer URL (what browsers see) often differs from the URL the
        # gateway can reach, so the JWKS URL can be set explicitly; otherwise use discovery.
        self._jwks_url = jwks_url
        self._http = http_client or httpx.AsyncClient(timeout=5.0)
        self._algorithms = list(algorithms)
        self._cache_seconds = cache_seconds
        self._min_refresh = min_refresh_seconds
        self._leeway = leeway_seconds
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = float("-inf")
        self._lock = asyncio.Lock()

    async def authenticate(self, token: str) -> Principal | None:
        if token.count(".") != 2:
            return None  # not a JWT (e.g. the static admin key); let other authenticators try
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        if header.get("alg") not in self._algorithms:
            return None
        key = await self._signing_key(header.get("kid"))
        if key is None:
            return None
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            log.info("rejected admin token", extra={"reason": type(exc).__name__})
            return None
        roles = claims.get("realm_access", {}).get("roles", [])
        subject = claims.get("preferred_username") or claims.get("sub", "unknown")
        # "oidc:" prefix makes audit rows show which mechanism authenticated the actor.
        return Principal(subject=f"oidc:{subject}", roles=frozenset(map(str, roles)))

    async def _signing_key(self, kid: str | None) -> jwt.PyJWK | None:
        if kid is None:
            return None
        now = time.monotonic()
        stale = now - self._fetched_at > self._cache_seconds
        unknown_kid = kid not in self._keys and now - self._fetched_at > self._min_refresh
        if stale or unknown_kid:
            async with self._lock:
                # Re-check under the lock: a concurrent request may have refreshed already.
                if time.monotonic() - self._fetched_at > self._min_refresh:
                    await self._refresh()
        return self._keys.get(kid)

    async def _refresh(self) -> None:
        try:
            url = self._jwks_url or await self._discover_jwks_url()
            response = await self._http.get(url)
            response.raise_for_status()
            keys = {}
            for jwk in response.json().get("keys", []):
                # Only signing keys for the algorithms we accept (skip encryption keys).
                if jwk.get("use", "sig") == "sig" and jwk.get("kid"):
                    try:
                        keys[jwk["kid"]] = jwt.PyJWK(jwk)
                    except jwt.PyJWTError:
                        continue
            self._keys = keys
        except (httpx.HTTPError, ValueError) as exc:
            # Keep the old keys: a brief IdP outage shouldn't lock admins out.
            log.warning("JWKS refresh failed", extra={"error": type(exc).__name__})
        finally:
            self._fetched_at = time.monotonic()

    async def _discover_jwks_url(self) -> str:
        response = await self._http.get(f"{self._issuer}/.well-known/openid-configuration")
        response.raise_for_status()
        url: str = response.json()["jwks_uri"]
        self._jwks_url = url
        return url

    async def aclose(self) -> None:
        await self._http.aclose()
