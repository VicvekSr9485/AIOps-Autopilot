"""Incident orchestrator: the full agent loop over one incident.

ingest -> triage (reasoning tier, scoped tools) -> plan (default tier, NO
tools) -> HITL gate -> execute (infra tools only; dry-run then apply) ->
verify (telemetry only) -> auto-rollback if unresolved -> record outcome.

Every stage is span-traced; LLM spend is metered end-to-end per incident and
reported on the IncidentRunReport. Outcome recording is deterministic pipeline
code (no LLM involved), so it calls the knowledge server directly rather than
through a stage exposure.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field

from autopilot.llm.client import QwenClient
from autopilot.mcp_servers.context import RunContext
from autopilot.models import (
    ExecutionResult,
    Incident,
    RemediationProposal,
    TriageResult,
    VerificationResult,
)
from autopilot.pipeline.executor import execute
from autopilot.pipeline.hitl import Approver, GateOutcome, hitl_gate
from autopilot.pipeline.remediation import plan_remediation
from autopilot.pipeline.triage import ContextMode, run_triage
from autopilot.pipeline.verify import verify
from autopilot.tracing import span

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = structlog.get_logger("autopilot.pipeline.run")


class LLMSpend(BaseModel):
    """End-to-end LLM spend for one incident (local estimate; see CostMeter)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0.0
    steps: list[str] = Field(default_factory=list)


class IncidentRunReport(BaseModel):
    incident_id: str
    resolved: bool
    triage: TriageResult
    proposal: RemediationProposal
    gate: GateOutcome
    execution: ExecutionResult | None = None
    verification: VerificationResult | None = None
    rolled_back: bool = False
    outcome_recorded: bool = False
    llm: LLMSpend
    # wall-clock seconds per stage (diagnosis time = stage_seconds["triage"])
    stage_seconds: dict[str, float] = Field(default_factory=dict)


async def _record_outcome(servers: Mapping[str, FastMCP], incident: Incident,
                          report_fields: dict) -> bool:
    try:
        await servers["knowledge"].call_tool("record_outcome", report_fields)
        return True
    except Exception as e:
        # Recording is best-effort bookkeeping; never fail the run over it.
        log.warning("outcome_record_failed", step="pipeline.run",
                    incident_id=incident.id, error=str(e)[:200])
        return False


def _spend_since(client: QwenClient, start_index: int) -> LLMSpend:
    records = client.meter.records[start_index:]
    return LLMSpend(
        calls=len(records),
        input_tokens=sum(r.input_tokens for r in records),
        output_tokens=sum(r.output_tokens for r in records),
        est_cost_usd=round(sum(r.est_cost_usd for r in records), 6),
        steps=[r.step for r in records],
    )


async def run_incident(
    incident: Incident,
    servers: Mapping[str, FastMCP],
    client: QwenClient,
    approver: Approver,
    context: RunContext,
    *,
    confidence_threshold: float = 0.75,
    risk_threshold: float = 0.4,
    verify_interval_s: float = 1.0,
    verify_settle_s: float = 0.0,
    context_mode: ContextMode = "summarized",
) -> IncidentRunReport:
    meter_start = len(client.meter.records)
    context.incident_id = incident.id  # server-side binding for record_outcome
    stage_seconds: dict[str, float] = {}

    def _timed(stage: str, t0: float) -> None:
        stage_seconds[stage] = round(time.perf_counter() - t0, 4)

    with span("incident_run", incident_id=incident.id):
        t0 = time.perf_counter()
        triage = await run_triage(incident, servers, client,
                                  context_mode=context_mode)
        _timed("triage", t0)
        t0 = time.perf_counter()
        proposal = plan_remediation(triage, client)  # structurally toolless
        _timed("remediation", t0)
        gate = hitl_gate(
            triage.top, proposal, approver,
            confidence_threshold=confidence_threshold,
            risk_threshold=risk_threshold,
        )

        execution: ExecutionResult | None = None
        verification: VerificationResult | None = None
        rolled_back = False
        resolved = False

        if gate.approved:
            t0 = time.perf_counter()
            execution = await execute(gate.proposal, servers)
            _timed("execution", t0)
            t0 = time.perf_counter()
            verification = await verify(incident.id, servers,
                                        interval_s=verify_interval_s,
                                        settle_timeout_s=verify_settle_s)
            _timed("verification", t0)
            resolved = execution.success and verification.resolved
            if not resolved and gate.proposal.rollback_plan:
                rollback_result = await execute(
                    gate.proposal, servers, use_rollback_plan=True
                )
                rolled_back = True
                log.info("auto_rollback", step="pipeline.run",
                         incident_id=incident.id,
                         rollback_success=rollback_result.success)

        notes = (
            f"gate={gate.route}"
            + (f" human={gate.human_action}" if gate.human_action else "")
            + (" auto_rolled_back" if rolled_back else "")
            + (f" reasons={'; '.join(gate.escalation_reasons)}"
               if gate.escalation_reasons else "")
        )
        remediation_text = (
            "; ".join(f"{s.action} {s.target}" for s in gate.proposal.steps)
            if gate.approved else "rejected at HITL gate — not executed"
        )
        recorded = await _record_outcome(servers, incident, {
            "summary": incident.title,
            "root_cause": triage.top.cause,
            "remediation": remediation_text,
            "resolved": resolved,
            "notes": notes,
        })

        report = IncidentRunReport(
            incident_id=incident.id,
            resolved=resolved,
            triage=triage,
            proposal=proposal,
            gate=gate,
            execution=execution,
            verification=verification,
            rolled_back=rolled_back,
            outcome_recorded=recorded,
            llm=_spend_since(client, meter_start),
            stage_seconds=stage_seconds,
        )
        log.info(
            "incident_run_done", step="pipeline.run", incident_id=incident.id,
            resolved=resolved, gate_route=gate.route, approved=gate.approved,
            rolled_back=rolled_back, llm_calls=report.llm.calls,
            est_cost_usd=report.llm.est_cost_usd,
        )
        return report
