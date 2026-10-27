"""Prometheus metrics.

Cardinality is the main risk with per-tenant metrics: every distinct label combination is a
separate time series stored by Prometheus. The rules used here:

* ``route`` is the route *template* (``/admin/models/{name}/{action}``), never the raw path;
  unmatched paths collapse to ``"unmatched"``.
* ``model`` only ever holds names of registry models that resolved (bounded by the registry).
* ``tenant`` is capped by ``TenantLabeler``: the first N tenants seen by this process get their
  own label, the rest are aggregated as ``"other"``.
* error ``reason`` values come from a fixed vocabulary (see backends.base.UpstreamError).

Metrics live in the default process-wide registry (the usual prometheus_client pattern).
"""

from prometheus_client import Counter, Histogram, disable_created_metrics

# Drop the extra "*_created" timestamp series per label set: halves series count, unused here.
disable_created_metrics()  # type: ignore[no-untyped-call]  # untyped in prometheus_client

REQUEST_LATENCY = Histogram(
    "gateway_http_request_duration_seconds",
    "HTTP request latency, measured until the last byte of the body (covers full streams).",
    ["method", "route", "status"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

TENANT_REQUESTS = Counter(
    "gateway_tenant_requests_total",
    "Chat completion requests per tenant, model and outcome.",
    ["tenant", "model", "outcome"],
)

TENANT_TOKENS = Counter(
    "gateway_tenant_tokens_total",
    "Tokens consumed per tenant and model.",
    ["tenant", "model", "kind"],  # kind: prompt | completion
)

TENANT_REJECTIONS = Counter(
    "gateway_tenant_rejections_total",
    "Requests rejected by policy before reaching a backend.",
    ["tenant", "reason"],  # reason: rate_limit | quota | disabled
)

TOKENS_PER_SECOND = Histogram(
    "gateway_tokens_per_second",
    "Completion tokens per second of generation time, per request.",
    ["model"],
    buckets=(1, 5, 10, 20, 40, 60, 100, 200, 500),
)

TIME_TO_FIRST_TOKEN = Histogram(
    "gateway_time_to_first_token_seconds",
    "Streaming only: time until the first chunk arrived from the upstream.",
    ["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

UPSTREAM_ERRORS = Counter(
    "gateway_upstream_errors_total",
    "Errors talking to model backends.",
    ["backend", "reason"],
)


class TenantLabeler:
    """Caps the number of distinct tenant label values exported by one process."""

    OVERFLOW = "other"

    def __init__(self, max_tenants: int) -> None:
        self._max = max_tenants
        self._seen: set[str] = set()

    def __call__(self, tenant: str) -> str:
        if tenant in self._seen:
            return tenant
        if len(self._seen) < self._max:
            self._seen.add(tenant)
            return tenant
        return self.OVERFLOW
