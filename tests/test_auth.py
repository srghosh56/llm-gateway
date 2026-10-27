import httpx
from fastapi import FastAPI
from sqlalchemy import select

from gateway.auth import hash_api_key
from gateway.db import ApiKey, Database
from tests.conftest import TenantHandle, make_tenant

CHAT = "/v1/chat/completions"
MSG = {"model": "mock-echo", "messages": [{"role": "user", "content": "hi"}]}


async def test_missing_key_is_401(anon: httpx.AsyncClient) -> None:
    r = await anon.post(CHAT, json=MSG)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


async def test_wrong_key_is_401(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/v1/models", headers={"Authorization": "Bearer gw_nope"})
    assert r.status_code == 401


async def test_valid_key_works(client: httpx.AsyncClient) -> None:
    assert (await client.post(CHAT, json=MSG)).status_code == 200


async def test_key_is_stored_hashed_only(app: FastAPI, tenant: TenantHandle) -> None:
    db: Database = app.state.db
    async with db.session() as s:
        row = (await s.scalars(select(ApiKey))).one()
    assert row.key_hash == hash_api_key(tenant.key)
    assert tenant.key not in (row.key_hash, row.prefix)
    assert tenant.key.startswith(row.prefix)


async def test_revoked_key_is_401(admin: httpx.AsyncClient, tenant: TenantHandle) -> None:
    keys = await admin.post(f"/admin/tenants/{tenant.id}/keys")  # second key stays valid
    assert (await admin.delete("/admin/keys/1")).status_code == 204
    assert (await tenant.client.post(CHAT, json=MSG)).status_code == 401
    other = {"Authorization": f"Bearer {keys.json()['key']}"}
    assert (await tenant.client.post(CHAT, json=MSG, headers=other)).status_code == 200


async def test_disabled_tenant_is_403(admin: httpx.AsyncClient, tenant: TenantHandle) -> None:
    await admin.patch(f"/admin/tenants/{tenant.id}", json={"active": False})
    r = await tenant.client.post(CHAT, json=MSG)
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"


async def test_admin_routes_need_admin_key(
    anon: httpx.AsyncClient, client: httpx.AsyncClient
) -> None:
    assert (await anon.get("/admin/tenants")).status_code == 401
    # A tenant key is not an admin credential.
    assert (await client.get("/admin/tenants")).status_code == 401


async def test_admin_disabled_when_no_key_configured(app: FastAPI, anon: httpx.AsyncClient) -> None:
    from gateway.auth import StaticKeyAdminAuthenticator

    app.state.admin_auth = StaticKeyAdminAuthenticator(None)
    r = await anon.get("/admin/tenants", headers={"Authorization": "Bearer "})
    assert r.status_code == 401


async def test_key_shown_once_and_tenant_listing_has_no_secrets(
    app: FastAPI, admin: httpx.AsyncClient
) -> None:
    handle = await make_tenant(app, admin, name="beta")
    listing = (await admin.get("/admin/tenants")).text
    assert handle.key not in listing
    await handle.client.aclose()


async def test_duplicate_tenant_is_409(admin: httpx.AsyncClient, tenant: TenantHandle) -> None:
    r = await admin.post("/admin/tenants", json={"name": "acme"})
    assert r.status_code == 409
