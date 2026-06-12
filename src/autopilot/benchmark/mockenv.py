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
import re

import structlog

from autopilot.benchmark.scoring import fixing_step_key, steps_fix
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


# Live db log lines that exist ONLY post-capture: the decisive detail for the
# tool-disambiguation fault, reachable through a live query_logs call.
_AMBIGUOUS_LIVE_DB_LOGS = "\n".join(
    f"autopilot-sbx-db  | 2026-06-12T12:05:0{i}.000000000Z FATAL:  remaining "
    "connection slots are reserved for roles with privileges"
    for i in range(3)
)


class MockSandboxController(SandboxController):
    """Fault-aware offline stand-in: never touches Docker; flips from the
    fault's synthetic symptoms to healthy once the applied actions cover a full
    fixing alternative (scoring.REQUIRED_FIX_STEPS — multi-step faults need
    every step). Partial fixes surface honest intermediate states, and metric
    series extend monotonically so re-probing cannot dodge a divergence."""

    def __init__(self, fault_id: str):
        super().__init__()
        self.fault_id = fault_id
        self.calls: list[tuple] = []
        self._satisfied: set[tuple[str, str]] = set()
        self._log_text, self._fault_snaps = scenario_capture(fault_id)
        self._probe_i = 0
        self._config = self.default_app_config()
        if fault_id in ("bad_config_rollout", "config_rollout_worker_wedge"):
            # mirror the injected state, else the rollback tool would no-op
            self._config["feature_mode"] = "turbo_v2"

    def _compose(self, *args, check=True):
        raise AssertionError("docker compose must never run in the mock benchmark")

    @property
    def fixed(self) -> bool:
        return steps_fix(self.fault_id, self._satisfied)

    def _mark(self, action: str, target: str, params: dict | None = None) -> None:
        key = fixing_step_key(self.fault_id, action, target, params)
        if key is not None:
            self._satisfied.add(key)

    # ------------------------------------------------------------ observation

    def _extend_metrics(self, snap: ProbeSnapshot, steps_past_end: int
                        ) -> ProbeSnapshot:
        """Continue the capture's metric trend instead of cycling: an unfixed
        divergence keeps diverging, a stuck backlog stays stuck."""
        if not snap.metrics or len(self._fault_snaps) < 2:
            return snap
        prev = self._fault_snaps[-2]
        if not prev.metrics:
            return snap
        extended = {
            name: value + steps_past_end * (value - prev.metrics.get(name, value))
            for name, value in snap.metrics.items()
        }
        return snap.model_copy(update={"metrics": extended})

    def probe(self) -> ProbeSnapshot:
        if self.fixed:
            return healthy_snap()
        if (self.fault_id == "config_rollout_worker_wedge"
                and ("rollback", "app") in self._satisfied):
            # honest partial state: config rolled back so probes go green, but
            # the wedged consumer still is not draining the backlog
            return healthy_snap(queue_depth=12, jobs_processed=37)
        snaps = self._fault_snaps
        i = self._probe_i
        self._probe_i += 1
        if i < len(snaps):
            return snaps[i]
        return self._extend_metrics(snaps[-1], i - len(snaps) + 1)

    def logs(self, since=None) -> str:
        if self.fault_id == "db_outage_ambiguous" and not self.fixed:
            return f"{self._log_text}\n{_AMBIGUOUS_LIVE_DB_LOGS}"
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
        self._mark("restart_service", service)

    def scale(self, service: str, replicas: int) -> None:
        self.calls.append(("scale", service, replicas))
        self._mark("scale_service", service, {"replicas": replicas})

    def read_app_config(self) -> dict:
        return dict(self._config)

    def write_app_config(self, config: dict) -> None:
        self._config = dict(config)
        self.calls.append(("write_app_config", json.dumps(config, sort_keys=True)))
        # rollback/apply_config both land here; fixing is judged on final state
        self._mark("apply_config", "app", config)


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
    "config_rollout_worker_wedge": (
        "config rollout set feature_mode to an invalid value and left the queue "
        "consumer wedged; the backlog is stuck undrained", 0.85),
    "worker_scaled_to_zero": (
        "worker consumers were scaled away (SIGTERM shutdown observed); queue "
        "backlog growing with no consumers running", 0.8),
    # db_outage_ambiguous never appears here: once the live db logs reveal the
    # connection-slot FATALs it is diagnosed AS pool exhaustion; without them
    # (the no-tool baseline) the symptoms support no specific cause at all.
}

# Base plans: the action an unaided reader of the symptoms would reach for.
_PLANS: dict[str, list[dict]] = {
    "db_pool_exhaustion": [{"action": "restart_service", "target": "db"}],
    "bad_config_rollout": [{"action": "rollback", "target": "app"}],
    "downstream_timeout": [{"action": "restart_service", "target": "downstream"}],
    "queue_consumer_stall": [{"action": "restart_service", "target": "worker"}],
    "expired_credential": [{"action": "restart_service", "target": "db"}],  # wrong fix
    # tempting-but-incomplete: fixes the loud half, leaves the wedged consumer
    "config_rollout_worker_wedge": [{"action": "rollback", "target": "app"}],
    # tempting reflex: restart the missing worker (a no-op at zero replicas)
    "worker_scaled_to_zero": [{"action": "restart_service", "target": "worker"}],
}

