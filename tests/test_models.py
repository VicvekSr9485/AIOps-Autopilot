import pytest
from pydantic import ValidationError

from autopilot.models import (
    AlertEvent,
    EvidenceRef,
    ExecutionResult,
    Incident,
    LogRecord,
    MetricPoint,
    RemediationProposal,
    RemediationStep,
    RootCauseHypothesis,
    Severity,
    StepOutcome,
    Telemetry,
    VerificationCheck,
    VerificationResult,
    utcnow,
)


def make_incident() -> Incident:
    return Incident(
        title="[high] sandbox.app.health_degraded",
        telemetry=Telemetry(
            alert=AlertEvent(name="sandbox.app.health_degraded", severity=Severity.high,
                             source="sandbox-probe"),
            logs=[LogRecord(service="app", message="healthz_component_failed")],
            metrics=[MetricPoint(name="errors_total", value=3.0, captured_at=utcnow())],
        ),
    )


def test_incident_roundtrip():
    inc = make_incident()
    assert inc.id.startswith("inc-")
    assert Incident.model_validate(inc.model_dump()) == inc


def test_hypothesis_confidence_bounds():
    ev = EvidenceRef(kind="log", pointer="log:app:1", excerpt="connection refused")
    RootCauseHypothesis(incident_id="inc-x", cause="db down", confidence=0.9, evidence=[ev])
    with pytest.raises(ValidationError):
        RootCauseHypothesis(incident_id="inc-x", cause="db down", confidence=1.5)


def test_remediation_risk_and_blast_radius():
    step = RemediationStep(order=1, action="restart worker", target="worker")
    prop = RemediationProposal(
        incident_id="inc-x", hypothesis_cause="worker stalled", steps=[step],
        rollback_plan=[step], risk_score=0.2, blast_radius="single_service",
    )
    assert prop.requires_approval is True  # HITL gate is default-closed
    with pytest.raises(ValidationError):
        RemediationProposal(
            incident_id="inc-x", hypothesis_cause="x", steps=[step],
            risk_score=0.5, blast_radius="the whole internet",
        )


def test_execution_and_verification():
    started = utcnow()
    res = ExecutionResult(
        proposal_id="rem-1", incident_id="inc-x", success=True,
        step_outcomes=[StepOutcome(step_order=1, success=True, output="ok")],
        started_at=started, finished_at=utcnow(),
    )
    assert res.success
    ver = VerificationResult(
        incident_id="inc-x", resolved=True,
        checks=[VerificationCheck(name="healthz", passed=True)],
    )
    assert ver.resolved
