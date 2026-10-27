# llm-gateway

A small, production-style, **multi-tenant, OpenAI-compatible LLM gateway** in Python.

Point any OpenAI client (SDK, LangChain, curl) at it, and it adds what a platform team needs
in front of self-hosted models: per-tenant API keys, rate limits and monthly token quotas, a
governed model registry with an audit trail, Prometheus metrics and structured logs.

It's deliberately small (~1,600 lines of application code, including comments) so every design decision can be read and
explained. Stack: Python 3.12, FastAPI, SQLAlchemy 2 (async) + Alembic, PostgreSQL, httpx,
prometheus_client, pytest. Models are served by [Ollama](https://ollama.com) (`qwen2.5:0.5b`
by default, small enough to run on a laptop CPU).

## Architecture

```
                    OpenAI SDK / curl / any OpenAI-compatible client
                                         |
                       Authorization: Bearer gw_...  (tenant key)
                                         v
 +-----------------------------------------------------------------------------------+
 |  llm-gateway (FastAPI)                                                            |
 |                                                                                   |
 |  RequestContextMiddleware  -> request id, JSON access log, latency histogram      |
 |        |                                                                          |
 |  authenticate_tenant       -> key hash lookup (401) -> tenant active? (403)       |
 |        |                      -> token bucket (429) -> monthly quota (429)        |
 |        v                                                                          |
 |  /v1/chat/completions      -> registry: name -> (backend, upstream id), deployed? |
 |  /v1/models                   |                                                   |
 |        |                      v                                                   |
 |        |              +---------------+     +-----------------+                   |
 |        +------------> | OllamaBackend | or  |   MockBackend   |  (Backend ABC)    |
 |                       +-------+-------+     +-----------------+                   |
 |  Meter: record tokens -> quota table, Prometheus counters, completion log line    |
 |                                                                                   |
 |  /admin/*  (admin key -> Principal{roles})  tenants, keys, models, audit log      |
 |  /metrics  /healthz  /readyz                                                      |
 +--------+-------------------------------------+---------------------+--------------+
          |                                     |                     |
          v                                     v                     ^ scrape
   +-------------+                     +-----------------+     +-------------+     +---------+
   | PostgreSQL  |                     | Ollama          |     | Prometheus  | --> | Grafana |
   | tenants     |                     | /v1 (OpenAI API)|     +-------------+     +---------+
   | api_keys    |                     | qwen2.5:0.5b    |
   | token_usage |                     +-----------------+
   | models      |
   | audit_log   |
   +-------------+
```

Model lifecycle (enforced in `src/gateway/registry.py`; only `deployed` is served):

```
submitted --approve--> approved --deploy--> deployed --retire--> retired (terminal)
    \______________________\____________retire__________________/
```

## Quick start (docker compose)

Requires Docker. The first start downloads the images and the ~400 MB model.

```bash
cp .env.example .env            # then change GATEWAY_ADMIN_API_KEY
docker compose up -d --build    # postgres, migrate, ollama, ollama-pull, gateway, prometheus, grafana
./scripts/bootstrap.sh          # deploys model "qwen-small", creates tenant "demo", prints its key
```

| What | URL |
|---|---|
| Gateway (OpenAPI docs at `/docs`) | http://localhost:8000 |
| Prometheus | http://localhost:9090 |
| Grafana, dashboard "LLM Gateway" (anonymous viewer; admin/admin) | http://localhost:3000 |

### Local development

```bash
uv sync
docker run -d --name llmgw-testdb -p 55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gateway_test postgres:16.10-alpine
uv run pytest                  # tests use the MockBackend, no Ollama needed
uv run ruff check . && uv run mypy
```

## API examples

The bootstrap script prints these two exports:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1
export OPENAI_API_KEY=gw_...
```

### curl

```bash
# list deployed models
curl -s $OPENAI_BASE_URL/models -H "Authorization: Bearer $OPENAI_API_KEY"

# chat completion
curl -s $OPENAI_BASE_URL/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "qwen-small", "messages": [{"role": "user", "content": "Hello!"}]}'

# streaming (Server-Sent Events)
curl -N $OPENAI_BASE_URL/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "qwen-small", "stream": true, "stream_options": {"include_usage": true},
       "messages": [{"role": "user", "content": "Count to 5"}]}'
```

### Official OpenAI Python client

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="gw_...")

resp = client.chat.completions.create(
    model="qwen-small",
    messages=[{"role": "user", "content": "In one sentence, what is an API gateway?"}],
)
print(resp.choices[0].message.content, resp.usage)

for chunk in client.chat.completions.create(
    model="qwen-small", messages=[{"role": "user", "content": "Count to 5"}], stream=True
):
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="")
```

