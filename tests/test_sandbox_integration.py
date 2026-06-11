"""Integration tests against the real docker-compose sandbox.

Each fault must: inject -> show its expected symptom signature -> revert -> stack
healthy again. Skipped automatically when docker isn't available.
"""

import shutil
import subprocess
import time

import pytest

from autopilot.harness.faults import FAULTS
from autopilot.harness.runner import ScenarioRunner
from autopilot.sandbox.controller import SandboxController

pytestmark = pytest.mark.sandbox


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(
        ["docker", "info"], capture_output=True, timeout=15
    ).returncode == 0


if not _docker_available():
    pytest.skip("docker not available", allow_module_level=True)


def wait_until(predicate, timeout_s: float = 45.0, interval_s: float = 1.5):
    deadline = time.monotonic() + timeout_s
    state = []
    while time.monotonic() < deadline:
        state.append(predicate())
        if state[-1]:
            return True
        time.sleep(interval_s)
    return False


@pytest.fixture(scope="module")
def ctrl():
    controller = SandboxController()
    controller.reset()
    yield controller
    controller.down()


@pytest.mark.parametrize("fault_id", sorted(FAULTS))
def test_fault_injects_shows_symptoms_and_reverts(ctrl, fault_id):
    fault = FAULTS[fault_id]
    assert wait_until(lambda: ctrl.probe().healthy), "stack not healthy before injection"

    capture_start = ctrl.probe().captured_at
    snapshots = []
    fault.inject(ctrl)
    try:
        assert wait_until(
            lambda: (snapshots.append(ctrl.probe()) or fault.symptoms_present(snapshots))
        ), f"{fault_id}: expected symptom signature never appeared"

        if fault.log_signature:
            assert fault.log_signature in ctrl.logs(since=capture_start), (
                f"{fault_id}: log signature {fault.log_signature!r} not found"
            )
    finally:
        fault.revert(ctrl)

    assert wait_until(lambda: ctrl.probe().healthy), (
        f"{fault_id}: stack did not return to healthy after revert"
    )


def test_runner_yields_valid_incident_end_to_end(ctrl):
    runner = ScenarioRunner(ctrl=ctrl, probe_count=4, probe_interval_s=1.0)
    try:
        incident = runner.run("bad_config_rollout")
    finally:
        runner.cleanup()

    assert incident.telemetry.logs, "incident carries no logs"
    assert incident.telemetry.metrics, "incident carries no metrics"
    assert incident.telemetry.alert.name in (
        "sandbox.app.work_errors",
        "sandbox.app.health_degraded",
    )
    assert wait_until(lambda: ctrl.probe().healthy), "stack unhealthy after cleanup"
