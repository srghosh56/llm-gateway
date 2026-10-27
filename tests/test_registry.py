import httpx
import pytest

from gateway.registry import TRANSITIONS, InvalidTransition, Status, check_transition
from tests.conftest import TenantHandle, deploy_model

CHAT = "/v1/chat/completions"


def submit_body(name: str = "qwen-small") -> dict[str, str]:
    return {"name": name, "backend": "mock", "upstream_model": "mock-echo", "owner": "team-ml"}


def ask(model: str) -> dict[str, object]:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}]}


# --- state machine (unit, exhaustive) ---------------------------------------------------

ALLOWED = {
    (Status.SUBMITTED, Status.APPROVED),
    (Status.SUBMITTED, Status.RETIRED),
    (Status.APPROVED, Status.DEPLOYED),
    (Status.APPROVED, Status.RETIRED),
    (Status.DEPLOYED, Status.RETIRED),
}


@pytest.mark.parametrize("current", list(Status))
@pytest.mark.parametrize("target", list(Status))
def test_transition_table(current: Status, target: Status) -> None:
    if (current, target) in ALLOWED:
        check_transition(current, target)
    else:
        with pytest.raises(InvalidTransition):
            check_transition(current, target)


def test_retired_is_terminal() -> None:
    assert TRANSITIONS[Status.RETIRED] == frozenset()


# --- API --------------------------------------------------------------------------------


async def test_full_lifecycle_controls_serving(
    admin: httpx.AsyncClient, tenant: TenantHandle
) -> None:
    c = tenant.client
    r = await admin.post("/admin/models", json=submit_body())
    assert r.status_code == 201
    assert r.json()["status"] == "submitted"

    # submitted and approved models are neither served nor listed
    assert (await c.post(CHAT, json=ask("qwen-small"))).status_code == 404
    assert (await admin.post("/admin/models/qwen-small/approve")).json()["status"] == "approved"
    assert (await c.post(CHAT, json=ask("qwen-small"))).status_code == 404
    assert "qwen-small" not in {m["id"] for m in (await c.get("/v1/models")).json()["data"]}

    # deployed: served (routed to the upstream id) and listed with its owner
    assert (await admin.post("/admin/models/qwen-small/deploy")).json()["status"] == "deployed"
    r = await c.post(CHAT, json=ask("qwen-small"))
    assert r.status_code == 200
    assert r.json()["model"] == "qwen-small"
    cards = {m["id"]: m for m in (await c.get("/v1/models")).json()["data"]}
    assert cards["qwen-small"]["owned_by"] == "team-ml"

    # retired: gone again
    assert (await admin.post("/admin/models/qwen-small/retire")).json()["status"] == "retired"
    assert (await c.post(CHAT, json=ask("qwen-small"))).status_code == 404
    assert "qwen-small" not in {m["id"] for m in (await c.get("/v1/models")).json()["data"]}


async def test_cannot_skip_approval(admin: httpx.AsyncClient) -> None:
    await admin.post("/admin/models", json=submit_body())
    r = await admin.post("/admin/models/qwen-small/deploy")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


async def test_cannot_revive_retired(admin: httpx.AsyncClient) -> None:
    await deploy_model(admin, "old")
    await admin.post("/admin/models/old/retire")
    for action in ("approve", "deploy", "retire"):
        assert (await admin.post(f"/admin/models/old/{action}")).status_code == 409


async def test_unknown_action_and_model(admin: httpx.AsyncClient) -> None:
    await admin.post("/admin/models", json=submit_body())
    assert (await admin.post("/admin/models/qwen-small/delete")).status_code == 400
    assert (await admin.post("/admin/models/missing/approve")).status_code == 404


async def test_duplicate_and_unknown_backend(admin: httpx.AsyncClient) -> None:
    assert (await admin.post("/admin/models", json=submit_body())).status_code == 201
    assert (await admin.post("/admin/models", json=submit_body())).status_code == 409
    bad = {**submit_body("x"), "backend": "vllm"}
    assert (await admin.post("/admin/models", json=bad)).status_code == 400


async def test_admin_listing_filters_by_status(admin: httpx.AsyncClient) -> None:
    await admin.post("/admin/models", json=submit_body("a"))
    await deploy_model(admin, "b")
    everything = (await admin.get("/admin/models")).json()
    assert [m["name"] for m in everything] == ["a", "b"]
    deployed = (await admin.get("/admin/models", params={"status": "deployed"})).json()
    assert [m["name"] for m in deployed] == ["b"]


async def test_registry_requires_admin(anon: httpx.AsyncClient) -> None:
    assert (await anon.post("/admin/models", json=submit_body())).status_code == 401
    assert (await anon.get("/admin/audit")).status_code == 401


async def test_audit_log_records_who_what_when(admin: httpx.AsyncClient) -> None:
    await deploy_model(admin, "qwen-small")
    await admin.post("/admin/models/qwen-small/retire")
    rows = (await admin.get("/admin/audit", params={"target_type": "model"})).json()
    # newest first
    assert [r["action"] for r in rows] == [
        "model.retire",
        "model.deploy",
        "model.approve",
        "model.submit",
    ]
    assert all(r["actor"] == "admin-key" and r["target_id"] == "qwen-small" for r in rows)
    assert all(r["created_at"] for r in rows)
    assert rows[0]["details"] == {"from": "deployed", "to": "retired"}


async def test_failed_transition_is_not_audited(admin: httpx.AsyncClient) -> None:
    await admin.post("/admin/models", json=submit_body())
    await admin.post("/admin/models/qwen-small/deploy")  # 409
    rows = (await admin.get("/admin/audit")).json()
    assert [r["action"] for r in rows] == ["model.submit"]


async def test_tenant_and_key_actions_are_audited(
    admin: httpx.AsyncClient, tenant: TenantHandle
) -> None:
    await admin.delete("/admin/keys/1")
    actions = [r["action"] for r in (await admin.get("/admin/audit")).json()]
    assert actions[:3] == ["api_key.revoke", "api_key.create", "tenant.create"]
    # the secret key never lands in the audit log
    assert tenant.key not in (await admin.get("/admin/audit")).text