Runnable version: `uv run python examples/openai_client.py`. Gateway errors come back in
OpenAI's error format, so the SDK raises its usual typed exceptions (`AuthenticationError`,
`PermissionDeniedError`, `RateLimitError`, `NotFoundError`).

### Admin API

All `/admin` routes need `Authorization: Bearer $GATEWAY_ADMIN_API_KEY`.

```bash
ADMIN="Authorization: Bearer $GATEWAY_ADMIN_API_KEY"; JSON="Content-Type: application/json"

# tenants and keys (the key is returned once; only its SHA-256 hash is stored)
curl -s -X POST localhost:8000/admin/tenants -H "$ADMIN" -H "$JSON" \
  -d '{"name": "team-a", "rate_limit_rpm": 60, "rate_limit_burst": 10, "monthly_token_quota": 500000}'
curl -s -X POST localhost:8000/admin/tenants/1/keys -H "$ADMIN"
curl -s -X PATCH localhost:8000/admin/tenants/1 -H "$ADMIN" -H "$JSON" -d '{"active": false}'
curl -s -X DELETE localhost:8000/admin/keys/1 -H "$ADMIN"

# model registry
curl -s -X POST localhost:8000/admin/models -H "$ADMIN" -H "$JSON" \
  -d '{"name": "qwen-small", "backend": "ollama", "upstream_model": "qwen2.5:0.5b", "owner": "ml-team"}'
curl -s -X POST localhost:8000/admin/models/qwen-small/approve -H "$ADMIN"
curl -s -X POST localhost:8000/admin/models/qwen-small/deploy  -H "$ADMIN"
curl -s -X POST localhost:8000/admin/models/qwen-small/retire  -H "$ADMIN"

# audit trail (newest first)
curl -s "localhost:8000/admin/audit?target_type=model&limit=20" -H "$ADMIN"
```

### Error codes

| Status | `error.code` | When |
|---|---|---|
| 400 | `invalid_request` | Request body fails validation |
| 401 | `invalid_api_key` | Missing, unknown or revoked key |
| 403 | `forbidden` | Tenant disabled; admin principal without the `platform-admin` role |
| 404 | `model_not_found` | Model unknown or not in `deployed` state |
| 409 | `conflict` | Invalid lifecycle transition, duplicate tenant/model name |
| 429 | `rate_limit_exceeded` | Token bucket empty (includes `Retry-After`) |
| 429 | `insufficient_quota` | Monthly token quota used up |
| 502 | `upstream_error` | Backend down, timed out or returned an error |

## Tests

75 pytest tests, 98% line coverage, run against a real PostgreSQL with the Alembic migrations
applied (no Ollama needed: tests use the `MockBackend`).

| Area | What is asserted |
|---|---|
| OpenAI API | non-stream and SSE stream shape, `[DONE]`, `include_usage`, 400/404/502 errors |
| OpenAI SDK | the official `openai` client: list models, completion, streaming, typed exceptions |
| Auth | 401 missing/wrong/revoked key, 403 disabled tenant, keys stored only as hashes, admin key required |
| Rate limit | token bucket burst/refill/cap (fake clock), per-tenant isolation, 429 + `Retry-After` |
| Quota | usage recorded for JSON and streamed responses, 429 `insufficient_quota`, raising quota unblocks |
| Registry | every (from, to) transition pair, lifecycle controls serving and listing, 409s, audit rows |
| Observability | metric deltas per tenant/model, route-template labels, upstream errors, label cap, request ids, JSON logs, probes |
| Backends | Ollama proxy against a fake upstream (`httpx.MockTransport`): payload, SSE parsing, error mapping |

## Observability

| Metric | Type | Labels |
|---|---|---|
| `gateway_http_request_duration_seconds` | histogram | method, route (template), status |
| `gateway_tokens_per_second` | histogram | model |
| `gateway_time_to_first_token_seconds` | histogram | model (streaming only) |
| `gateway_tenant_requests_total` | counter | tenant, model, outcome |
| `gateway_tenant_tokens_total` | counter | tenant, model, kind (prompt/completion) |
| `gateway_tenant_rejections_total` | counter | tenant, reason (rate_limit/quota/disabled) |
| `gateway_upstream_errors_total` | counter | backend, reason (timeout/connect/http_5xx/...) |

