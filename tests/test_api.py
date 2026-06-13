"""API tests (mock mode, offline): every endpoint returns its declared schema,
and a full mock-mode run surfaces in the dashboard contract — including an
escalation the operator resolves at the HITL gate (both a reject and an
approve-to-resolution). No Docker, no tokens, no network."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from autopilot.api.app import app
from autopilot.api.schemas import ApprovalView, RunDetail, RunSummary, ScenarioInfo
from autopilot.benchmark.metrics import BenchmarkReport

TERMINAL_OK = ("resolved", "rolled_back", "rejected", "failed")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _poll(client, run_id, *, until, timeout=8.0) -> RunDetail:
    """Poll the run until `until(detail)` is true (runs execute on a thread)."""
    deadline = time.time() + timeout
    detail = None
    while time.time() < deadline:
        detail = RunDetail.model_validate(client.get(f"/api/runs/{run_id}").json())
        if until(detail):
            return detail
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached condition; last={detail}")


# --------------------------------------------------------------- schema checks


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_scenarios_schema(client):
    resp = client.get("/api/scenarios")
    assert resp.status_code == 200
    scenarios = [ScenarioInfo.model_validate(s) for s in resp.json()]
    assert len(scenarios) == 8
    assert all(s.title for s in scenarios)  # no placeholder/empty titles


def test_benchmark_schema(client):
    resp = client.get("/api/benchmark")
    assert resp.status_code == 200
    report = BenchmarkReport.model_validate(resp.json())
    assert {a.approach for a in report.approaches} == {"pipeline", "baseline"}
    assert report.cost.total_tokens > 0
    assert report.ablation is not None


def test_unknown_run_404(client):
    assert client.get("/api/runs/run-nope").status_code == 404


def test_create_run_unknown_fault_404(client):
    assert client.post("/api/runs", json={"fault_id": "nope"}).status_code == 404


# ------------------------------------------------------- end-to-end mock runs


def test_auto_resolving_run_surfaces_full_trace(client):
    run_id = client.post("/api/runs",
                         json={"fault_id": "db_pool_exhaustion"}).json()["id"]
    detail = _poll(client, run_id, until=lambda d: d.status in
                   ("resolved", "rolled_back", "failed", "rejected"))
    assert detail.status == "resolved"
    assert detail.resolved and not detail.escalated
    stages = [e.stage for e in detail.events]
    assert stages == ["ingest", "triage", "remediation", "gate",
                      "execution", "verification", "outcome"]
    # the trace carries diagnosis confidence and real per-stage token/cost
    triage = next(e for e in detail.events if e.stage == "triage")
    assert triage.confidence and triage.tokens > 0 and triage.cost_usd > 0
    assert detail.total_tokens > 0 and detail.est_cost_usd > 0
    assert detail.top_cause


def test_escalation_can_be_rejected_by_operator(client):
    # expired_credential: no in-vocabulary fix -> planner declines -> gate.
    run_id = client.post("/api/runs",
                         json={"fault_id": "expired_credential"}).json()["id"]
    _poll(client, run_id, until=lambda d: d.status == "awaiting_approval")

    approval = ApprovalView.model_validate(
        client.get(f"/api/runs/{run_id}/approval").json())
    assert approval.reasons and approval.proposal.escalate

    resp = client.post(f"/api/runs/{run_id}/decision",
                       json={"action": "reject", "note": "rotate the credential"})
    assert resp.status_code == 200
    detail = _poll(client, run_id, until=lambda d: d.status in TERMINAL_OK)
    assert detail.status == "rejected"
    assert not detail.resolved and not detail.rolled_back
    assert detail.events[-1].stage == "outcome"


def test_escalation_can_be_approved_to_resolution(client):
    # worker_scaled_to_zero: the only fix (scale) is destructive -> must pass the
    # gate; the operator approves and it resolves.
    run_id = client.post("/api/runs",
                         json={"fault_id": "worker_scaled_to_zero"}).json()["id"]
    _poll(client, run_id, until=lambda d: d.status == "awaiting_approval")

    resp = client.post(f"/api/runs/{run_id}/decision", json={"action": "approve"})
    assert resp.status_code == 200
    detail = _poll(client, run_id, until=lambda d: d.status in TERMINAL_OK)
    assert detail.status == "resolved" and detail.resolved
    assert detail.escalated and any(e.stage == "execution" for e in detail.events)


def test_decision_rejected_when_not_awaiting(client):
    run_id = client.post("/api/runs",
                         json={"fault_id": "db_pool_exhaustion"}).json()["id"]
    _poll(client, run_id, until=lambda d: d.status == "resolved")
    resp = client.post(f"/api/runs/{run_id}/decision", json={"action": "approve"})
    assert resp.status_code == 409


def test_run_appears_in_list(client):
    run_id = client.post("/api/runs",
                         json={"fault_id": "bad_config_rollout"}).json()["id"]
    _poll(client, run_id, until=lambda d: d.status == "resolved")
    runs = [RunSummary.model_validate(r) for r in client.get("/api/runs").json()]
    assert any(r.id == run_id for r in runs)


def test_stream_emits_data_frames(client):
    run_id = client.post("/api/runs",
                         json={"fault_id": "downstream_timeout"}).json()["id"]
    frames = []
    with client.stream("GET", f"/api/runs/{run_id}/stream") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                frames.append(line)
            if line.startswith("event: done"):
                break
    assert frames  # at least one snapshot streamed
    # the final frame is a terminal snapshot
    import json as _json
    last = _json.loads(frames[-1][len("data: "):])
    assert last["status"] in ("resolved", "rolled_back", "rejected", "failed")
