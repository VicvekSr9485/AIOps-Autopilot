"""Remediation planner: top hypothesis + runbooks already retrieved by triage
-> schema-validated RemediationProposal (steps mapped to Infra/Ops actions,
rollback plan, risk/blast-radius).

Stage-scoping is STRUCTURAL here: this function takes no servers argument at
all (exposure maps planner -> no tools); runbook context arrives on the
TriageResult instead of being re-retrieved (cost rule: never re-ask).

Uses the `default` model (qwen3.7-plus) — the reasoning tier belongs to triage
alone. Server-side discipline mirrors the rest of the pipeline: incident_id and
hypothesis_cause are injected, action/target vocabularies are closed enums, a
risk floor is applied when destructive steps are present (the model's optimism
is not trusted), and requires_approval starts True — only the HITL gate may
clear it.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from autopilot.llm.client import QwenClient
from autopilot.models import RemediationProposal, RemediationStep, TriageResult
from autopilot.pipeline.structured import StructuredOutputError, complete_structured
from autopilot.tracing import span

log = structlog.get_logger("autopilot.pipeline.remediation")

STEP = "remediation.plan"
DEFAULT_TOKEN_CAP = 12_000

# The complete action vocabulary: exactly the Infra/Ops MCP tools the executor
# can invoke. The planner cannot invent actions outside this set.
ActionName = Literal["restart_service", "scale_service", "apply_config", "rollback"]

# Server-side destructive classification (never model-supplied): these remove
# capacity or overwrite state. restart/rollback converge to a known-good state.
DESTRUCTIVE_ACTIONS = frozenset({"scale_service", "apply_config"})
DESTRUCTIVE_RISK_FLOOR = 0.6


class _LLMPlanStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: ActionName
    target: Literal["app", "worker", "downstream", "db", "queue"] = "app"
    params: dict[str, Any] = Field(default_factory=dict)
    expected_effect: str = ""


class _LLMPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    steps: list[_LLMPlanStep] = Field(min_length=1, max_length=3)
    rollback_plan: list[_LLMPlanStep] = Field(default_factory=list, max_length=3)
    risk_score: float = Field(ge=0.0, le=1.0)
    blast_radius: Literal["single_service", "multiple_services", "stack_wide"]


_SYSTEM_PROMPT = (
    "You are an SRE remediation planner for a sandboxed compose stack. Given a "
    "root-cause hypothesis and runbook guidance, plan the smallest remediation.\n"
    "Available actions (the ONLY ones that exist):\n"
    "- restart_service: params {} — restart one service\n"
    "- scale_service: params {\"replicas\": 0..3} — scale a service (0 stops it)\n"
    "- apply_config: params = partial app config (keys: feature_mode, "
    "downstream_url, downstream_timeout_s); always targets the app service\n"
    "- rollback: params {} — restore the canonical app config; targets app\n"
    "Services: app, worker, downstream, db, queue.\n"
    "Respond with STRICT JSON only — no prose, no markdown — matching:\n"
    '{"steps": [{"action": str, "target": str, "params": object, '
    '"expected_effect": str}], "rollback_plan": [same shape], '
    '"risk_score": float 0..1, "blast_radius": "single_service"|'
    '"multiple_services"|"stack_wide"}\n'
    "1-3 steps, minimal blast radius. ALWAYS include a rollback_plan that "
    "restores the pre-remediation state."
)


def _to_step(order: int, s: _LLMPlanStep) -> RemediationStep:
    return RemediationStep(
        order=order,
        action=s.action,
        target="app" if s.action in ("apply_config", "rollback") else s.target,
        command=json.dumps(s.params, sort_keys=True) if s.params else None,
        expected_effect=s.expected_effect,
    )


def destructive_steps(proposal: RemediationProposal) -> list[str]:
    """Human-readable descriptions of the proposal's destructive steps."""
    return [
        f"step {s.order}: {s.action} on '{s.target}'"
        for s in proposal.steps
        if s.action in DESTRUCTIVE_ACTIONS
    ]


class PlanningError(RuntimeError):
    """Planner could not produce a valid proposal within its caps."""


def plan_remediation(
    triage: TriageResult,
    client: QwenClient,
    *,
    max_attempts: int = 3,
    token_cap: int = DEFAULT_TOKEN_CAP,
) -> RemediationProposal:
    top = triage.top
    with span("remediation", incident_id=triage.incident_id):
        evidence = "; ".join(
            f"{e.kind}:{e.pointer} ({e.excerpt[:120]})" for e in top.evidence
        ) or "none cited"
        runbooks = "\n".join(f"- {note}" for note in triage.consulted_runbooks) or "- none"
        user = (
            f"ROOT-CAUSE HYPOTHESIS (confidence {top.confidence:.2f}): {top.cause}\n"
            f"Reasoning: {top.reasoning_summary}\nEvidence: {evidence}\n\n"
            f"RUNBOOK GUIDANCE (retrieved during triage):\n{runbooks}"
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        try:
            plan, tokens_spent = complete_structured(
                client, "default", messages, _LLMPlan,
                step=STEP, max_attempts=max_attempts, token_cap=token_cap,
            )
        except StructuredOutputError as e:
            raise PlanningError(str(e)) from None

        risk = plan.risk_score
        if any(s.action in DESTRUCTIVE_ACTIONS for s in plan.steps):
            risk = max(risk, DESTRUCTIVE_RISK_FLOOR)  # don't trust model optimism

        proposal = RemediationProposal(
            incident_id=triage.incident_id,  # injected, never model-supplied
            hypothesis_cause=top.cause,
            steps=[_to_step(i + 1, s) for i, s in enumerate(plan.steps)],
            rollback_plan=[_to_step(i + 1, s) for i, s in enumerate(plan.rollback_plan)],
            risk_score=risk,
            blast_radius=plan.blast_radius,
            requires_approval=True,  # only the HITL gate may clear this
        )
        log.info(
            "remediation_planned", step=STEP, incident_id=triage.incident_id,
            steps=len(proposal.steps), rollback_steps=len(proposal.rollback_plan),
            risk_score=proposal.risk_score, blast_radius=proposal.blast_radius,
            destructive=bool(destructive_steps(proposal)), tokens_spent=tokens_spent,
        )
        return proposal
