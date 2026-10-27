"""Application factory.

``create_app()`` builds a fresh app from a ``Settings`` object. Tests call it with their own
settings and backends; production uses ``gateway.main:app`` via uvicorn.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.auth import StaticKeyAdminAuthenticator
from gateway.backends import Backend, MockBackend, OllamaBackend
from gateway.config import Settings, get_settings
from gateway.db import Database
from gateway.errors import install_error_handlers
from gateway.ratelimit import TokenBucketLimiter
from gateway.routers import admin, openai


def build_backends(settings: Settings) -> dict[str, Backend]:
    return {
        "mock": MockBackend(),
        "ollama": OllamaBackend(settings.ollama_base_url, settings.upstream_timeout_seconds),
    }


def create_app(
    settings: Settings | None = None, backends: dict[str, Backend] | None = None
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = Database(settings.database_url)
        app.state.backends = backends or build_backends(settings)
        yield
        for backend in app.state.backends.values():
            await backend.aclose()
        await app.state.db.dispose()

    app = FastAPI(title="llm-gateway", version="0.1.0", lifespan=lifespan)
    # State lives on the app instance (not module globals) so tests get isolated apps.
    app.state.settings = settings
    app.state.rate_limiter = TokenBucketLimiter()
    app.state.admin_auth = StaticKeyAdminAuthenticator(settings.admin_api_key)
    install_error_handlers(app)
    app.include_router(openai.router)
    app.include_router(admin.router)
    return app


def app() -> FastAPI:
    # Factory for uvicorn: `uvicorn --factory gateway.main:app`.
    return create_app()
