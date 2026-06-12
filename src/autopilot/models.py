"""Core domain models. Every pipeline stage boundary speaks these types — no bare dicts.

Flow: Telemetry -> Incident -> RootCauseHypothesis -> RemediationProposal
      -> ExecutionResult -> VerificationResult
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


# --------------------------------------------------------------------------- telemetry


class AlertEvent(BaseModel):
    name: str
    severity: Severity
    source: str
    fired_at: datetime = Field(default_factory=utcnow)
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class LogRecord(BaseModel):
    service: str
    message: str
    timestamp: datetime | None = None
    raw: str = ""


class MetricPoint(BaseModel):
    name: str
    value: float
    captured_at: datetime
    unit: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class Telemetry(BaseModel):
    alert: AlertEvent
    logs: list[LogRecord] = Field(default_factory=list)
    metrics: list[MetricPoint] = Field(default_factory=list)


class Incident(BaseModel):
    """The ambiguous bundle the agent starts from. MUST NOT contain ground truth."""

    id: str = Field(default_factory=lambda: new_id("inc"))
    title: str
    created_at: datetime = Field(default_factory=utcnow)
    environment: str = "sandbox"
    telemetry: Telemetry


# --------------------------------------------------------------------------- reasoning


class EvidenceRef(BaseModel):
    """Pointer into incident telemetry backing a claim."""

    kind: Literal["alert", "log", "metric"]
    pointer: str  # e.g. "log:app:line-42" or "metric:queue_depth"
    excerpt: str = ""


class RootCauseHypothesis(BaseModel):
    incident_id: str
    cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    reasoning_summary: str = ""


class TriageResult(BaseModel):
    """Output of the triage/root-cause stage: hypotheses ranked by confidence
    (descending — enforced by the stage, not trusted from the model)."""

    incident_id: str
    hypotheses: list[RootCauseHypothesis] = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utcnow)

    @property
    def top(self) -> RootCauseHypothesis:
        return self.hypotheses[0]


# ------------------------------------------------------------------------- remediation


class RemediationStep(BaseModel):
    order: int = Field(ge=1)
    action: str
    target: str  # sandbox service/component the step touches
    command: str | None = None
    expected_effect: str = ""


BlastRadius = Literal["single_service", "multiple_services", "stack_wide"]


class RemediationProposal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rem"))
    incident_id: str
    hypothesis_cause: str
    steps: list[RemediationStep]
    rollback_plan: list[RemediationStep] = Field(default_factory=list)
    risk_score: float = Field(ge=0.0, le=1.0)
    blast_radius: BlastRadius
    requires_approval: bool = True  # HITL gate default-closed


# --------------------------------------------------------------------------- execution


class StepOutcome(BaseModel):
    step_order: int
    success: bool
    output: str = ""


class ExecutionResult(BaseModel):
    proposal_id: str
    incident_id: str
    success: bool
    step_outcomes: list[StepOutcome] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
    error: str | None = None


class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class VerificationResult(BaseModel):
    incident_id: str
    resolved: bool
    checks: list[VerificationCheck] = Field(default_factory=list)
    verified_at: datetime = Field(default_factory=utcnow)