# Operational knowledge that only arrives via retrieved runbooks: when the
# marker phrase from the matching runbook is present in the prompt's runbook
# section, the plan is refined. Models a planner following runbook guidance —
# the no-retrieval baseline never sees these.
_RUNBOOK_REFINED_PLANS: dict[str, tuple[str, list[dict]]] = {
    "config_rollout_worker_wedge": (
        "restart the consumer as well",
        [{"action": "rollback", "target": "app"},
         {"action": "restart_service", "target": "worker"}],
    ),
    "worker_scaled_to_zero": (
        "scale the consumer back",
        [{"action": "scale_service", "target": "worker", "params": {"replicas": 1}}],
    ),
}


def _metric_series(text: str, name: str) -> list[float]:
    """Pull a metric's observed values out of prompt text, across the formats
    the prompts actually use: 'name: first=A last=B' (summaries/live sections)
    and 'name=V' point dumps (the ablation's raw rendering)."""
    pairs = re.findall(rf"{name}: first=([\d.]+) last=([\d.]+)", text)
    if pairs:
        return [float(v) for pair in pairs for v in pair]
    return [float(v) for v in re.findall(rf"{name}=([\d.]+)", text)]


def _queue_signature(text: str) -> str | None:
    """'growing' | 'stuck' | None, from observable metric text only."""
    depth = _metric_series(text, "queue_depth")
    processed = _metric_series(text, "jobs_processed")
    if len(depth) < 2 or len(processed) < 2 or processed[-1] != processed[0]:
        return None
    if depth[-1] > depth[0]:
        return "growing"
    if depth[-1] == depth[0] and depth[-1] > 0:
        return "stuck"
    return None


def _infer_fault(text: str) -> str | None:
    """Symptom keywords/patterns -> fault guess. Matches the telemetry-derived
    sections of triage/baseline/planner prompts (summaries, live log groups,
    live metric windows) and hypothesis phrasings. Observable signals only —
    most-specific rules first."""
    t = text.lower()
    queue_sig = _queue_signature(t)
    if "password authentication" in t or "credential" in t:
        return "expired_credential"
    if "connection slots" in t or "too many clients" in t or "idle session" in t:
        return "db_pool_exhaustion"
    if "feature_mode" in t and (queue_sig is not None or "wedged" in t):
        return "config_rollout_worker_wedge"
    if "feature_mode" in t:
        return "bad_config_rollout"
    if "downstream" in t and ("timeout" in t or "time out" in t):
        return "downstream_timeout"
    if queue_sig == "growing" and ("worker_shutdown" in t or "sigterm" in t
                                   or "scaled" in t):
        return "worker_scaled_to_zero"
    if queue_sig == "growing" or ("consumer" in t and "stall" in t):
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


def _plan_steps(fault: str | None, runbook_text: str = "") -> list[dict]:
    steps = _PLANS.get(fault or "", [{"action": "restart_service", "target": "app"}])
    if fault in _RUNBOOK_REFINED_PLANS:
        marker, refined = _RUNBOOK_REFINED_PLANS[fault]
        if marker in runbook_text.lower():
            steps = refined
    return [{"params": {}, "expected_effect": "service converges to healthy", **s}
            for s in steps]


def _plan_json(fault: str | None, runbook_text: str = "") -> str:
    return json.dumps({
        "steps": _plan_steps(fault, runbook_text),
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
        "steps": _plan_steps(fault),  # no retrieval: base plans only, by contract
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
        full = " ".join(m.get("content", "") for m in messages[1:])
        # The mock models a competent reader: the FAULT is inferred from the
        # prompt's PRIMARY-EVIDENCE sections (incident telemetry/symptoms), not
        # from retrieved runbook excerpts, which legitimately describe OTHER
        # faults' symptoms. Since the planner-handoff fix, every prompt's
        # primary section carries the actual symptom summary — inference no
        # longer depends on hypothesis prose happening to name the right token.
        # The runbook section is then consulted separately for PLAN refinement
        # (following guidance that matches the diagnosed fault), exactly the
        # role runbooks play for a real planner.
        blob = full.split("RELEVANT RUNBOOKS", 1)[0]  # triage prompt section
        blob = blob.split("RUNBOOK GUIDANCE", 1)[0]  # planner prompt section
        fault = _infer_fault(blob)
        if "root-cause analyst" in system:
            text = _triage_json(fault)
        elif "remediation planner" in system:
            runbook_text = full.split("RUNBOOK GUIDANCE", 1)
            text = _plan_json(fault, runbook_text[1] if len(runbook_text) > 1 else "")
        elif "single-prompt" in system:
            text = _baseline_json(fault)
        else:  # pragma: no cover - no other prompts exist
            raise AssertionError(f"unrecognized system prompt: {system[:80]}")
        input_tokens = max(1, sum(len(m.get("content", "")) for m in messages) // 4)
        output_tokens = max(1, len(text) // 4)
        log.info("heuristic_mock_completed", step="benchmark.mock",
                 inferred_fault=fault, input_tokens=input_tokens)
        return text, input_tokens, output_tokens
