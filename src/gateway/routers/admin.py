"""Admin API: tenants and API keys. All routes require an admin principal."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gateway import errors
from gateway.auth import generate_api_key, hash_api_key, require_admin
from gateway.db import ApiKey, Database, Tenant
from gateway.quota import used_tokens

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    rate_limit_rpm: int = Field(default=60, gt=0)
    rate_limit_burst: int = Field(default=10, gt=0)
    monthly_token_quota: int = Field(default=1_000_000, ge=0)


class TenantUpdate(BaseModel):
    active: bool | None = None
    rate_limit_rpm: int | None = Field(default=None, gt=0)
    rate_limit_burst: int | None = Field(default=None, gt=0)
    monthly_token_quota: int | None = Field(default=None, ge=0)


class TenantOut(BaseModel):
    id: int
    name: str
    active: bool
    rate_limit_rpm: int
    rate_limit_burst: int
    monthly_token_quota: int
    tokens_used_this_month: int = 0


class ApiKeyCreated(BaseModel):
    id: int
    tenant_id: int
    prefix: str
    key: str = Field(description="The full key. Shown only once; it is not stored.")


def not_found(what: str) -> errors.APIError:
    return errors.APIError(404, f"{what} not found.", "invalid_request_error", "not_found")


def _db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


async def _tenant_out(request: Request, tenant: Tenant) -> TenantOut:
    async with _db(request).session() as session:
        used = await used_tokens(session, tenant.id)
    return TenantOut.model_validate(tenant, from_attributes=True).model_copy(
        update={"tokens_used_this_month": used}
    )


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantCreate, request: Request) -> TenantOut:
    async with _db(request).session() as session:
        tenant = Tenant(**body.model_dump())
        session.add(tenant)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise errors.APIError(
                409, f"Tenant '{body.name}' already exists.", "invalid_request_error", "conflict"
            ) from exc
    return await _tenant_out(request, tenant)


@router.get("/tenants")
async def list_tenants(request: Request) -> list[TenantOut]:
    async with _db(request).session() as session:
        tenants = (await session.scalars(select(Tenant).order_by(Tenant.id))).all()
    return [await _tenant_out(request, t) for t in tenants]


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: int, body: TenantUpdate, request: Request) -> TenantOut:
    async with _db(request).session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise not_found("Tenant")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(tenant, field, value)
        await session.commit()
    return await _tenant_out(request, tenant)


@router.post("/tenants/{tenant_id}/keys", status_code=201)
async def create_api_key(tenant_id: int, request: Request) -> ApiKeyCreated:
    key = generate_api_key()
    async with _db(request).session() as session:
        if await session.get(Tenant, tenant_id) is None:
            raise not_found("Tenant")
        api_key = ApiKey(tenant_id=tenant_id, key_hash=hash_api_key(key), prefix=key[:10])
        session.add(api_key)
        await session.commit()
    # The plaintext key exists only in this response.
    return ApiKeyCreated(id=api_key.id, tenant_id=tenant_id, prefix=api_key.prefix, key=key)


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: int, request: Request) -> Response:
    async with _db(request).session() as session:
        api_key = await session.get(ApiKey, key_id)
        if api_key is None:
            raise not_found("API key")
        # Soft delete: keep the row so past usage/audit stays attributable.
        api_key.revoked_at = api_key.revoked_at or datetime.now(UTC)
        await session.commit()
    return Response(status_code=204)
