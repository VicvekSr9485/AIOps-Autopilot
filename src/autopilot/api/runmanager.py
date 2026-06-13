"""RunManager: drives the agent pipeline for the demo and exposes it live.

Each run executes the real pipeline stages (triage -> plan -> HITL gate ->
execute -> verify -> auto-rollback) over an offline MockWorld (fault-aware mock
sandbox + deterministic mock model — no Docker, no tokens, no network), so the
dashboard works anywhere including CI. A run lives on its own thread (its own
asyncio loop); per-stage trace events with token/cost are published to a
thread-safe RunState the API reads.

The HITL gate is the one place a run PAUSES: ApiApprover.decide blocks on a
threading.Event until the operator POSTs approve/edit/reject. This is why the
pipeline's synchronous Approver protocol is reused unchanged — the web human
sits behind the exact same interface the benchmark's oracle and the CLI do.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime

import structlog

from autopilot.api.schemas import (
    ApprovalView,
    EvidenceView,
    HypothesisView,
    ProposalView,
    RunDetail,
    RunStatus,
    RunSummary,
    ScenarioInfo,
    StepView,
    TraceEventView,
)
from autopilot.benchmark.mockenv import HeuristicMockClient, MockWorld
from autopilot.harness.synthetic import FAULT_IDS
from autopilot.llm.client import QwenClient
from autopilot.models import RemediationProposal, RemediationStep
from autopilot.pipeline.executor import execute
from autopilot.pipeline.hitl import (
    ApprovalRequest,
    Approver,
    HumanDecision,
    hitl_gate,
)
from autopilot.pipeline.remediation import PlanningError, plan_remediation
from autopilot.pipeline.triage import TriageError, run_triage
from autopilot.pipeline.verify import verify

log = structlog.get_logger("autopilot.api.runmanager")

TERMINAL: frozenset[RunStatus] = frozenset(
    {"resolved", "rolled_back", "rejected", "failed"}
)


def _steps_to_views(steps: list[RemediationStep]) -> list[StepView]:
    out = []
    for s in steps:
        params = json.loads(s.command) if s.command else {}
        out.append(StepView(order=s.order, action=s.action, target=s.target,
                            params=params, expected_effect=s.expected_effect))
    return out


def _proposal_view(p: RemediationProposal) -> ProposalView:
    return ProposalView(
        id=p.id, steps=_steps_to_views(p.steps),
        rollback_plan=_steps_to_views(p.rollback_plan),
        risk_score=round(p.risk_score, 3),
        remediation_confidence=round(p.remediation_confidence, 3),
        blast_radius=p.blast_radius, escalate=p.escalate,
    )


class RunState:
    """Thread-safe live state for one run. The worker thread writes; the API
    (event loop) reads under the same lock."""

    def __init__(self, run_id: str, fault_id: str, scenario_title: str):
        self.id = run_id
        self.fault_id = fault_id
        self.scenario_title = scenario_title
        self.status: RunStatus = "running"
        self.started_at = datetime.now(UTC).isoformat()
        self.events: list[TraceEventView] = []
        self.approval: ApprovalView | None = None
        self.resolved = False
        self.rolled_back = False
        self.escalated = False
        self.top_cause: str | None = None
        self.top_confidence: float | None = None
        self.total_tokens = 0
        self.est_cost_usd = 0.0
        self.revision = 0  # bumps on every observable change (SSE change-detect)

        self._lock = threading.Lock()
        self._t0 = time.perf_counter()
        # HITL handshake
        self._decision_event = threading.Event()
        self._decision: HumanDecision | None = None
        self._pending_proposal: RemediationProposal | None = None

    # ----------------------------------------------------------- writes (worker)

    def emit(self, event: TraceEventView) -> None:
        with self._lock:
            event.elapsed_s = round(time.perf_counter() - self._t0, 3)
            self.events.append(event)
            self.total_tokens += event.tokens
            self.est_cost_usd = round(self.est_cost_usd + event.cost_usd, 6)
            self.revision += 1

    def set_status(self, status: RunStatus) -> None:
        with self._lock:
            self.status = status
            self.revision += 1

    def open_gate(self, view: ApprovalView, proposal: RemediationProposal) -> None:
        with self._lock:
            self.approval = view
            self._pending_proposal = proposal
            self.status = "awaiting_approval"
            self.escalated = True
            self.revision += 1

    def close_gate(self) -> None:
        with self._lock:
            self.approval = None
            self.status = "running"
            self.revision += 1

    # ------------------------------------------------------------ reads (API)

    def snapshot(self) -> tuple[int, RunDetail]:
        with self._lock:
            detail = RunDetail(
                id=self.id, fault_id=self.fault_id,
                scenario_title=self.scenario_title, status=self.status,
                resolved=self.resolved, rolled_back=self.rolled_back,
                escalated=self.escalated, total_tokens=self.total_tokens,
                est_cost_usd=self.est_cost_usd, started_at=self.started_at,
                top_cause=self.top_cause, top_confidence=self.top_confidence,
                events=list(self.events),
                approval=self.approval,
            )
            return self.revision, detail

    def summary(self) -> RunSummary:
        _, d = self.snapshot()
        return RunSummary(**d.model_dump(exclude={"events", "approval"}))

    # ------------------------------------------------------------ HITL handshake

    def submit_decision(self, decision: HumanDecision) -> None:
        self._decision = decision
        self._decision_event.set()

    def wait_for_decision(self) -> HumanDecision:
        self._decision_event.wait()
        assert self._decision is not None
        return self._decision


class ApiApprover(Approver):
    """The web operator behind the pipeline's Approver protocol: publishes the
    pending decision, blocks the run's worker thread until the API delivers it."""

    def __init__(self, run: RunState):
        self.run = run

    def decide(self, request: ApprovalRequest) -> HumanDecision:
        self.run.open_gate(
            ApprovalView(
                incident_id=request.incident_id,
                hypothesis_cause=request.hypothesis.cause,
                hypothesis_confidence=round(request.hypothesis.confidence, 3),
                reasons=request.escalation_reasons,
                proposal=_proposal_view(request.proposal),
            ),
            request.proposal,
        )
        self.run.emit(TraceEventView(
            stage="gate", title="Escalated to human", status="warn",
            detail="; ".join(request.escalation_reasons),
            payload={"reasons": request.escalation_reasons},
        ))
        decision = self.run.wait_for_decision()
        self.run.close_gate()
        return decision