Logs are one JSON object per line, and every line carries `request_id` (taken from an incoming
`X-Request-ID` if it's safe, otherwise generated, and always echoed in the response header)
and `tenant`. `/healthz` is liveness (process up); `/readyz` is readiness (database reachable).

## Design decisions

- **OpenAI wire format end to end.** The request and response schemas are the OpenAI ones,
  with `extra="allow"` so unknown parameters are forwarded, not rejected. Any OpenAI client
  works unchanged, and the backends are thin. Errors use OpenAI's error envelope so SDKs map
  them to typed exceptions.
- **Backend interface (ABC) with two implementations.** `OllamaBackend` is a pooled `httpx`
  proxy; `MockBackend` is deterministic (echo, word-count "tokens"), so tests and CI need no
  GPU and token assertions are exact. A new provider is one subclass.
- **Streaming done carefully.** The first upstream chunk is fetched *before* the response
  starts, so "upstream down" is a clean 502 instead of a 200 that dies. Usage is always
  requested upstream for metering but only forwarded if the client asked
  (`stream_options.include_usage`), as in the OpenAI API. Accounting runs in a `finally` under
  `asyncio.shield`, so partial streams are still charged when a client disconnects.
- **API keys: 256-bit random, stored as SHA-256.** A slow hash (bcrypt/argon2) protects
  low-entropy passwords; a 256-bit random key can't be brute-forced, and a fast hash keeps
  per-request lookup a single indexed equality query. The plaintext is returned once.
- **Rate limiting: in-memory token bucket per tenant.** It allows bursts and enforces the
  average rate. It needs no lock because it never awaits (atomic on one event loop).
  Documented limitation: per-process.
- **Soft monthly quota.** Checked before the call, charged afterwards with an atomic
  `INSERT ... ON CONFLICT DO UPDATE`. Concurrent in-flight requests can overshoot slightly;
  a hard quota would need up-front token reservation.
- **Short-lived DB sessions.** Each DB step opens and closes its own session, so a 60-second
  generation never holds a pooled connection.
- **Registry as an explicit transition table** with `SELECT ... FOR UPDATE` against racing
  admins. Every mutation writes its `audit_log` row **in the same transaction**, so no change
  can exist without its audit record. Non-deployed models return 404, the same as unknown ones.
- **Pluggable admin auth.** Endpoints depend on a `Principal(subject, roles)` and check the
  `platform-admin` role; today a static key produces that principal (constant-time compare),
  and an OIDC/JWT authenticator can replace it without touching the endpoints.
- **Bounded metric cardinality.** Routes are labelled by template, models only by registry
  names, tenants are capped (first N, then `other`), and error reasons come from a fixed
  vocabulary.
- **Ops hygiene.** Non-root image (UID 10001, root-owned code), migrations run as a one-shot
  compose job (not at app start, which would race between replicas), and healthcheck-ordered
  start-up. CI pins every action to a commit SHA with `permissions: contents: read`, and
  tests run against real Postgres via the actual Alembic migrations, plus an `alembic check`
  drift test.

## What I'd change for production

- **Distributed rate limiting** in Redis (atomic Lua token bucket) or at the ingress, because
  in-memory limits multiply with replicas and reset on restart.
- **Hard quotas and billing-grade metering:** reserve `max_tokens` before the call and
  reconcile afterwards; write usage events to a durable log for invoicing.
- **SSO for admins (OIDC)** with separation of duties (submitter ≠ approver), and scoped,
  expiring tenant keys with rotation.
- **Cache registry lookups and key lookups** (short TTL, invalidated on change) to take two
  queries off the hot path.
- **Resilience:** per-backend timeouts and circuit breakers, retries with jitter before the
  first byte, concurrency limits per model, and load-balancing across backend replicas (vLLM
  for GPU serving).
- **Security hardening:** TLS everywhere, secrets from a secret manager, request size
  limits, prompt/response logging policy (PII), network policies, image signing (cosign) and
  SBOMs.
- **Tracing** (OpenTelemetry) and SLO-based alerting (e.g. on TTFT p95 and upstream error rate).
- **Kubernetes deployment** via a Helm chart with HPA, PodDisruptionBudget and a
  ServiceMonitor, delivered with GitOps.

## Project layout

```
src/gateway/
  main.py            app factory, lifespan, wiring
  config.py          env-based settings (GATEWAY_*)
  schemas.py         OpenAI request/response models
  errors.py          OpenAI-style error envelope + handlers
  auth.py            API key hashing, tenant auth, pluggable admin auth
  ratelimit.py       token bucket
  quota.py           monthly token usage (atomic upsert)
  registry.py        model lifecycle state machine + audit helper
  db.py              ORM tables + engine/session
  metrics.py         Prometheus metrics, tenant label cap
  observability.py   JSON logging, request-id/latency ASGI middleware
  backends/          Backend ABC, OllamaBackend, MockBackend
  routers/           openai.py, admin.py, health.py
migrations/          Alembic migrations
tests/               pytest suite (real Postgres + MockBackend)
deploy/              Prometheus scrape config, Grafana provisioning + dashboard
```

## License

MIT, see [LICENSE](LICENSE).
