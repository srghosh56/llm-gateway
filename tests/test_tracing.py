"""Tracing: spans, gateway attributes, DB/upstream child spans, traceparent propagation and
trace ids in logs. Uses an in-memory exporter instead of Jaeger."""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from asgi_lifespan import LifespanManager
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from gateway.backends import MockBackend, OllamaBackend
from gateway.config import Settings
from gateway.main import create_app
from gateway.observability import JsonFormatter
from tests.conftest import ADMIN_KEY, deploy_model, make_tenant
from tests.test_observability import ListHandler

CHAT = "/v1/chat/completions"


@dataclass
class Traced:
    exporter: InMemorySpanExporter
    admin: httpx.AsyncClient
    tenant: httpx.AsyncClient
    upstream_headers: list[httpx.Headers]

    def spans(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def server_span(self) -> ReadableSpan:
        return next(s for s in self.spans() if s.kind == SpanKind.SERVER and "chat" in s.name)


def fake_ollama(seen: list[httpx.Headers]) -> OllamaBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen2.5:0.5b",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        )

    backend = OllamaBackend("http://ollama/v1")
    backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama/v1"
    )
    return backend


@pytest.fixture
async def traced(settings: Settings) -> AsyncIterator[Traced]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    seen: list[httpx.Headers] = []
    backends = {"mock": MockBackend(), "ollama": fake_ollama(seen)}
    app = create_app(settings, backends=backends, tracer_provider=provider)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        admin = httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        await deploy_model(admin, "mock-echo")
        await deploy_model(admin, "qwen", upstream="qwen2.5:0.5b", backend="ollama")
        tenant = await make_tenant(app, admin, rate_limit_rpm=600, rate_limit_burst=100)
        exporter.clear()
        yield Traced(exporter, admin, tenant.client, seen)
        await tenant.client.aclose()
        await admin.aclose()


def ask(model: str = "mock-echo", stream: bool = False) -> dict[str, object]:
    return {"model": model, "messages": [{"role": "user", "content": "a b c"}], "stream": stream}


async def test_chat_server_span_has_gateway_attributes(traced: Traced) -> None:
    r = await traced.tenant.post(CHAT, json=ask(), headers={"X-Request-ID": "req-42"})
    assert r.status_code == 200
    attrs = traced.server_span().attributes or {}
    assert attrs["gateway.tenant"] == "acme"
    assert attrs["gen_ai.request.model"] == "mock-echo"
    assert attrs["gen_ai.usage.input_tokens"] == 3
    assert attrs["gen_ai.usage.output_tokens"] == 4
    assert attrs["gateway.outcome"] == "success"
    assert attrs["gateway.request_id"] == "req-42"


async def test_streaming_span_covers_stream_and_marks_first_token(traced: Traced) -> None:
    r = await traced.tenant.post(CHAT, json=ask(stream=True))
    assert r.text.endswith("data: [DONE]\n\n")
    span = traced.server_span()
    assert (span.attributes or {})["gen_ai.usage.output_tokens"] == 4  # set after last chunk
    assert [e.name for e in span.events] == ["first_token"]
    # No per-chunk "http send" spans (they were excluded on purpose).
    assert not [s for s in traced.spans() if "http send" in s.name]


async def test_db_queries_are_child_spans_of_the_request(traced: Traced) -> None:
    await traced.tenant.post(CHAT, json=ask())
    server = traced.server_span()
    db_spans = [s for s in traced.spans() if (s.attributes or {}).get("db.system") == "postgresql"]
    assert db_spans, "expected SQLAlchemy spans"
    assert all(s.context.trace_id == server.context.trace_id for s in db_spans)


async def test_upstream_call_is_traced_and_propagates_traceparent(traced: Traced) -> None:
    r = await traced.tenant.post(CHAT, json=ask("qwen"))
    assert r.status_code == 200
    server = traced.server_span()
    client = next(s for s in traced.spans() if s.kind == SpanKind.CLIENT)
    assert client.context.trace_id == server.context.trace_id
    # W3C header sent to Ollama carries the same trace id.
    traceparent = traced.upstream_headers[-1]["traceparent"]
    assert traceparent.split("-")[1] == format(server.context.trace_id, "032x")


async def test_probes_are_not_traced(traced: Traced) -> None:
    await traced.tenant.get("/healthz")
    await traced.tenant.get("/metrics")
    assert traced.spans() == []


async def test_access_log_carries_trace_id(traced: Traced) -> None:
    handler = ListHandler()
    logging.getLogger("gateway.access").addHandler(handler)
    try:
        await traced.tenant.post(CHAT, json=ask())
    finally:
        logging.getLogger("gateway.access").removeHandler(handler)
    record = next(r for r in handler.records if r.__dict__.get("route") == CHAT)
    line = json.loads(JsonFormatter().format(record))
    assert line["trace_id"] == format(traced.server_span().context.trace_id, "032x")


async def test_tracing_disabled_by_default(settings: Settings) -> None:
    app = create_app(settings, backends={"mock": MockBackend()})
    assert not settings.otel_enabled
    # No OpenTelemetry middleware installed when tracing is off.
    assert not any("OpenTelemetry" in str(m.cls) for m in app.user_middleware)
