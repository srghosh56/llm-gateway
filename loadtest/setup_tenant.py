"""Prepare a gateway for a load test through the admin API and print the tenant's API key.

Deploys a model (default: the instant MockBackend "mock-echo") and creates a tenant whose
rate limit and quota are high enough that the load test measures the gateway, not the
limiter (429s would count as failures).

    GATEWAY_URL=http://localhost:8000 GATEWAY_ADMIN_API_KEY=... \
        uv run python loadtest/setup_tenant.py            # prints gw_...
"""

import os
import sys

import httpx

URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
ADMIN_KEY = os.environ["GATEWAY_ADMIN_API_KEY"]
MODEL = os.environ.get("LOADTEST_MODEL", "mock-echo")
BACKEND = os.environ.get("LOADTEST_BACKEND", "mock")
UPSTREAM = os.environ.get("LOADTEST_UPSTREAM", MODEL)
TENANT = os.environ.get("LOADTEST_TENANT", "loadtest")


def main() -> None:
    with httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {ADMIN_KEY}"}) as admin:
        # Re-runnable: 409 means "already exists", which is fine.
        r = admin.post(
            "/admin/models",
            json={"name": MODEL, "backend": BACKEND, "upstream_model": UPSTREAM, "owner": "qa"},
        )
        if r.status_code not in (201, 409):
            sys.exit(f"submit model failed: {r.status_code} {r.text}")
        for action in ("approve", "deploy"):
            admin.post(f"/admin/models/{MODEL}/{action}")  # 409 if already past this state

        limits = {"rate_limit_rpm": 1_000_000, "rate_limit_burst": 100_000}
        quota = {"monthly_token_quota": 10**12}
        r = admin.post("/admin/tenants", json={"name": TENANT, **limits, **quota})
        if r.status_code not in (201, 409):
            sys.exit(f"create tenant failed: {r.status_code} {r.text}")
        tenant = next(t for t in admin.get("/admin/tenants").json() if t["name"] == TENANT)
        key = admin.post(f"/admin/tenants/{tenant['id']}/keys").json()["key"]
    print(key)


if __name__ == "__main__":
    main()
