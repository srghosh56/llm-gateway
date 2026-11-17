# llm-gateway

A small, production-style, **multi-tenant, OpenAI-compatible LLM gateway** in Python.

Point any OpenAI client (SDK, LangChain, curl) at it, and it adds what a platform team needs
in front of self-hosted models: per-tenant API keys, rate limits and monthly token quotas, a
governed model registry with an approval workflow and audit trail, admin single sign-on via
Keycloak (OIDC), Prometheus metrics, OpenTelemetry traces and structured logs. It ships as a
docker compose stack and as a Helm chart deployed by Argo CD.

It's deliberately small (~1,950 lines of application code including comments, ~1,250 lines
of tests) so every design decision can be read and explained.

| Area | Technology |
|---|---|
| API | Python 3.12, FastAPI, Pydantic, httpx, Server-Sent Events |
| Data | PostgreSQL, SQLAlchemy 2.0 (async, asyncpg), Alembic migrations |
| Models | [Ollama](https://ollama.com) (`qwen2.5:0.5b`, runs on a laptop CPU); MockBackend for tests |
| Auth | per-tenant API keys (SHA-256), admin OIDC via Keycloak (JWT + JWKS) |
| Observability | Prometheus, Grafana, OpenTelemetry -> Jaeger, JSON logs |
| Delivery | Docker (non-root), docker compose, Helm, Argo CD, kind |
| Quality | pytest (96 tests, ~97% coverage), Locust load test, ruff, mypy --strict, Trivy, GitHub Actions |

## Architecture

```
   OpenAI SDK / curl / any OpenAI client          admin (curl / CLI)
                    |                                    |  1. log in (alice/bob)
     Authorization: Bearer gw_...                        v
       (tenant API key)                           +-------------+
                    |                             |  Keycloak   |  realm llm-gateway,
                    |       2. Bearer <JWT> ----- |   (OIDC)    |  role platform-admin
                    v              |              +------+------+
                                   |                     | JWKS (public signing keys)
                                   v                     v
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
 |  /admin/*  (static key OR Keycloak JWT -> Principal{roles})  tenants, keys,       |
 |            models (approval workflow), audit log                                  |
 |  /metrics  /healthz  /readyz           OpenTelemetry: spans for request, DB, HTTP |
 +--------+-------------------------------------+---------------------+-------+------+
          |                                     |                     |       |
          v                                     v                     ^ scrape| OTLP
   +-------------+                     +-----------------+     +-------------+ | +--------+
   | PostgreSQL  |                     | Ollama          |     | Prometheus  | | | Jaeger |
   | tenants     |                     | /v1 (OpenAI API)|     +------+------+ +>| traces |
   | api_keys    |                     | qwen2.5:0.5b    |            v          +--------+
   | token_usage |                     +-----------------+     +-------------+
   | models      |                                             |   Grafana   |
   | audit_log   |                                             |  dashboard  |
   +-------------+                                             +-------------+

 Runs as: docker compose (all of the above)  or  Kubernetes: Helm chart synced by Argo CD
 (gateway, Postgres, Ollama + ServiceMonitor/alerts/dashboard into kube-prometheus-stack)
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
docker compose up -d --build    # postgres, migrate, ollama, ollama-pull, gateway,
                                #   keycloak, jaeger, prometheus, grafana
./scripts/bootstrap.sh          # deploys model "qwen-small", creates tenant "demo", prints its key
```

| What | URL |
|---|---|
| Gateway (OpenAPI docs at `/docs`) | http://localhost:8000 |
| Prometheus | http://localhost:9090 |
| Grafana, dashboard "LLM Gateway" (anonymous viewer; admin/admin) | http://localhost:3000 |
| Keycloak (admin console: admin/admin; realm `llm-gateway`) | http://localhost:8081 |
| Jaeger UI (traces, service `llm-gateway`) | http://localhost:16686 |

### Local development

```bash
uv sync
docker run -d --name llmgw-testdb -p 55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gateway_test postgres:16.10-alpine
uv run pytest                  # tests use the MockBackend, no Ollama needed
uv run ruff check . && uv run mypy
```

## Kubernetes (Helm + Argo CD)

`charts/llm-gateway` deploys the gateway (2 replicas, zero-downtime rolling updates, PDB),
Postgres (StatefulSet) and Ollama (Deployment + model PVC), plus a ServiceMonitor,
PrometheusRule alerts and the Grafana dashboard as a ConfigMap. It's built for the kind
cluster from [gitops-microservice-demo](https://github.com/srghosh56/gitops-microservice-demo)
(Argo CD + kube-prometheus-stack via Terraform) and follows that repo's conventions.

```bash
# in gitops-microservice-demo: make cluster && make platform    (once)
make kind-load                  # build llm-gateway:0.1.0 and load it into the kind node
make argocd-app                 # Argo CD syncs charts/llm-gateway from this repo's main branch
kubectl -n llm-gateway get pods # 2x gateway, postgres-0, ollama; Jobs: migrate (done), ollama-pull
make pf-app                     # http://localhost:8091
make bootstrap-k8s              # deploy qwen-small + demo tenant via the admin API
```

How the sync is ordered (Argo CD sync waves and hooks):

```
wave -2  Secret                             (read by Postgres, migrations, gateway)
wave -1  Postgres StatefulSet + Service      (must be healthy before migrating)
wave  0  migrate Job (Sync hook)             alembic upgrade head, once per sync
wave  1  gateway Deployment                  readiness = database reachable
PostSync ollama-pull Job                     pulls the model, skipped if already on the PVC
```

Every container runs as non-root with a read-only root filesystem, no capabilities, no
service-account token and a RuntimeDefault seccomp profile (Trivy config scan: no HIGH or
CRITICAL findings). Prometheus picks up the ServiceMonitor (the platform selects
ServiceMonitors in all namespaces) and Grafana loads the dashboard via its sidecar
(`grafana_dashboard: "1"`). Alerts: upstream error ratio > 5% for 5 min, p95 time to first
token > 10 s for 10 min.

Secrets: by default the chart renders a demo Secret from `values.yaml` (fine for a local
kind cluster, never for anything shared). For real use, create the Secret out of band (or
with External Secrets / Sealed Secrets) and set `secrets.existingSecret`.

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

### Admin SSO with Keycloak (OIDC)

The compose stack includes Keycloak (http://localhost:8081, console login `admin`/`admin`)
with an imported realm `llm-gateway`: user **alice** has the realm role `platform-admin`,
user **bob** does not. The gateway validates the JWT access token against the realm's JWKS
(signature, `RS256` only, `exp`, `iss`, `aud=llm-gateway`) and maps `platform-admin` to admin.
The static admin key keeps working next to it (break-glass / automation).

```bash
token() { curl -s http://localhost:8081/realms/llm-gateway/protocol/openid-connect/token \
  -d grant_type=password -d client_id=llm-gateway-cli -d username=$1 -d password=$1 \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])"; }

curl -s localhost:8000/admin/models -H "Authorization: Bearer $(token alice)"   # 200
curl -s localhost:8000/admin/models -H "Authorization: Bearer $(token bob)"     # 403
curl -s "localhost:8000/admin/audit?limit=1" -H "Authorization: Bearer $(token alice)"
# -> actor "oidc:alice"
```

The password grant is used only to keep the demo to one `curl`; a real admin CLI would use
the authorization-code + PKCE or device flow, which produce the same kind of token.

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

96 pytest tests, 98% line coverage, run against a real PostgreSQL with the Alembic migrations
applied (no Ollama needed: tests use the `MockBackend`).

| Area | What is asserted |
|---|---|
| OpenAI API | non-stream and SSE stream shape, `[DONE]`, `include_usage`, 400/404/502 errors |
| OpenAI SDK | the official `openai` client: list models, completion, streaming, typed exceptions |
| Auth | 401 missing/wrong/revoked key, 403 disabled tenant, keys stored only as hashes, admin key required |
| OIDC | admin role 200, missing role 403, expired/wrong issuer/wrong audience/foreign signature/`alg: none`/HS256 confusion 401, JWKS cache, key rotation, refetch rate limit, IdP outage |
| Rate limit | token bucket burst/refill/cap (fake clock), per-tenant isolation, 429 + `Retry-After` |
| Quota | usage recorded for JSON and streamed responses, 429 `insufficient_quota`, raising quota unblocks |
| Registry | every (from, to) transition pair, lifecycle controls serving and listing, 409s, audit rows |
| Observability | metric deltas per tenant/model, route-template labels, upstream errors, label cap, request ids, JSON logs, probes |
| Tracing | gateway/gen_ai attributes on the server span, stream span + `first_token` event, DB child spans, upstream client span + `traceparent` propagation, probes untraced, `trace_id` in logs, off by default |
| Backends | Ollama proxy against a fake upstream (`httpx.MockTransport`): payload, SSE parsing, error mapping |

### Load test

`loadtest/locustfile.py` (Locust) mixes non-streaming chat, streaming chat and model listing
from 20 concurrent users. In CI it runs for 30 s against the real gateway + Postgres with the
MockBackend, so it measures the gateway's own overhead (auth, rate limit, quota, registry
lookup, accounting, SSE) and fails the build if more than 1% of requests fail or p95 latency
exceeds 500 ms. A local run on a laptop: 690 requests, 0 failures, p95 35 ms, ~60 req/s.

```bash
GATEWAY_ADMIN_API_KEY=... make loadtest          # against a running gateway (compose)
```

Pointed at a real model (`LOADTEST_MODEL=qwen-small LOADTEST_BACKEND=ollama
LOADTEST_UPSTREAM=qwen2.5:0.5b`) it measures end-to-end latency, which is dominated by the
model on CPU, so that variant is for exploration, not a CI gate.

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
`X-Request-ID` if it's safe, otherwise generated, and always echoed in the response header),
`tenant`, and, when tracing is on, `trace_id`/`span_id`. `/healthz` is liveness (process up);
`/readyz` is readiness (database reachable).

### Tracing (OpenTelemetry -> Jaeger)

With `GATEWAY_OTEL_ENABLED=true` (set in compose) every request becomes one trace, exported
over OTLP/HTTP to Jaeger:

```
POST /v1/chat/completions   9423 ms  gateway.tenant=demo gen_ai.request.model=qwen-small
├── SELECT api_keys ...        0.6 ms   (auth)                gen_ai.usage.input_tokens=31
├── SELECT token_usage ...     0.3 ms   (quota)               gen_ai.usage.output_tokens=30
├── SELECT models ...          0.4 ms   (registry)            gateway.outcome=success
├── POST ollama:11434/v1/...  9364 ms   (upstream, traceparent propagated)
└── INSERT token_usage ...     5.5 ms   (usage accounting)
```

To go from a log line to its trace, copy `trace_id` from the JSON log and open
`http://localhost:16686/trace/<trace_id>`. Probes and `/metrics` aren't traced, per-chunk
ASGI send spans are disabled (a stream would otherwise create one span per token), and
sampling is parent-based with a configurable ratio (`GATEWAY_OTEL_SAMPLE_RATIO`).

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
- **Pluggable admin auth.** Endpoints only depend on a `Principal(subject, roles)` and check
  the `platform-admin` role. Two authenticators produce it, tried in order: a static key
  (constant-time compare) and OIDC (Keycloak JWTs). OIDC validation pins the algorithm
  allow-list (blocks `alg: none` and RS256->HS256 key confusion), requires `exp`/`iss`/`aud`,
  caches the JWKS and refetches on an unknown `kid` (key rotation) at most every 30 s, and
  keeps the cached keys if the identity provider is briefly down.
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
- **Separation of duties** in the registry (submitter ≠ approver, now possible since admins
  have individual OIDC identities), and scoped, expiring tenant keys with rotation.
- **Cache registry lookups and key lookups** (short TTL, invalidated on change) to take two
  queries off the hot path.
- **Resilience:** per-backend timeouts and circuit breakers, retries with jitter before the
  first byte, concurrency limits per model, and load-balancing across backend replicas (vLLM
  for GPU serving).
- **Security hardening:** TLS everywhere, secrets from a secret manager, request size
  limits, prompt/response logging policy (PII), network policies, image signing (cosign) and
  SBOMs.
- **SLO-based alerting** (e.g. on TTFT p95 and upstream error rate), and tail-based trace
  sampling in an OpenTelemetry Collector (keep all slow and failed traces, sample the rest).
- **Kubernetes hardening:** publish signed images to a registry (pinned by digest instead of
  `kind load`), Ingress with TLS, HPA on request rate, NetworkPolicies, secrets from External
  Secrets, and a managed Postgres instead of the in-chart StatefulSet.

## Project layout

```
src/gateway/
  main.py            app factory, lifespan, wiring
  config.py          env-based settings (GATEWAY_*)
  schemas.py         OpenAI request/response models
  errors.py          OpenAI-style error envelope + handlers
  auth.py            API key hashing, tenant auth, pluggable admin auth
  oidc.py            OIDC/JWT admin authenticator (JWKS cache, key rotation)
  ratelimit.py       token bucket
  quota.py           monthly token usage (atomic upsert)
  registry.py        model lifecycle state machine + audit helper
  db.py              ORM tables + engine/session
  metrics.py         Prometheus metrics, tenant label cap
  observability.py   JSON logging, request-id/latency ASGI middleware
  tracing.py         OpenTelemetry setup (FastAPI, SQLAlchemy, httpx) and span helpers
  backends/          Backend ABC, OllamaBackend, MockBackend
  routers/           openai.py, admin.py, health.py
migrations/          Alembic migrations
tests/               pytest suite (real Postgres + MockBackend)
loadtest/            Locust load test + tenant setup (CI performance gate)
charts/llm-gateway/  Helm chart (gateway, Postgres, Ollama, ServiceMonitor, alerts, dashboard)
argocd/              Argo CD Application (auto-sync, prune, self-heal)
deploy/              Prometheus scrape config, Grafana provisioning, Keycloak realm
scripts/bootstrap.sh demo setup through the admin API (model + tenant + key)
Makefile             shortcuts: test, lint, loadtest, build, kind-load, argocd-app, ...
```

## Development workflow

`main` only changes through pull requests: each feature lives on its own branch
(`feat/oidc-keycloak`, `feat/otel-tracing`, `feat/helm-argocd`, `feat/load-tests`, ...), CI
(lint, tests, load test, image build + Trivy) runs on the pull request, and the branch is
merged once CI is green. Argo CD deploys whatever is on `main`, so merging is deploying.

## License

MIT, see [LICENSE](LICENSE).
