"""API response/request schemas. The dashboard speaks these types; FastAPI
validates every endpoint against them (the tests assert the schemas hold).

These are presentation views over the domain models — deliberately flat and
JSON-friendly. They never carry ground truth (FaultSpec semantics): the demo
shows what the AGENT observed and decided, exactly like production would."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal[
    "running",            # pipeline in flight
    "awaiting_approval",  # paused at the HITL gate, needs a human decision
    "resolved",           # acted and verified healthy
    "rolled_back",        # acted, verification failed, auto-rolled back (contained)
    "rejected",           # escalated and the operator rejected the plan
    "failed",             # a stage errored (e.g. schema failure)
]

StageName = Literal[
    "ingest", "triage", "remediation", "gate",
    "execution", "verification", "rollback", "outcome",
]


class ScenarioInfo(BaseModel):
    """A fault scenario the operator can inject to start a demo run."""

    fault_id: str
    title: str  # the alert-derived incident title (what the agent will see)


class EvidenceView(BaseModel):
    kind: str
    pointer: str
    excerpt: str = ""


class HypothesisView(BaseModel):
    cause: str
    confidence: float
    evidence: list[EvidenceView] = Field(default_factory=list)
    reasoning_summary: str = ""


class StepView(BaseModel):
    order: int
    action: str
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    expected_effect: str = ""


class ProposalView(BaseModel):
    id: str
    steps: list[StepView] = Field(default_factory=list)
    rollback_plan: list[StepView] = Field(default_factory=list)
    risk_score: float
    remediation_confidence: float
    blast_radius: str
    escalate: bool


class TraceEventView(BaseModel):
    """One entry in the live reasoning trace — built to be readable in seconds:
    a stage, a one-line takeaway, an optional confidence, and what it cost."""

    stage: StageName
    title: str
    status: Literal["ok", "info", "warn", "error", "pending"]
    detail: str = ""
    confidence: float | None = None
    tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalView(BaseModel):
    """The pending HITL decision surfaced to the operator."""

    incident_id: str
    hypothesis_cause: str
    hypothesis_confidence: float
    reasons: list[str]
    proposal: ProposalView


class RunSummary(BaseModel):
    id: str
    fault_id: str
    scenario_title: str
    status: RunStatus
    resolved: bool = False
    rolled_back: bool = False
    escalated: bool = False
    total_tokens: int = 0
    est_cost_usd: float = 0.0
    started_at: str
    top_cause: str | None = None
    top_confidence: float | None = None


class RunDetail(RunSummary):
    events: list[TraceEventView] = Field(default_factory=list)
    approval: ApprovalView | None = None


class StepInput(BaseModel):
    action: Literal["restart_service", "scale_service", "apply_config", "rollback"]
    target: Literal["app", "worker", "downstream", "db", "queue"] = "app"
    params: dict[str, Any] = Field(default_factory=dict)
    expected_effect: str = ""


class DecisionRequest(BaseModel):
    action: Literal["approve", "edit", "reject"]
    note: str = ""
    # Only for action="edit": the operator's replacement steps. If omitted on an
    # edit, the originally-proposed steps are kept (still routed as a human edit).
    steps: list[StepInput] | None = None


class CreateRunRequest(BaseModel):
    fault_id: str
