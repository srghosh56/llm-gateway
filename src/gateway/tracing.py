"""OpenTelemetry distributed tracing (exported over OTLP/HTTP, e.g. to Jaeger).

One trace per request: the FastAPI server span is the root; SQLAlchemy queries and outgoing
httpx calls to the model backend are child spans, and the W3C ``traceparent`` header is
propagated upstream. The gateway adds its own attributes to the server span (tenant, model,
token usage using the OpenTelemetry ``gen_ai.*`` conventions, request id), and every JSON log
line carries ``trace_id``/``span_id``, so logs, metrics and traces can be joined.

Off by default (``GATEWAY_OTEL_ENABLED``): without it nothing is instrumented, so there is no
overhead. The tracer provider is passed explicitly instead of being set globally, which keeps
test apps isolated from each other.
"""

from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.config import Settings

# Probes and scrapes would otherwise produce a trace every few seconds per replica.
EXCLUDED_URLS = "healthz,readyz,metrics"


def build_tracer_provider(settings: Settings) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": settings.otel_service_name, "service.version": "0.1.0"}
        ),
        # Parent-based: follow the caller's sampling decision if it sent a traceparent.
        sampler=ParentBasedTraceIdRatio(settings.otel_sample_ratio),
    )
    exporter = OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces")
    # Batch: export asynchronously in the background, never on the request path.
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def instrument_app(app: FastAPI, provider: TracerProvider) -> None:
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=EXCLUDED_URLS,
        # No extra span per ASGI send/receive: a stream would create one per token chunk.
        exclude_spans=["receive", "send"],
    )


def instrument_engine(engine: AsyncEngine, provider: TracerProvider) -> None:
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)


def uninstrument_engine() -> None:
    SQLAlchemyInstrumentor().uninstrument()


def annotate(**attributes: Any) -> None:
    """Set attributes on the current span (a no-op when tracing is off)."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attributes({k: v for k, v in attributes.items() if v is not None})


def add_event(name: str) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(name)


def current_ids() -> tuple[str | None, str | None]:
    """(trace_id, span_id) as hex for log correlation, or (None, None) outside a span."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