class RunManager:
    """Owns all runs and the scenario catalog. One instance per app."""

    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()
        self._catalog: list[ScenarioInfo] = []

    # ------------------------------------------------------------ catalog

    def scenarios(self) -> list[ScenarioInfo]:
        if not self._catalog:
            for fault_id in FAULT_IDS:
                # the alert-derived title is exactly what the agent will see
                title = MockWorld(fault_id).incident.title
                self._catalog.append(ScenarioInfo(fault_id=fault_id, title=title))
        return self._catalog

    # ------------------------------------------------------------ runs

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def list_runs(self) -> list[RunSummary]:
        with self._lock:
            runs = list(self._runs.values())
        return [r.summary() for r in sorted(runs, key=lambda r: r.started_at,
                                            reverse=True)]

    def start(self, fault_id: str) -> RunState:
        if fault_id not in FAULT_IDS:
            raise KeyError(fault_id)
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        title = next((s.title for s in self.scenarios() if s.fault_id == fault_id),
                     fault_id)
        run = RunState(run_id, fault_id, title)
        with self._lock:
            self._runs[run_id] = run
        threading.Thread(target=self._worker, args=(run,), daemon=True).start()
        log.info("run_started", step="api", run_id=run_id, fault_id=fault_id)
        return run

    # ------------------------------------------------------------ worker thread

    def _worker(self, run: RunState) -> None:
        try:
            asyncio.run(self._drive(run))
        except Exception as e:  # never let a worker thread die silently
            log.warning("run_worker_crashed", step="api", run_id=run.id,
                        error=str(e)[:300])
            run.emit(TraceEventView(stage="outcome", title="Run failed",
                                    status="error", detail=str(e)[:300]))
            run.set_status("failed")

    def _slice(self, client: QwenClient, start: int) -> tuple[int, float]:
        recs = client.meter.records[start:]
        return (sum(r.input_tokens + r.output_tokens for r in recs),
                round(sum(r.est_cost_usd for r in recs), 6))

    async def _drive(self, run: RunState) -> None:
        world = MockWorld(run.fault_id)
        client = HeuristicMockClient()
        approver = ApiApprover(run)
        incident = world.incident

        run.emit(TraceEventView(
            stage="ingest", title="Incident ingested", status="info",
            detail=incident.title,
            payload={"incident_id": incident.id,
                     "logs": len(incident.telemetry.logs),
                     "metrics": len(incident.telemetry.metrics)},
        ))

        # --- triage / root cause (reasoning tier) -------------------------------
        idx = len(client.meter.records)
        try:
            triage = await run_triage(incident, world.servers, client)
        except TriageError as e:
            run.emit(TraceEventView(stage="triage", title="Triage failed",
                                    status="error", detail=str(e)[:200]))
            run.set_status("failed")
            return
        tok, cost = self._slice(client, idx)
        top = triage.top
        run.top_cause = top.cause
        run.top_confidence = round(top.confidence, 3)
        run.emit(TraceEventView(
            stage="triage", title="Root cause diagnosed", status="ok",
            detail=top.cause, confidence=round(top.confidence, 3),
            tokens=tok, cost_usd=cost,
            payload={
                "hypotheses": [
                    HypothesisView(
                        cause=h.cause, confidence=round(h.confidence, 3),
                        reasoning_summary=h.reasoning_summary,
                        evidence=[EvidenceView(kind=e.kind, pointer=e.pointer,
                                               excerpt=e.excerpt)
                                  for e in h.evidence],
                    ).model_dump() for h in triage.hypotheses
                ],
                "runbooks": triage.consulted_runbooks,
            },
        ))

        # --- remediation planning (planning tier) -------------------------------
        idx = len(client.meter.records)
        try:
            proposal = plan_remediation(triage, client)
        except PlanningError as e:
            run.emit(TraceEventView(stage="remediation", title="Planning failed",
                                    status="error", detail=str(e)[:200]))
            run.set_status("failed")
            return
        tok, cost = self._slice(client, idx)
        if proposal.escalate:
            detail = "No safe in-vocabulary remediation — declining to act"
        else:
            detail = " -> ".join(f"{s.action} {s.target}" for s in proposal.steps)
        run.emit(TraceEventView(
            stage="remediation", title="Remediation planned", status="ok",
            detail=detail, confidence=round(proposal.remediation_confidence, 3),
            tokens=tok, cost_usd=cost,
            payload={"proposal": _proposal_view(proposal).model_dump()},
        ))

        # --- HITL gate (auto-approve or PAUSE for the operator) -----------------
        gate = hitl_gate(top, proposal, approver,
                         confidence_threshold=0.75, risk_threshold=0.4)
        if gate.route == "auto":
            run.emit(TraceEventView(
                stage="gate", title="Auto-approved", status="ok",
                detail=f"remediation confidence {proposal.remediation_confidence:.2f} "
                       f"≥ 0.75 and risk {proposal.risk_score:.2f} ≤ 0.40; "
                       "non-destructive",
            ))
        else:
            run.emit(TraceEventView(
                stage="gate",
                title=f"Operator {gate.human_action}d the plan",
                status="ok" if gate.approved else "warn",
                detail=gate.note or ("approved by operator" if gate.approved
                                     else "rejected by operator"),
                payload={"human_action": gate.human_action},
            ))

        if not gate.approved:
            run.emit(TraceEventView(
                stage="outcome", title="Escalated and rejected", status="warn",
                detail="No action taken — handed to a human for out-of-band fix "
                       "(e.g. credential rotation). The sandbox was never touched.",
            ))
            run.set_status("rejected")
            return

        # --- execute (infra tools only; dry-run then apply) ---------------------
        execution = await execute(gate.proposal, world.servers)
        run.emit(TraceEventView(
            stage="execution",
            title="Remediation executed" if execution.success
            else "Execution failed",
            status="ok" if execution.success else "error",
            detail="; ".join(
                f"step {o.step_order}: {'ok' if o.success else 'FAILED'}"
                + (f" — {o.output}" if o.output and not o.success else "")
                for o in execution.step_outcomes) or (execution.error or ""),
            payload={"success": execution.success},
        ))

        # --- verify (telemetry only; backlog-aware) -----------------------------
        verification = await verify(incident.id, world.servers, interval_s=0.0)
        run.emit(TraceEventView(
            stage="verification",
            title="Resolution verified" if verification.resolved
            else "Verification failed",
            status="ok" if verification.resolved else "warn",
            detail="; ".join(f"{c.name}: {'pass' if c.passed else 'FAIL'}"
                             for c in verification.checks),
            payload={"checks": [c.model_dump() for c in verification.checks]},
        ))

        resolved = execution.success and verification.resolved

        # --- auto-rollback on failure (damage containment) ----------------------
        if not resolved and gate.proposal.rollback_plan:
            rb = await execute(gate.proposal, world.servers, use_rollback_plan=True)
            run.rolled_back = True
            run.emit(TraceEventView(
                stage="rollback", title="Auto-rolled back", status="warn",
                detail="Remediation did not restore health; the agent reverted "
                       "its own change so the sandbox is left contained, not "
                       f"further broken (rollback {'ok' if rb.success else 'failed'}).",
                payload={"success": rb.success},
            ))

        run.resolved = resolved
        if resolved:
            run.emit(TraceEventView(stage="outcome", title="Incident resolved",
                                    status="ok",
                                    detail="Health verified restored end-to-end."))
            run.set_status("resolved")
        else:
            run.set_status("rolled_back")


_MANAGER: RunManager | None = None


def get_manager() -> RunManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = RunManager()
    return _MANAGER
