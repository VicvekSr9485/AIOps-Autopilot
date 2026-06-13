"""Remediation planner: triage's evidence handoff (summarized telemetry + top
hypothesis + runbooks) -> schema-validated RemediationProposal (steps mapped to
Infra/Ops actions, rollback plan, risk/blast-radius).

Stage-scoping is STRUCTURAL here: this function takes no servers argument at
all (exposure maps planner -> no tools); runbook context arrives on the
TriageResult instead of being re-retrieved (cost rule: never re-ask).

Input contract (the fix for the first benchmark's planner losses): the prompt
leads with the INCIDENT SYMPTOMS the triage stage actually gathered — the
planner must never depend on the hypothesis prose alone, and retrieved runbook
text is explicitly labeled as reference material that may be irrelevant, so it
cannot outweigh primary evidence.

Uses the `planning` role (qwen3.7-max). The real-model benchmark localized the
pipeline's remediation losses to a too-cheap planner; planning now runs on the
max tier (see config.MODEL_BY_ROLE / Key Decisions). Server-side discipline
mirrors the rest of the pipeline: incident_id and hypothesis_cause are injected,
action/target vocabularies are closed enums, config VALUES are grounded to the
app's known-good set (a hallucinated feature_mode can never reach the executor),
a risk floor is applied when destructive steps are present (the model's optimism
is not trusted), and requires_approval starts True — only the HITL gate may
clear it. The planner may also DECLINE (escalate=True) when no in-vocabulary fix
exists or its remediation confidence is low — that routes to a human, and it is
never a free safe pass (escalating a fixable fault scores as a miss downstream).
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

# The app's only valid feature_mode (sandbox/app/config.default.json + the
# app's own validation: it rejects anything != "standard"). This is the app's
# documented config schema — operational knowledge, NOT fault ground truth.
# Grounding apply_config against it kills the "hallucinated value" failure
# class (the real run's planner proposed feature_mode='stable'): any other
# value collapses to a `rollback` (restore the canonical config), which is
# what the operator actually wants and needs no guessed value.
VALID_FEATURE_MODES = frozenset({"standard"})


class _LLMPlanStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: ActionName
    target: Literal["app", "worker", "downstream", "db", "queue"] = "app"
    params: dict[str, Any] = Field(default_factory=dict)
    expected_effect: str = ""


class _LLMPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    steps: list[_LLMPlanStep] = Field(default_factory=list, max_length=3)
    rollback_plan: list[_LLMPlanStep] = Field(default_factory=list, max_length=3)
    risk_score: float = Field(ge=0.0, le=1.0)
    blast_radius: Literal["single_service", "multiple_services", "stack_wide"]
    # The planner's confidence in this remediation specifically. The gate gates
    # on this — a confident diagnosis with a shaky fix still reaches a human.
    remediation_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # Decline to act: no fix is expressible in the action vocabulary above (e.g.
    # the fix is "rotate a credential" / "free DB slots by hand"), or confidence
    # is too low. When true, steps may be empty and the gate escalates.
    escalate: bool = False


_SYSTEM_PROMPT = (
    "You are an SRE remediation planner for a sandboxed compose stack. Given a "
    "root-cause hypothesis and runbook guidance, plan the smallest remediation.\n"
    "Available actions (the ONLY ones that exist):\n"
    "- restart_service: params {} — restart ONE specific service (you choose the "
    "target). Restarting a service drops its in-flight state: restart `db` to "
    "clear stuck/idle DB sessions holding connection slots; restart `worker` to "
    "revive a stalled queue consumer; restart `downstream` to recover a hung "
    "dependency; restart `app` only for app-process issues. Pick the target that "
    "OWNS the failing component — do not default to `app`.\n"
    "- scale_service: params {\"replicas\": 0..3} — scale a service (0 stops it, "
    "1 brings a scaled-to-zero service back). A restart is a NO-OP at 0 replicas.\n"
    "- apply_config: params = partial app config; the ONLY valid feature_mode is "
    "\"standard\" (the app rejects any other value). Prefer `rollback` over "
    "apply_config to undo a bad config rollout — do NOT invent a config value.\n"
    "- rollback: params {} — restore the canonical (known-good) app config and "
    "restart app; targets app. Use this to revert ANY bad config rollout.\n"
    "Services: app, worker, downstream, db, queue.\n"
    "If NO action above can restore health (e.g. the fix is to rotate a "
    "credential, free DB slots by hand, or anything outside this vocabulary), or "
    "you are not confident enough to act, set escalate=true with empty steps and "
    "a low remediation_confidence — a human will handle it. Do NOT fabricate a "
    "plausible-looking action just to fill the field.\n"
    "Respond with STRICT JSON only — no prose, no markdown — matching:\n"
    '{"steps": [{"action": str, "target": str, "params": object, '
    '"expected_effect": str}], "rollback_plan": [same shape], '
    '"risk_score": float 0..1, "blast_radius": "single_service"|'
    '"multiple_services"|"stack_wide", "remediation_confidence": float 0..1, '
    '"escalate": bool}\n'
    "1-3 steps (or 0 with escalate=true), minimal blast radius. When you act, "
    "ALWAYS include a rollback_plan that restores the pre-remediation state."
)


def _ground_step(s: _LLMPlanStep) -> _LLMPlanStep:
    """Ground model-supplied config VALUES against the app's known-good schema.
    An apply_config naming a feature_mode the app would reject (anything but
    'standard') is collapsed to a `rollback` — the planner cannot push a
    hallucinated value (e.g. 'stable') through to the executor."""
    if s.action != "apply_config":
        return s
    mode = s.params.get("feature_mode")
    if mode is not None and mode not in VALID_FEATURE_MODES:
        log.info("planner_grounded_config", step=STEP, rejected_feature_mode=mode)
        return _LLMPlanStep(action="rollback", target="app", params={},
                            expected_effect="restore canonical config "
                            f"(grounded: '{mode}' is not a valid feature_mode)")
    return s


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
            f"{e.kind}:{e.pointer} ({e.excerpt[:300]})" for e in top.evidence
        ) or "none cited"
        runbooks = "\n".join(f"- {note}" for note in triage.consulted_runbooks) or "- none"
        symptoms = triage.telemetry_summary or "(no telemetry summary forwarded)"
        # Primary evidence first (symptoms, then the hypothesis built on them);
        # retrieved runbooks last and explicitly marked as fallible reference.
        user = (
            f"INCIDENT SYMPTOMS (summarized telemetry gathered during triage):\n"
            f"{symptoms}\n\n"
            f"ROOT-CAUSE HYPOTHESIS (confidence {top.confidence:.2f}): {top.cause}\n"
            f"Reasoning: {top.reasoning_summary}\nEvidence: {evidence}\n\n"
            f"RUNBOOK GUIDANCE (retrieved reference material — relevance is "
            f"approximate; trust the symptoms above when they disagree):\n{runbooks}"
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        try:
            plan, tokens_spent = complete_structured(
                client, "planning", messages, _LLMPlan,
                step=STEP, max_attempts=max_attempts, token_cap=token_cap,
            )
        except StructuredOutputError as e:
            raise PlanningError(str(e)) from None

        grounded = [_ground_step(s) for s in plan.steps]
        # A planner that declined, or returned no actionable step, is an
        # escalation — never an empty proposal that the executor would no-op.
        escalate = plan.escalate or not grounded

        risk = plan.risk_score
        if any(s.action in DESTRUCTIVE_ACTIONS for s in grounded):
            risk = max(risk, DESTRUCTIVE_RISK_FLOOR)  # don't trust model optimism

        proposal = RemediationProposal(
            incident_id=triage.incident_id,  # injected, never model-supplied
            hypothesis_cause=top.cause,
            steps=[] if escalate else [_to_step(i + 1, s) for i, s in enumerate(grounded)],
            rollback_plan=[_to_step(i + 1, s) for i, s in enumerate(plan.rollback_plan)],
            risk_score=risk,
            blast_radius=plan.blast_radius,
            remediation_confidence=plan.remediation_confidence,
            escalate=escalate,
            requires_approval=True,  # only the HITL gate may clear this
        )
        log.info(
            "remediation_planned", step=STEP, incident_id=triage.incident_id,
            steps=len(proposal.steps), rollback_steps=len(proposal.rollback_plan),
            risk_score=proposal.risk_score, blast_radius=proposal.blast_radius,
            remediation_confidence=proposal.remediation_confidence,
            escalate=proposal.escalate,
            destructive=bool(destructive_steps(proposal)), tokens_spent=tokens_spent,
        )
        return proposal
