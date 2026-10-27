from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from gateway.backends import MockBackend
from gateway.config import Settings
from gateway.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(default_backend="mock", models=["mock-echo", "mock-error"])


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings, backends={"mock": MockBackend()})
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
