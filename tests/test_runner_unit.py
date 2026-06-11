"""Runner skeleton test with a fake controller — no docker, no network."""

import json
from datetime import UTC, datetime, timedelta

from autopilot.harness.faults import FAULTS
from autopilot.harness.runner import ScenarioRunner
from autopilot.models import Incident
from autopilot.sandbox.controller import ProbeSnapshot

T0 = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)

SAMPLE_LOGS = "\n".join(
    [
        "autopilot-sbx-app  | 2026-06-11T12:00:01.000000000Z "
        '{"ts": "2026-06-11T12:00:01+00:00", "service": "app", '
        '"event": "healthz_component_failed", "component": "db", '
        '"error": "FATAL: remaining connection slots are reserved"}',
        "autopilot-sbx-db   | 2026-06-11T12:00:01.500000000Z "
        "FATAL:  remaining connection slots are reserved for roles with the SUPERUSER attribute",
    ]
)


class FakeController:
    def __init__(self):
        self.calls: list[str] = []
        self._probes = 0

    def reset(self):
        self.calls.append("reset")

    def exec(self, service, *cmd, detach=False):
        self.calls.append(f"exec:{service}")
        return ""

    def psql(self, sql, user="autopilot"):
        self.calls.append("psql")
        return ""

    def pause(self, service):
        self.calls.append(f"pause:{service}")

    def unpause(self, service):
        self.calls.append(f"unpause:{service}")

    def restart(self, service):
        self.calls.append(f"restart:{service}")

    def write_app_config(self, config):
        self.calls.append("write_app_config")

    def default_app_config(self):
        return {"feature_mode": "standard"}

    def logs(self, since=None):
        self.calls.append("logs")
        return SAMPLE_LOGS

    def probe(self):
        self.calls.append("probe")
        self._probes += 1
        return ProbeSnapshot(
            captured_at=T0 + timedelta(seconds=self._probes),
            healthz_status=503,
            healthz_body={"status": "degraded", "components": {"db": {"ok": False}}},
            work_status=500,
            work_body={"error": "db error: connection failed"},
            metrics={"requests_total": self._probes, "errors_total": self._probes,
                     "queue_depth": 0, "jobs_processed": 0},
        )


def test_runner_produces_valid_incident_in_correct_order():
    ctrl = FakeController()
    runner = ScenarioRunner(ctrl=ctrl, probe_count=3, probe_interval_s=0)
    incident = runner.run("db_pool_exhaustion", incident_id="inc-test")

    assert isinstance(incident, Incident)
    assert incident.id == "inc-test"
    assert incident.environment == "sandbox"

    # ordering: reset -> inject (db execs) -> probes -> logs
    assert ctrl.calls[0] == "reset"
    first_exec = ctrl.calls.index("exec:db")
    assert first_exec > 0 and ctrl.calls.index("logs") > first_exec

    # telemetry is populated and the alert reflects observed degradation
    t = incident.telemetry
    assert t.alert.name == "sandbox.app.health_degraded"
    assert t.alert.severity.value == "high"  # every probe failed
    assert len(t.logs) == 2 and t.logs[0].service == "app"
    assert any(m.name == "queue_depth" for m in t.metrics)


def test_incident_does_not_leak_ground_truth():
    runner = ScenarioRunner(ctrl=FakeController(), probe_count=3, probe_interval_s=0)
    incident = runner.run("db_pool_exhaustion")
    dump = json.dumps(incident.model_dump(mode="json")).lower()

    spec = FAULTS["db_pool_exhaustion"].spec
    assert spec.id not in dump
    assert spec.canonical_root_cause.lower() not in dump
    assert spec.canonical_remediation.lower() not in dump
    assert "fault" not in dump


def test_cleanup_reverts_active_fault():
    ctrl = FakeController()
    runner = ScenarioRunner(ctrl=ctrl, probe_count=2, probe_interval_s=0)
    runner.run("downstream_timeout")
    assert "pause:downstream" in ctrl.calls
    runner.cleanup()
    assert "unpause:downstream" in ctrl.calls
