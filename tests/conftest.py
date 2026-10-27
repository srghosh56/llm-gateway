"""Shared fixtures.

Tests run against a real PostgreSQL (the quota upsert is Postgres-specific, and testing on
SQLite would hide exactly that kind of difference). The schema is created once per session
by running the Alembic migrations, so the migrations themselves are under test too.

    docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gateway_test \
        postgres:16-alpine
"""

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.backends import MockBackend
from gateway.config import Settings
from gateway.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:55432/gateway_test"
)
ADMIN_KEY = "test-admin-key"


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> Iterator[None]:
    os.environ["GATEWAY_DATABASE_URL"] = TEST_DATABASE_URL
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
async def clean_tables() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename <> 'alembic_version'"
                )
            )
        ).scalars()
        await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        admin_api_key=ADMIN_KEY,
        default_backend="mock",
        models=["mock-echo", "mock-error"],
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings, backends={"mock": MockBackend()})
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def anon(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Client without credentials."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


@dataclass
class TenantHandle:
    id: int
    key: str
    client: httpx.AsyncClient


async def make_tenant(
    app: FastAPI, admin: httpx.AsyncClient, name: str = "acme", **limits: int
) -> TenantHandle:
    r = await admin.post("/admin/tenants", json={"name": name, **limits})
    assert r.status_code == 201, r.text
    tenant_id = r.json()["id"]
    r = await admin.post(f"/admin/tenants/{tenant_id}/keys")
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {key}"},
    )
    return TenantHandle(tenant_id, key, client)


@pytest.fixture
async def tenant(app: FastAPI, admin: httpx.AsyncClient) -> AsyncIterator[TenantHandle]:
    handle = await make_tenant(app, admin, rate_limit_rpm=6000, rate_limit_burst=1000)
    yield handle
    await handle.client.aclose()


@pytest.fixture
def client(tenant: TenantHandle) -> httpx.AsyncClient:
    """Authenticated tenant client with generous limits."""
    return tenant.client
