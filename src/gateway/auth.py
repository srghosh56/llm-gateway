"""Authentication for tenants (API keys) and admins (pluggable).

Tenant API keys
    Format ``gw_<43 url-safe chars>`` (256 bits of randomness). Only a SHA-256 hash is stored.
    A slow password hash (bcrypt/argon2) is unnecessary here: those protect *low-entropy*
    human passwords from brute force, while a 256-bit random key cannot be brute-forced, and
    a fast hash keeps per-request lookup cheap (indexed equality on the hash column).

Admins
    ``AdminAuthenticator`` is a small protocol that turns a bearer token into a ``Principal``.
    Today it is a static admin key; an OIDC/JWT implementation can be dropped in later without
    touching the endpoints, which only check ``Principal.roles``.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request
from sqlalchemy import select

from gateway import errors
from gateway.db import ApiKey, Database, Tenant
from gateway.metrics import TENANT_REJECTIONS, TenantLabeler
from gateway.observability import tenant_var
from gateway.quota import used_tokens
from gateway.ratelimit import TokenBucketLimiter

KEY_PREFIX = "gw_"
ADMIN_ROLE = "platform-admin"


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


@dataclass(frozen=True)
class TenantContext:
    """What the rest of the request needs to know about the caller (no ORM objects leak)."""

    id: int
    name: str


async def authenticate_tenant(request: Request) -> TenantContext:
    """FastAPI dependency: API key -> tenant, then rate limit and quota checks.

    Order matters: 401 (unknown key) before 403 (disabled tenant) before 429 (limits), so a
    caller with a bad key cannot probe a tenant's limits.
    """
    token = bearer_token(request)
    if token is None:
        raise errors.unauthorized("Missing bearer token in Authorization header.")

    db: Database = request.app.state.db
    # Short-lived session: we must not hold a pooled connection for the whole LLM call.
    async with db.session() as session:
        row = (
            await session.execute(
                select(ApiKey, Tenant)
                .join(Tenant)
                .where(ApiKey.key_hash == hash_api_key(token), ApiKey.revoked_at.is_(None))
            )
        ).first()
        if row is None:
            raise errors.unauthorized()
        tenant: Tenant = row.Tenant
        tenant_var.set(tenant.name)  # every later log line of this request carries the tenant
        label: TenantLabeler = request.app.state.tenant_labeler
        if not tenant.active:
            TENANT_REJECTIONS.labels(label(tenant.name), "disabled").inc()
            raise errors.forbidden("Tenant is disabled.")

        limiter: TokenBucketLimiter = request.app.state.rate_limiter
        allowed, retry_after = limiter.acquire(
            tenant.id, tenant.rate_limit_rpm, tenant.rate_limit_burst
        )
        if not allowed:
            TENANT_REJECTIONS.labels(label(tenant.name), "rate_limit").inc()
            raise errors.rate_limited(retry_after)

        if await used_tokens(session, tenant.id) >= tenant.monthly_token_quota:
            TENANT_REJECTIONS.labels(label(tenant.name), "quota").inc()
            raise errors.quota_exceeded()

    return TenantContext(id=tenant.id, name=tenant.name)


# --- admin -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]


class AdminAuthenticator(Protocol):
    async def authenticate(self, token: str) -> Principal | None:
        """Return the principal for a valid token, or None if the token is not recognised."""


class StaticKeyAdminAuthenticator:
    """Single shared admin key from config. If no key is configured, admin access is off."""

    def __init__(self, admin_key: str | None) -> None:
        self._key = admin_key

    async def authenticate(self, token: str) -> Principal | None:
        # compare_digest: constant-time comparison, avoids timing side channels.
        if self._key and hmac.compare_digest(token.encode(), self._key.encode()):
            return Principal(subject="admin-key", roles=frozenset({ADMIN_ROLE}))
        return None


async def require_admin(request: Request) -> Principal:
    """FastAPI dependency for /admin routes."""
    token = bearer_token(request)
    if token is None:
        raise errors.unauthorized("Missing bearer token in Authorization header.")
    authenticator: AdminAuthenticator = request.app.state.admin_auth
    principal = await authenticator.authenticate(token)
    if principal is None:
        raise errors.unauthorized("Invalid admin credentials.")
    if ADMIN_ROLE not in principal.roles:
        raise errors.forbidden(f"Requires role '{ADMIN_ROLE}'.")
    return principal
