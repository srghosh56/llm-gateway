"""Application factory.

``create_app()`` builds a fresh app from a ``Settings`` object. Tests call it with their own
settings and backends; production uses ``gateway.main:app`` via uvicorn.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from gateway.auth import AdminAuthenticator, ChainAdminAuthenticator, StaticKeyAdminAuthenticator
from gateway.backends import Backend, MockBackend, OllamaBackend
from gateway.config import Settings, get_settings
from gateway.db import Database
from gateway.errors import install_error_handlers
from gateway.metrics import TenantLabeler
from gateway.observability import RequestContextMiddleware, configure_logging
from gateway.oidc import OIDCAdminAuthenticator
from gateway.ratelimit import TokenBucketLimiter
from gateway.routers import admin, health, openai
from gateway.tracing import (
    build_tracer_provider,
    instrument_app,
    instrument_engine,
    uninstrument_engine,
)


def build_backends(settings: Settings) -> dict[str, Backend]:
    return {
        "mock": MockBackend(),
        "ollama": OllamaBackend(settings.ollama_base_url, settings.upstream_timeout_seconds),
    }


def build_admin_auth(
    settings: Settings,
) -> tuple[AdminAuthenticator, OIDCAdminAuthenticator | None]:
    static = StaticKeyAdminAuthenticator(settings.admin_api_key)
    if not settings.oidc_issuer:
        return static, None
    oidc = OIDCAdminAuthenticator(
        settings.oidc_issuer, settings.oidc_audience, jwks_url=settings.oidc_jwks_url
    )
    return ChainAdminAuthenticator(static, oidc), oidc


def create_app(
    settings: Settings | None = None,
    backends: dict[str, Backend] | None = None,
    tracer_provider: TracerProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    # Tests inject an in-memory provider; production builds an OTLP one when enabled.
    owns_provider = tracer_provider is None and settings.otel_enabled
    provider = build_tracer_provider(settings) if owns_provider else tracer_provider

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = Database(settings.database_url)
        app.state.backends = backends or build_backends(settings)
        if provider is not None:
            instrument_engine(app.state.db.engine, provider)
            for backend in app.state.backends.values():
                backend.instrument(provider)
        yield
        if provider is not None:
            uninstrument_engine()
            if owns_provider:
                provider.shutdown()  # flush spans still buffered in the batch processor
        for backend in app.state.backends.values():
            await backend.aclose()
        if oidc is not None:
            await oidc.aclose()
        await app.state.db.dispose()

    app = FastAPI(title="llm-gateway", version="0.1.0", lifespan=lifespan)
    # State lives on the app instance (not module globals) so tests get isolated apps.
    app.state.settings = settings
    app.state.rate_limiter = TokenBucketLimiter()
    app.state.admin_auth, oidc = build_admin_auth(settings)
    app.state.tenant_labeler = TenantLabeler(settings.metrics_max_tenants)
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(openai.router)
    app.include_router(admin.router)
    app.include_router(health.router)
    if provider is not None:
        instrument_app(app, provider)
    return app


def app() -> FastAPI:
    # Factory for uvicorn: `uvicorn --factory gateway.main:app`.
    return create_app()
