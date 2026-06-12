"""Offline benchmark environment: a fault-aware mock sandbox + a deterministic
heuristic mock model, so the FULL benchmark (both approaches, ablation, report)
runs to completion with zero Docker and zero network.

The mock sandbox is a tiny state machine: it serves the fault's synthetic
symptoms until a remediation that would genuinely fix the fault (per
scoring.FIXING_ACTIONS) is applied, then reads healthy — so resolution,
false-remediation, and rollback paths all behave like the real stack.

The heuristic mock model infers a cause/plan from OBSERVABLE prompt keywords
only (never ground truth) and derives token counts from prompt length, so the
summarization ablation produces genuinely different token figures in mock mode.
"""

from __future__ import annotations

import json

import structlog

from autopilot.benchmark.scoring import remediation_fixes
from autopilot.config import LLMConfig
from autopilot.harness.synthetic import healthy_snap, scenario_capture
from autopilot.llm.client import QwenClient
from autopilot.mcp_servers.context import RunContext
from autopilot.mcp_servers.infra import build_infra_server
from autopilot.mcp_servers.knowledge import build_knowledge_server
from autopilot.mcp_servers.store import KnowledgeStore
from autopilot.mcp_servers.telemetry import build_telemetry_server
from autopilot.models import Incident
from autopilot.pipeline.ingest import ingest
from autopilot.sandbox.controller import (
    ProbeSnapshot,
    RequestObservation,
    SandboxController,
)

log = structlog.get_logger("autopilot.benchmark.mockenv")


class MockSandboxController(SandboxController):
    """Fault-aware offline stand-in: never touches Docker; flips from the
    fault's synthetic symptoms to healthy once a genuinely-fixing action lands."""

    def __init__(self, fault_id: str):
        super().__init__()
        self.fault_id = fault_id
        self.fixed = False
        self.calls: list[tuple] = []
        self._log_text, self._fault_snaps = scenario_capture(fault_id)
        self._probe_i = 0
        self._config = self.default_app_config()
        if fault_id == "bad_config_rollout":
            # mirror the injected state, else the rollback tool would no-op
            self._config["feature_mode"] = "turbo_v2"

    def _compose(self, *args, check=True):
        raise AssertionError("docker compose must never run in the mock benchmark")

    # ------------------------------------------------------------ observation

    def probe(self) -> ProbeSnapshot:
        if self.fixed:
            return healthy_snap()
        snap = self._fault_snaps[self._probe_i % len(self._fault_snaps)]
        self._probe_i += 1
        return snap

    def logs(self, since=None) -> str:
        return self._log_text

    def timed_request(self, path: str) -> RequestObservation:
        snap = self.probe()
        return RequestObservation(
            path=path, started_at=snap.captured_at,
            status=snap.work_status, latency_ms=10.0, body=snap.work_body,
        )

    # --------------------------------------------------------------- mutation

    def restart(self, service: str) -> None:
        self.calls.append(("restart", service))
        if remediation_fixes(self.fault_id, "restart_service", service):
            self.fixed = True

    def scale(self, service: str, replicas: int) -> None:
        self.calls.append(("scale", service, replicas))
        if remediation_fixes(self.fault_id, "scale_service", service,
                             {"replicas": replicas}):
            self.fixed = True

    def read_app_config(self) -> dict:
        return dict(self._config)

    def write_app_config(self, config: dict) -> None:
        self._config = dict(config)
        self.calls.append(("write_app_config", json.dumps(config, sort_keys=True)))
        # rollback/apply_config both land here; fixing is judged on final state
        if (self.fault_id == "bad_config_rollout"
                and config.get("feature_mode") == "standard"):
            self.fixed = True


class MockWorld:
    """One scenario's worth of offline wiring: incident from the synthetic
    capture, the three MCP servers over a fault-aware mock controller, a seeded
    in-memory knowledge store, and a run-bound context."""

    def __init__(self, fault_id: str):
        self.fault_id = fault_id
        self.ctrl = MockSandboxController(fault_id)
        log_text, snapshots = scenario_capture(fault_id)
        self.incident: Incident = ingest(log_text, snapshots)
        self.store = KnowledgeStore(":memory:")
        self.context = RunContext()
        self.servers = {
            "telemetry": build_telemetry_server(self.ctrl),
            "infra": build_infra_server(self.ctrl),
            "knowledge": build_knowledge_server(store=self.store, context=self.context),
        }

    def cleanup(self) -> None:  # symmetry with LiveWorld; nothing to revert
        pass


# --------------------------------------------------------------- mock model


