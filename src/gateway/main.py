"""Application factory.

``create_app()`` builds a fresh app from a ``Settings`` object. Tests call it with their own
settings and backends; production uses ``gateway.main:app`` via uvicorn.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.backends import Backend, MockBackend, OllamaBackend
from gateway.config import Settings, get_settings
from gateway.errors import install_error_handlers
from gateway.routers import openai


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
        app.state.backends = backends or build_backends(settings)
        yield
        for backend in app.state.backends.values():
            await backend.aclose()

    app = FastAPI(title="llm-gateway", version="0.1.0", lifespan=lifespan)
    # State lives on the app instance (not module globals) so tests get isolated apps.
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(openai.router)
    return app


def app() -> FastAPI:
    # Factory for uvicorn: `uvicorn --factory gateway.main:app`.
    return create_app()
