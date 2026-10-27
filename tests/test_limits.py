import json

import httpx
from fastapi import FastAPI

from gateway.ratelimit import TokenBucketLimiter
from tests.conftest import make_tenant

CHAT = "/v1/chat/completions"


def msg(text: str = "one two three", stream: bool = False) -> dict[str, object]:
    return {"model": "mock-echo", "messages": [{"role": "user", "content": text}], "stream": stream}


# --- token bucket (unit) ----------------------------------------------------------------


def test_bucket_allows_burst_then_blocks() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(clock=lambda: now[0])
    assert all(limiter.acquire(1, rpm=60, burst=3)[0] for _ in range(3))
    allowed, retry_after = limiter.acquire(1, rpm=60, burst=3)
    assert not allowed
    assert retry_after == 1.0  # 60 rpm = 1 token per second


def test_bucket_refills_over_time_and_caps_at_burst() -> None:
    now = [0.0]
    limiter = TokenBucketLimiter(clock=lambda: now[0])
    for _ in range(2):
        limiter.acquire(1, rpm=60, burst=2)
    now[0] = 100.0  # long idle: refill is capped at burst, not 100 tokens
    assert [limiter.acquire(1, rpm=60, burst=2)[0] for _ in range(3)] == [True, True, False]


def test_buckets_are_per_tenant() -> None:
    limiter = TokenBucketLimiter(clock=lambda: 0.0)
    assert limiter.acquire(1, rpm=60, burst=1)[0]
    assert not limiter.acquire(1, rpm=60, burst=1)[0]
    assert limiter.acquire(2, rpm=60, burst=1)[0]


# --- rate limit + quota (API) -----------------------------------------------------------


async def test_rate_limit_returns_429_with_retry_after(
    app: FastAPI, admin: httpx.AsyncClient
) -> None:
    t = await make_tenant(app, admin, rate_limit_rpm=1, rate_limit_burst=2)
    codes = [(await t.client.post(CHAT, json=msg())).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = await t.client.post(CHAT, json=msg())
    assert r.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(r.headers["retry-after"]) >= 1
    # Another tenant is unaffected.
    other = await make_tenant(app, admin, name="other", rate_limit_rpm=1, rate_limit_burst=1)
    assert (await other.client.post(CHAT, json=msg())).status_code == 200


async def test_usage_is_recorded(admin: httpx.AsyncClient, client: httpx.AsyncClient) -> None:
    await client.post(CHAT, json=msg("one two three"))  # 3 prompt + 4 completion tokens
    tenants = (await admin.get("/admin/tenants")).json()
    assert tenants[0]["tokens_used_this_month"] == 7


async def test_streamed_usage_is_recorded(
    admin: httpx.AsyncClient, client: httpx.AsyncClient
) -> None:
    r = await client.post(CHAT, json=msg("one two three", stream=True))
    assert r.text.endswith("data: [DONE]\n\n")
    tenants = (await admin.get("/admin/tenants")).json()
    assert tenants[0]["tokens_used_this_month"] == 7


async def test_quota_exceeded_returns_429(app: FastAPI, admin: httpx.AsyncClient) -> None:
    t = await make_tenant(
        app, admin, monthly_token_quota=10, rate_limit_rpm=600, rate_limit_burst=100
    )
    assert (await t.client.post(CHAT, json=msg())).status_code == 200  # 7 tokens
    assert (await t.client.post(CHAT, json=msg())).status_code == 200  # 14: soft overshoot
    r = await t.client.post(CHAT, json=msg())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "insufficient_quota"
    # Raising the quota unblocks the tenant.
    await admin.patch(f"/admin/tenants/{t.id}", json={"monthly_token_quota": 1000})
    assert (await t.client.post(CHAT, json=msg())).status_code == 200


async def test_openai_error_body_shape(app: FastAPI, admin: httpx.AsyncClient) -> None:
    t = await make_tenant(app, admin, monthly_token_quota=0)
    r = await t.client.post(CHAT, json=msg())
    assert set(json.loads(r.text)["error"]) == {"message", "type", "param", "code"}
