"""HITL gate: the safety valve between a proposal and the executor.

Routing: REMEDIATION confidence >= threshold AND risk <= threshold ->
auto-approve; anything else escalates to a human. The gate evaluates the
remediation's appropriateness — its risk and the planner's confidence in the
FIX (proposal.remediation_confidence) — not the diagnosis confidence: a
correct root cause with a shaky/uncertain remediation must still reach a human.
(The real-model benchmark found diagnosis confidence sat at 0.85–0.98 on every
fault, so a diagnosis-confidence gate never fired and was effectively
destructiveness-only.) Destructive actions ALWAYS escalate, no matter how
confident the model is — destructiveness is classified server-side
(remediation.DESTRUCTIVE_ACTIONS), never taken from the model. A planner that
DECLINED (proposal.escalate) always escalates too: declining is routed to a
human, never auto-approved.

The human is behind the Approver protocol so benchmarks auto-answer it
(StaticApprover) and the API can put a real user behind the same interface.
"""

from __future__ import annotations

from typing import Literal, Protocol

import structlog
from pydantic import BaseModel, Field

from autopilot.models import RemediationProposal, RootCauseHypothesis
from autopilot.pipeline.remediation import destructive_steps
from autopilot.tracing import span

log = structlog.get_logger("autopilot.pipeline.hitl")

DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_RISK_THRESHOLD = 0.4


class ApprovalRequest(BaseModel):
    incident_id: str
    hypothesis: RootCauseHypothesis
    proposal: RemediationProposal
    escalation_reasons: list[str]


class HumanDecision(BaseModel):
    action: Literal["approve", "edit", "reject"]
    edited_proposal: RemediationProposal | None = None  # required when action="edit"
    note: str = ""


class Approver(Protocol):
    """Anything that can answer an ApprovalRequest: a benchmark policy now,
    a real user behind the API later."""

    def decide(self, request: ApprovalRequest) -> HumanDecision: ...


class StaticApprover:
    """Scripted approver for benchmarks/tests."""

    def __init__(self, action: Literal["approve", "edit", "reject"] = "approve",
                 edited_proposal: RemediationProposal | None = None, note: str = ""):
        self._decision = HumanDecision(action=action, edited_proposal=edited_proposal,
                                       note=note)
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> HumanDecision:
        self.requests.append(request)
        return self._decision


class GateOutcome(BaseModel):
    approved: bool
    route: Literal["auto", "human"]
    proposal: RemediationProposal  # the proposal to execute (possibly edited)
    escalation_reasons: list[str] = Field(default_factory=list)
    human_action: Literal["approve", "edit", "reject"] | None = None
    note: str = ""


def hitl_gate(
    hypothesis: RootCauseHypothesis,
    proposal: RemediationProposal,
    approver: Approver,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    risk_threshold: float = DEFAULT_RISK_THRESHOLD,
) -> GateOutcome:
    with span("hitl", incident_id=proposal.incident_id):
        reasons: list[str] = []
        if proposal.escalate:
            reasons.append(
                "planner declined to act (no safe in-vocabulary remediation)"
            )
        for desc in destructive_steps(proposal):
            reasons.append(f"destructive action always escalates ({desc})")
        if proposal.remediation_confidence < confidence_threshold:
            reasons.append(
                f"remediation confidence {proposal.remediation_confidence:.2f} "
                f"< {confidence_threshold:.2f}"
            )
        if proposal.risk_score > risk_threshold:
            reasons.append(
                f"risk {proposal.risk_score:.2f} > {risk_threshold:.2f}"
            )

        if not reasons:
            outcome = GateOutcome(
                approved=True, route="auto",
                proposal=proposal.model_copy(update={"requires_approval": False}),
            )
            log.info("hitl_auto_approved", step="hitl",
                     incident_id=proposal.incident_id,
                     remediation_confidence=proposal.remediation_confidence,
                     diagnosis_confidence=hypothesis.confidence,
                     risk=proposal.risk_score)
            return outcome

        request = ApprovalRequest(
            incident_id=proposal.incident_id, hypothesis=hypothesis,
            proposal=proposal, escalation_reasons=reasons,
        )
        log.info("hitl_escalated", step="hitl", incident_id=proposal.incident_id,
                 reasons=reasons)
        decision = approver.decide(request)

        if decision.action == "reject":
            outcome = GateOutcome(approved=False, route="human", proposal=proposal,
                                  escalation_reasons=reasons,
                                  human_action="reject", note=decision.note)
        elif decision.action == "edit":
            if decision.edited_proposal is None:
                raise ValueError("HumanDecision(action='edit') requires edited_proposal")
            approved = decision.edited_proposal.model_copy(
                update={"requires_approval": False})  # human approval clears the flag
            outcome = GateOutcome(approved=True, route="human", proposal=approved,
                                  escalation_reasons=reasons,
                                  human_action="edit", note=decision.note)
        else:
            approved = proposal.model_copy(update={"requires_approval": False})
            outcome = GateOutcome(approved=True, route="human", proposal=approved,
                                  escalation_reasons=reasons,
                                  human_action="approve", note=decision.note)
        log.info("hitl_human_decided", step="hitl", incident_id=proposal.incident_id,
                 action=decision.action, approved=outcome.approved)
        return outcome
