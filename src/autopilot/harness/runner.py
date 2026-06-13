"""Scenario runner skeleton: fault id -> reset sandbox -> inject -> capture telemetry
-> normalized Incident. No scoring, no agent reasoning here.

The fault stays INJECTED when run() returns — resolving it is the agent's job in
later milestones. Call cleanup() (or ctrl.reset()) to restore health manually.
"""

from __future__ import annotations

import time

import structlog

from autopilot.harness.faults import Fault, get_fault
from autopilot.ingestion.normalize import build_incident
from autopilot.models import Incident
from autopilot.sandbox.controller import ProbeSnapshot, SandboxController

log = structlog.get_logger("autopilot.harness.runner")


class ScenarioRunner:
    def __init__(
        self,
        ctrl: SandboxController | None = None,
        probe_count: int = 6,
        probe_interval_s: float = 1.0,
    ):
        self.ctrl = ctrl or SandboxController()
        self.probe_count = probe_count
        self.probe_interval_s = probe_interval_s
        self._active_fault: Fault | None = None

    def run(self, fault_id: str, incident_id: str | None = None) -> Incident:
        fault = get_fault(fault_id)
        log.info("scenario_start", step="harness", fault_id=fault_id)

        self.ctrl.reset()
        capture_start = self.ctrl.probe().captured_at  # also confirms reachability

        fault.inject(self.ctrl)
        self._active_fault = fault
        log.info("fault_injected", step="harness", fault_id=fault_id)

        snapshots: list[ProbeSnapshot] = []
        for i in range(self.probe_count):
            snapshots.append(self.ctrl.probe())
            if i < self.probe_count - 1:
                time.sleep(self.probe_interval_s)

        # redact_capture models degraded alert-time observability (default
        # identity); the controller's live logs() stay untouched, so detail it
        # strips remains reachable via a telemetry query during triage.
        log_text = fault.redact_capture(self.ctrl.logs(since=capture_start))
        incident = build_incident(log_text, snapshots, incident_id=incident_id)
        log.info(
            "scenario_captured", step="harness", fault_id=fault_id, incident_id=incident.id
        )
        return incident

    def cleanup(self) -> None:
        """Revert the active fault (if any) so the stack returns to healthy."""
        if self._active_fault is not None:
            self._active_fault.revert(self.ctrl)
            log.info("fault_reverted", step="harness", fault_id=self._active_fault.spec.id)
            self._active_fault = None
