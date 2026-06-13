"""Smoke test against a DEPLOYED backend (e.g. on Alibaba Cloud ECS).

Two parts:

1. An always-on structural check that the executor's sandbox-only guard holds —
   it runs in the normal offline suite, so every `make test` re-confirms the
   guarantee the deployment relies on.

2. Env-gated checks that hit a LIVE deployment: health, the Qwen Cloud live
   proof, and one incident driven end-to-end through the deployed pipeline.
   These are SKIPPED unless `AUTOPILOT_SMOKE_BASE_URL` points at a running
   backend, so they never run (or spend tokens) during a normal `make test`.

Run them explicitly:

    AUTOPILOT_SMOKE_BASE_URL=http://<ecs-ip>:8080 \
    AUTOPILOT_SMOKE_REAL_CLOUD=1 \
        make smoke-deploy
"""

import os
import time

import httpx
import pytest

from autopilot.cloud.qwen_live import CloudSelfCheck, run_self_check
from autopilot.harness.synthetic import FAULT_IDS
from autopilot.mcp_servers.guards import (
    SANDBOX_SERVICES,
    SandboxViolation,
    ensure_sandbox_service,
)

BASE_URL = os.environ.get("AUTOPILOT_SMOKE_BASE_URL")
REAL_CLOUD = os.environ.get("AUTOPILOT_SMOKE_REAL_CLOUD") == "1"

requires_deployment = pytest.mark.skipif(
    not BASE_URL,
    reason="set AUTOPILOT_SMOKE_BASE_URL to run the deploy smoke test",
)


# --------------------------------------------------------------------------- #
# Always-on: the deployment relies on this guard. Confirm it structurally.
# --------------------------------------------------------------------------- #

def test_executor_sandbox_guard_holds():
    """The sandbox-only guard must reject anything outside the five sandbox
    services — host, external systems, foreign compose projects. This is the
    invariant the deployed executor depends on (DEPLOYMENT.md)."""
    for svc in sorted(SANDBOX_SERVICES):
        assert ensure_sandbox_service(svc) == svc  # in-scope targets pass

    for hostile in ["localhost", "/var/run/docker.sock", "prod-db",
                    "app; rm -rf /", "../host", "redis", ""]:
        with pytest.raises(SandboxViolation):
            ensure_sandbox_service(hostile)


def test_selfcheck_mock_vs_real_headline_distinct():
    """A mock self-check and a real one must be UNMISTAKABLY different, so a mock
    run can never be passed off as a real cloud proof. Runs offline (conftest
    forces AUTOPILOT_MOCK_LLM=1) — no network, no tokens."""
    mock = run_self_check()
    assert mock.mocked is True and mock.ok is True
    assert "MOCK MODE" in mock.headline  # the mock sentinel
    assert "mocked=false" not in mock.headline

    # A real OK result (constructed, not called) reads REAL and never "MOCK MODE"
    # (note its text contains "mocked=false", so a naive "MOCK" check is wrong).
    real = CloudSelfCheck(
        ok=True, mocked=False, checked_at="t",
        endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        cloud_host="dashscope-intl.aliyuncs.com",
        region="alibaba-cloud-intl (Singapore)", role="default",
        model="qwen3.7-plus", model_by_role={"default": "qwen3.7-plus"},
        input_tokens=20, output_tokens=2, est_cost_usd=0.000007,
        tier="free", latency_ms=412.0,
    )
    assert real.headline.startswith("REAL") and "mocked=false" in real.headline
    assert "MOCK MODE" not in real.headline
    assert mock.headline != real.headline


# --------------------------------------------------------------------------- #
# Env-gated: hit the live deployment.
# --------------------------------------------------------------------------- #

@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as c:
        yield c


@requires_deployment
@pytest.mark.deploy
def test_deployed_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body.get("version")


@requires_deployment
@pytest.mark.deploy
def test_deployed_cloud_selfcheck(client):
    """The deployed backend can reach the Qwen Cloud (Alibaba Cloud) endpoint."""
    resp = client.get("/api/cloud/selfcheck", timeout=45.0)
    assert resp.status_code == 200
    body = resp.json()
    # Loud, camera-proof line in the smoke output (run with `make smoke-deploy`).
    print(f"\n  CLOUD SELF-CHECK → {body.get('headline')}\n")
    assert body["ok"] is True, f"cloud self-check failed: {body.get('error')}"
    assert "aliyuncs" in body["cloud_host"], body["cloud_host"]

    if REAL_CLOUD:
        # Prove it was a genuine round-trip, not the offline mock short-circuit.
        assert body["mocked"] is False, (
            "AUTOPILOT_SMOKE_REAL_CLOUD=1 but the deployed backend is in MOCK mode "
            "— this is NOT a real proof. Unset AUTOPILOT_MOCK_LLM on the server."
        )
        assert body["headline"].startswith("REAL") and "MOCK MODE" not in body["headline"]
        assert body["model"] in body["model_by_role"].values()
        assert (body["input_tokens"] or 0) + (body["output_tokens"] or 0) > 0
        assert body["est_cost_usd"] is not None
        assert body["latency_ms"] and body["latency_ms"] > 0


@requires_deployment
@pytest.mark.deploy
def test_one_incident_end_to_end(client):
    """Drive one incident through the deployed pipeline to a terminal state."""
    fault_id = FAULT_IDS[0]
    created = client.post("/api/runs", json={"fault_id": fault_id})
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]

    terminal = {"resolved", "rolled_back", "rejected", "failed"}
    deadline = time.monotonic() + 90  # bounded; never hangs CI
    status = None
    while time.monotonic() < deadline:
        detail = client.get(f"/api/runs/{run_id}").json()
        status = detail["status"]
        if status == "awaiting_approval":
            # Approve so the run can complete end-to-end.
            client.post(f"/api/runs/{run_id}/decision", json={"action": "approve"})
        elif status in terminal:
            break
        time.sleep(1.0)

    assert status in terminal, f"run did not finish (last status: {status})"
    final = client.get(f"/api/runs/{run_id}").json()
    # A real run produced a diagnosis and a non-empty reasoning trace.
    assert final["top_cause"]
    assert len(final["events"]) >= 4