_CAUSES: dict[str, tuple[str, float]] = {
    # fault inferred from symptoms -> (cause text, confidence)
    "db_pool_exhaustion": (
        "db connection slots exhausted by long-running idle sessions", 0.86),
    "bad_config_rollout": (
        "config rollout set feature_mode to an invalid value", 0.9),
    "downstream_timeout": (
        "downstream dependency stopped responding; requests time out", 0.82),
    "queue_consumer_stall": (
        "queue consumer (worker) stalled; jobs accumulate unprocessed", 0.8),
    # ambiguous on purpose: looks db-shaped, model is unsure -> escalation path
    "expired_credential": (
        "database rejecting connections: password authentication failing", 0.55),
}

_PLANS: dict[str, dict] = {
    "db_pool_exhaustion": {"action": "restart_service", "target": "db"},
    "bad_config_rollout": {"action": "rollback", "target": "app"},
    "downstream_timeout": {"action": "restart_service", "target": "downstream"},
    "queue_consumer_stall": {"action": "restart_service", "target": "worker"},
    "expired_credential": {"action": "restart_service", "target": "db"},  # wrong fix
}


def _infer_fault(text: str) -> str | None:
    """Symptom keywords -> fault guess. Matches both raw telemetry tokens (the
    triage/baseline prompts) and hypothesis phrasings (the planner prompt, whose
    input is _CAUSES text rather than log lines). Observable signals only."""
    t = text.lower()
    if "password authentication" in t or "credential" in t:
        return "expired_credential"
    if "connection slots" in t or "too many clients" in t or "idle session" in t:
        return "db_pool_exhaustion"
    if "feature_mode" in t:
        return "bad_config_rollout"
    if "downstream" in t and ("timeout" in t or "time out" in t):
        return "downstream_timeout"
    if ("queue_depth" in t and "jobs_processed" in t) or \
            ("consumer" in t and "stall" in t):
        return "queue_consumer_stall"
    return None


def _triage_json(fault: str | None) -> str:
    if fault is None:
        return json.dumps({"hypotheses": [
            {"cause": "unknown degradation", "confidence": 0.3, "evidence": [],
             "reasoning_summary": "no recognizable symptom signature"}]})
    cause, confidence = _CAUSES[fault]
    return json.dumps({"hypotheses": [
        {"cause": cause, "confidence": confidence,
         "evidence": [{"kind": "log", "pointer": "log:app",
                       "excerpt": "see grouped log summary"}],
         "reasoning_summary": "dominant log/metric signature matches this cause"},
        {"cause": "transient network blip", "confidence": 0.1,
         "evidence": [{"kind": "alert", "pointer": "alert:sandbox"}],
         "reasoning_summary": "weak alternative"},
    ]})


def _plan_steps(fault: str | None) -> list[dict]:
    step = _PLANS.get(fault or "", {"action": "restart_service", "target": "app"})
    return [{**step, "params": {}, "expected_effect": "service converges to healthy"}]


def _plan_json(fault: str | None) -> str:
    return json.dumps({
        "steps": _plan_steps(fault),
        "rollback_plan": [{"action": "rollback", "target": "app", "params": {},
                           "expected_effect": "restore canonical config"}],
        "risk_score": 0.2,
        "blast_radius": "single_service",
    })


def _baseline_json(fault: str | None) -> str:
    cause, confidence = _CAUSES.get(fault or "", ("unknown degradation", 0.3))
    return json.dumps({
        "root_cause": cause,
        "confidence": confidence,
        "steps": _plan_steps(fault),
    })


class HeuristicMockClient(QwenClient):
    """Deterministic mock-mode client for benchmark development runs.

    Routes on the system prompt (triage / planner / baseline), infers the fault
    from observable prompt keywords, and answers with valid stage JSON. Token
    counts are length-derived (~4 chars/token), so prompt size differences —
    e.g. the summarization ablation — show up in the metrics for real.
    """

    def __init__(self):
        super().__init__(config=LLMConfig(mock_mode=True))

    def _mock_complete(self, model, messages):
        system = messages[0].get("content", "")
        blob = " ".join(m.get("content", "") for m in messages[1:])
        # infer from the incident's own telemetry/hypothesis only — runbook
        # excerpts in the prompt may describe OTHER faults' symptoms
        blob = blob.split("RELEVANT RUNBOOKS", 1)[0]  # triage prompt section
        blob = blob.split("RUNBOOK GUIDANCE", 1)[0]  # planner prompt section
        fault = _infer_fault(blob)
        if "root-cause analyst" in system:
            text = _triage_json(fault)
        elif "remediation planner" in system:
            text = _plan_json(fault)
        elif "single-prompt" in system:
            text = _baseline_json(fault)
        else:  # pragma: no cover - no other prompts exist
            raise AssertionError(f"unrecognized system prompt: {system[:80]}")
        input_tokens = max(1, sum(len(m.get("content", "")) for m in messages) // 4)
        output_tokens = max(1, len(text) // 4)
        log.info("heuristic_mock_completed", step="benchmark.mock",
                 inferred_fault=fault, input_tokens=input_tokens)
        return text, input_tokens, output_tokens
