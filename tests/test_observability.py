import json
import logging
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY

from gateway.db import Database
from gateway.metrics import TenantLabeler
from gateway.observability import JsonFormatter, request_id_var, tenant_var
from tests.conftest import TenantHandle, make_tenant

CHAT = "/v1/chat/completions"
pytestmark = pytest.mark.usefixtures("models")


def sample(name: str, **labels: str) -> float:
    # Metrics are process-global, so tests compare before/after deltas.
    return REGISTRY.get_sample_value(name, labels) or 0.0


def ask(model: str = "mock-echo", stream: bool = False) -> dict[str, object]:
    return {"model": model, "messages": [{"role": "user", "content": "a b c"}], "stream": stream}


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def access_log() -> Iterator[ListHandler]:
    handler = ListHandler()
    logger = logging.getLogger("gateway.access")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


# --- probes -----------------------------------------------------------------------------


async def test_healthz_and_readyz(anon: httpx.AsyncClient) -> None:
    assert (await anon.get("/healthz")).json() == {"status": "ok"}
    r = await anon.get("/readyz")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


async def test_readyz_fails_without_database(app: FastAPI, anon: httpx.AsyncClient) -> None:
    healthy_db = app.state.db
    app.state.db = Database("postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    try:
        assert (await anon.get("/readyz")).status_code == 503
        assert (await anon.get("/healthz")).status_code == 200  # liveness unaffected
    finally:
        await app.state.db.dispose()
        app.state.db = healthy_db


# --- metrics ----------------------------------------------------------------------------


async def test_metrics_endpoint_exposes_gateway_metrics(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/metrics")
    assert r.status_code == 200
    for name in (
        "gateway_http_request_duration_seconds",
        "gateway_tenant_requests_total",
        "gateway_tenant_tokens_total",
        "gateway_tokens_per_second",
        "gateway_upstream_errors_total",
    ):
        assert name in r.text


async def test_completion_updates_tenant_metrics(client: httpx.AsyncClient) -> None:
    req = dict(tenant="acme", model="mock-echo")
    before = {
        "req": sample("gateway_tenant_requests_total", **req, outcome="success"),
        "prompt": sample("gateway_tenant_tokens_total", **req, kind="prompt"),
        "completion": sample("gateway_tenant_tokens_total", **req, kind="completion"),
        "tps": sample("gateway_tokens_per_second_count", model="mock-echo"),
    }
    assert (await client.post(CHAT, json=ask())).status_code == 200
    assert (await client.post(CHAT, json=ask(stream=True))).status_code == 200

    assert sample("gateway_tenant_requests_total", **req, outcome="success") == before["req"] + 2
    # "a b c" = 3 prompt tokens; "Echo: a b c" = 4 completion tokens; two requests
    assert sample("gateway_tenant_tokens_total", **req, kind="prompt") == before["prompt"] + 6
    assert (
        sample("gateway_tenant_tokens_total", **req, kind="completion") == before["completion"] + 8
    )
    assert sample("gateway_tokens_per_second_count", model="mock-echo") == before["tps"] + 2


async def test_streaming_records_time_to_first_token(client: httpx.AsyncClient) -> None:
    before = sample("gateway_time_to_first_token_seconds_count", model="mock-echo")
    await client.post(CHAT, json=ask(stream=True))
    assert sample("gateway_time_to_first_token_seconds_count", model="mock-echo") == before + 1


async def test_latency_histogram_uses_route_template(
    admin: httpx.AsyncClient, client: httpx.AsyncClient
) -> None:
    labels = dict(method="POST", route="/admin/models/{name}/{action}", status="409")
    before = sample("gateway_http_request_duration_seconds_count", **labels)
    await admin.post("/admin/models/mock-echo/approve")  # deployed -> approved is a 409
    assert sample("gateway_http_request_duration_seconds_count", **labels) == before + 1

    unmatched = dict(method="GET", route="unmatched", status="404")
    before = sample("gateway_http_request_duration_seconds_count", **unmatched)
    await client.get("/some/random/path/123")
    assert sample("gateway_http_request_duration_seconds_count", **unmatched) == before + 1


@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_errors_are_counted(client: httpx.AsyncClient, stream: bool) -> None:
    before = sample("gateway_upstream_errors_total", backend="mock", reason="mock_failure")
    outcome = dict(tenant="acme", model="mock-error", outcome="upstream_error")
    before_tenant = sample("gateway_tenant_requests_total", **outcome)
    r = await client.post(CHAT, json=ask("mock-error", stream=stream))
    assert r.status_code == 502
    assert sample("gateway_upstream_errors_total", backend="mock", reason="mock_failure") == (
        before + 1
    )
    assert sample("gateway_tenant_requests_total", **outcome) == before_tenant + 1


async def test_rejections_are_counted(app: FastAPI, admin: httpx.AsyncClient) -> None:
    t = await make_tenant(app, admin, name="tiny", rate_limit_rpm=1, rate_limit_burst=1)
    before = sample("gateway_tenant_rejections_total", tenant="tiny", reason="rate_limit")
    await t.client.post(CHAT, json=ask())
    await t.client.post(CHAT, json=ask())
    assert sample("gateway_tenant_rejections_total", tenant="tiny", reason="rate_limit") == (
        before + 1
    )


def test_tenant_label_cardinality_is_capped() -> None:
    label = TenantLabeler(max_tenants=2)
    assert [label(t) for t in ["a", "b", "c", "a", "d"]] == ["a", "b", "other", "a", "other"]


# --- request ids & structured logs ------------------------------------------------------


async def test_request_id_generated_and_returned(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/models")
    assert len(r.headers["x-request-id"]) == 32


async def test_request_id_propagated_or_replaced_if_unsafe(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/models", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = await client.get("/v1/models", headers={"X-Request-ID": "bad id\nwith newline"})
    assert r.headers["x-request-id"] != "bad id\nwith newline"


async def test_access_log_has_request_id_and_tenant(
    tenant: TenantHandle, access_log: ListHandler
) -> None:
    r = await tenant.client.post(CHAT, json=ask(), headers={"X-Request-ID": "trace-me"})
    record = next(rec for rec in access_log.records if rec.__dict__.get("route") == CHAT)
    line = json.loads(JsonFormatter().format(record))
    assert r.status_code == 200
    assert line["msg"] == "request"
    assert line["request_id"] == "trace-me"
    assert line["tenant"] == "acme"
    assert line["status"] == 200
    assert line["route"] == CHAT
    assert line["duration_ms"] >= 0


def test_json_formatter_includes_context() -> None:
    rid, ten = request_id_var.set("rid-1"), tenant_var.set("acme")
    try:
        record = logging.makeLogRecord({"msg": "hello", "levelname": "INFO", "extra_field": 7})
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(rid)
        tenant_var.reset(ten)
    assert line["msg"] == "hello"
    assert line["request_id"] == "rid-1"
    assert line["tenant"] == "acme"
    assert line["extra_field"] == 7
