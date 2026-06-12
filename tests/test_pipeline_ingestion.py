"""Ingestion stage tests: every synthetic scenario capture normalizes into a
valid typed Incident, degraded/empty inputs are handled gracefully, and no
ground-truth fault metadata leaks into what the agent sees."""

from __future__ import annotations

import pytest

from autopilot.harness.faults import FAULTS
from autopilot.harness.synthetic import FAULT_IDS, scenario_capture
from autopilot.models import Incident, Severity
from autopilot.pipeline.ingest import ingest


@pytest.mark.parametrize("fault_id", FAULT_IDS)
def test_each_scenario_yields_valid_incident(fault_id):
    log_text, snapshots = scenario_capture(fault_id)
    incident = ingest(log_text, snapshots)

    assert Incident.model_validate(incident.model_dump())
    assert incident.environment == "sandbox"
    assert incident.telemetry.logs, "logs should be parsed"
    assert incident.telemetry.alert.name.startswith("sandbox.app.")
    # timestamps parsed from the compose prefix
    assert all(r.timestamp is not None for r in incident.telemetry.logs)


@pytest.mark.parametrize("fault_id", FAULT_IDS)
def test_no_ground_truth_leaks_into_incident(fault_id):
    """The Incident must carry observable symptoms only — never FaultSpec text.
    (Fault *ids* may coincide with app-emitted error strings, e.g.
    'downstream_timeout'; the leak vector is the spec's answer fields.)"""
    log_text, snapshots = scenario_capture(fault_id)
    blob = ingest(log_text, snapshots).model_dump_json().lower()
    spec = FAULTS[fault_id].spec
    for forbidden in (spec.canonical_root_cause.lower(),
                      spec.canonical_remediation.lower(),
                      spec.trigger.lower(),
                      "canonical_root_cause", "faultspec", "ground truth"):
        assert forbidden not in blob, forbidden[:60]


def test_alert_severity_tracks_observed_failures():
    _, db_down = scenario_capture("db_pool_exhaustion")
    incident = ingest("", db_down)
    assert incident.telemetry.alert.name == "sandbox.app.health_degraded"
    assert incident.telemetry.alert.severity == Severity.high  # every probe failing

    _, silent = scenario_capture("queue_consumer_stall")
    incident = ingest("", silent)
    assert incident.telemetry.alert.name == "sandbox.app.anomaly_suspected"
    assert incident.telemetry.alert.severity == Severity.low


def test_ingest_handles_empty_and_garbage_input():
    incident = ingest("", [])
    assert incident.telemetry.logs == []
    assert incident.telemetry.metrics == []
    assert incident.telemetry.alert.severity == Severity.low

    garbage = "no pipe here\n\x00\xff binary-ish\n| | | |\nrandom | not-a-timestamp text"
    incident = ingest(garbage, [])
    assert Incident.model_validate(incident.model_dump())  # skips, never crashes


def test_metrics_extracted_from_snapshots():
    _, snaps = scenario_capture("queue_consumer_stall")
    incident = ingest("", snaps)
    depths = [p.value for p in incident.telemetry.metrics if p.name == "queue_depth"]
    assert depths == [2, 5, 8, 11]  # growing backlog visible to the agent
