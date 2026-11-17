"""Load test for the gateway, written with Locust (https://locust.io).

What it measures: the *gateway's own overhead* (auth lookup, rate limit, quota check,
registry lookup, usage accounting, SSE re-streaming) when pointed at the MockBackend, which
answers instantly. That isolates the gateway from model speed, so results are stable enough
to gate CI on. Pointed at a real model (LOADTEST_MODEL=qwen-small) it measures end-to-end
latency instead, which is dominated by the model and should not be used as a CI gate.

Run (see `make loadtest` and the CI "loadtest" job):
    LOADTEST_API_KEY=gw_... uv run locust -f loadtest/locustfile.py --headless \
        -u 20 -r 10 -t 30s --host http://localhost:8000

Pass/fail: when the run ends, the process exit code is set to 1 if the failure ratio or the
95th-percentile latency exceeds the thresholds below, so CI fails on a regression.
"""

import logging
import os

from locust import HttpUser, between, events, task
from locust.env import Environment

API_KEY = os.environ.get("LOADTEST_API_KEY", "")
MODEL = os.environ.get("LOADTEST_MODEL", "mock-echo")
MAX_FAIL_RATIO = float(os.environ.get("LOADTEST_MAX_FAIL_RATIO", "0.01"))  # 1 %
MAX_P95_MS = float(os.environ.get("LOADTEST_MAX_P95_MS", "500"))

MESSAGES = [{"role": "user", "content": "Summarise what an API gateway does in one line."}]


class GatewayUser(HttpUser):
    # Each simulated user waits 0.1-0.5 s between requests (think time).
    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        self.client.headers["Authorization"] = f"Bearer {API_KEY}"

    @task(5)
    def chat(self) -> None:
        body = {"model": MODEL, "messages": MESSAGES, "max_tokens": 32}
        with self.client.post(
            "/v1/chat/completions", json=body, name="chat", catch_response=True
        ) as r:
            if r.status_code != 200 or not r.json()["choices"][0]["message"]["content"]:
                r.failure(f"status {r.status_code}")

    @task(2)
    def chat_stream(self) -> None:
        body = {"model": MODEL, "messages": MESSAGES, "max_tokens": 32, "stream": True}
        with self.client.post(
            "/v1/chat/completions", json=body, name="chat (stream)", catch_response=True
        ) as r:
            # Reading r.text consumes the whole SSE stream, so the timing covers all chunks.
            events_ = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
            if r.status_code != 200 or not events_ or events_[-1] != "data: [DONE]":
                r.failure(f"status {r.status_code}, stream incomplete")
            elif any('"error"' in e for e in events_[:-1]):
                r.failure("error event in stream")

    @task(1)
    def list_models(self) -> None:
        self.client.get("/v1/models", name="models")


@events.quitting.add_listener
def enforce_thresholds(environment: Environment, **_: object) -> None:
    stats = environment.stats.total
    p95 = stats.get_response_time_percentile(0.95) or 0.0
    log = logging.getLogger("loadtest")
    log.info(
        "requests=%d failures=%d fail_ratio=%.4f p95=%.0fms rps=%.1f",
        stats.num_requests,
        stats.num_failures,
        stats.fail_ratio,
        p95,
        stats.total_rps,
    )
    if stats.num_requests == 0:
        log.error("FAIL: no requests were made")
        environment.process_exit_code = 1
    elif stats.fail_ratio > MAX_FAIL_RATIO:
        log.error("FAIL: failure ratio %.4f > %.4f", stats.fail_ratio, MAX_FAIL_RATIO)
        environment.process_exit_code = 1
    elif p95 > MAX_P95_MS:
        log.error("FAIL: p95 %.0fms > %.0fms", p95, MAX_P95_MS)
        environment.process_exit_code = 1
    else:
        environment.process_exit_code = 0
