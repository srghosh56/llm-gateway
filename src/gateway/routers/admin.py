"""Admin API: tenants, API keys, model registry and audit log.

Every route requires an admin ``Principal`` (see auth.require_admin). Every mutation writes an
audit row in the same transaction as the change itself.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gateway import errors
from gateway.auth import Principal, generate_api_key, hash_api_key, require_admin
from gateway.db import ApiKey, AuditLog, Database, ModelEntry, Tenant
from gateway.quota import used_tokens
from gateway.registry import InvalidTransition, Status, audit, transition

router = APIRouter(prefix="/admin", tags=["admin"])

# Shared validation for user-chosen identifiers: lowercase slug, safe in URLs and logs.
NAME_PATTERN = r"^[a-z0-9][a-z0-9.:_-]*$"


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=NAME_PATTERN)
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


class ModelSubmit(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=NAME_PATTERN)
    backend: str
    upstream_model: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=100)


class ModelOut(BaseModel):
    name: str
    backend: str
    upstream_model: str
    owner: str
    status: Status
    created_at: datetime
    updated_at: datetime


class AuditOut(BaseModel):
    id: int
    actor: str
    action: str
    target_type: str
    target_id: str
    details: dict[str, object]
    created_at: datetime


def not_found(what: str) -> errors.APIError:
    return errors.APIError(404, f"{what} not found.", "invalid_request_error", "not_found")


def conflict(message: str) -> errors.APIError:
    return errors.APIError(409, message, "invalid_request_error", "conflict")


def _db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


# --- tenants & keys ---------------------------------------------------------------------


async def _tenant_out(request: Request, tenant: Tenant) -> TenantOut:
    async with _db(request).session() as session:
        used = await used_tokens(session, tenant.id)
    return TenantOut.model_validate(tenant, from_attributes=True).model_copy(
        update={"tokens_used_this_month": used}
    )


@router.post("/tenants", status_code=201)
async def create_tenant(
    body: TenantCreate, request: Request, actor: Principal = Depends(require_admin)
) -> TenantOut:
    async with _db(request).session() as session:
        tenant = Tenant(**body.model_dump())
        session.add(tenant)
        try:
            await session.flush()  # assigns tenant.id for the audit row
        except IntegrityError as exc:
            raise conflict(f"Tenant '{body.name}' already exists.") from exc
        audit(session, actor, "tenant.create", "tenant", tenant.id, **body.model_dump())
        await session.commit()
    return await _tenant_out(request, tenant)


@router.get("/tenants")
async def list_tenants(request: Request, _: Principal = Depends(require_admin)) -> list[TenantOut]:
    async with _db(request).session() as session:
        tenants = (await session.scalars(select(Tenant).order_by(Tenant.id))).all()
    return [await _tenant_out(request, t) for t in tenants]


@router.patch("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: int,
    body: TenantUpdate,
    request: Request,
    actor: Principal = Depends(require_admin),
) -> TenantOut:
    changes = body.model_dump(exclude_none=True)
    async with _db(request).session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise not_found("Tenant")
        for field, value in changes.items():
            setattr(tenant, field, value)
        audit(session, actor, "tenant.update", "tenant", tenant_id, **changes)
        await session.commit()
    return await _tenant_out(request, tenant)


@router.post("/tenants/{tenant_id}/keys", status_code=201)
async def create_api_key(
    tenant_id: int, request: Request, actor: Principal = Depends(require_admin)
) -> ApiKeyCreated:
    key = generate_api_key()
    async with _db(request).session() as session:
        if await session.get(Tenant, tenant_id) is None:
            raise not_found("Tenant")
        api_key = ApiKey(tenant_id=tenant_id, key_hash=hash_api_key(key), prefix=key[:10])
        session.add(api_key)
        await session.flush()
        # Audit the prefix only; the secret never goes anywhere but this response.
        audit(session, actor, "api_key.create", "api_key", api_key.id, prefix=api_key.prefix)
        await session.commit()
    return ApiKeyCreated(id=api_key.id, tenant_id=tenant_id, prefix=api_key.prefix, key=key)


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: int, request: Request, actor: Principal = Depends(require_admin)
) -> Response:
    async with _db(request).session() as session:
        api_key = await session.get(ApiKey, key_id)
        if api_key is None:
            raise not_found("API key")
        # Soft delete: keep the row so past usage/audit stays attributable.
        if api_key.revoked_at is None:
            api_key.revoked_at = datetime.now(UTC)
            audit(session, actor, "api_key.revoke", "api_key", key_id, prefix=api_key.prefix)
        await session.commit()
    return Response(status_code=204)


# --- model registry ---------------------------------------------------------------------


@router.post("/models", status_code=201)
async def submit_model(
    body: ModelSubmit, request: Request, actor: Principal = Depends(require_admin)
) -> ModelOut:
    if body.backend not in request.app.state.backends:
        raise errors.APIError(
            400, f"Unknown backend '{body.backend}'.", "invalid_request_error", param="backend"
        )
    async with _db(request).session() as session:
        model = ModelEntry(**body.model_dump(), status=Status.SUBMITTED)
        session.add(model)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise conflict(f"Model '{body.name}' already exists.") from exc
        audit(session, actor, "model.submit", "model", body.name, **body.model_dump())
        await session.commit()
        await session.refresh(model)  # load server-side timestamps
    return ModelOut.model_validate(model, from_attributes=True)


@router.get("/models")
async def list_models(
    request: Request,
    status: Status | None = None,
    _: Principal = Depends(require_admin),
) -> list[ModelOut]:
    stmt = select(ModelEntry).order_by(ModelEntry.name)
    if status is not None:
        stmt = stmt.where(ModelEntry.status == status)
    async with _db(request).session() as session:
        models = (await session.scalars(stmt)).all()
    return [ModelOut.model_validate(m, from_attributes=True) for m in models]


@router.post("/models/{name}/{action}")
async def change_model_status(
    name: str,
    action: Literal["approve", "deploy", "retire"],
    request: Request,
    actor: Principal = Depends(require_admin),
) -> ModelOut:
    async with _db(request).session() as session:
        # FOR UPDATE: two admins racing on the same model are serialised by Postgres, so
        # the transition check always sees the latest status.
        model = await session.scalar(
            select(ModelEntry).where(ModelEntry.name == name).with_for_update()
        )
        if model is None:
            raise not_found("Model")
        try:
            transition(session, model, action, actor)
        except InvalidTransition as exc:
            raise conflict(str(exc)) from exc
        await session.commit()
        await session.refresh(model)
    return ModelOut.model_validate(model, from_attributes=True)


# --- audit log --------------------------------------------------------------------------


@router.get("/audit")
async def list_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    target_type: str | None = None,
    _: Principal = Depends(require_admin),
) -> list[AuditOut]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if target_type is not None:
        stmt = stmt.where(AuditLog.target_type == target_type)
    async with _db(request).session() as session:
        rows = (await session.scalars(stmt)).all()
    return [AuditOut.model_validate(r, from_attributes=True) for r in rows]
