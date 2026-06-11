"""Fault library: 5 injectable faults with ground-truth metadata.

Ground truth (FaultSpec) is for the benchmark/scoring side ONLY — it must never
leak into the Incident the agent sees. Each fault injects and reverses cleanly
against the sandbox stack; `symptoms_present` is the machine-checkable signature
used by tests and (later) the benchmark.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

import structlog
from pydantic import BaseModel

from autopilot.models import Severity
from autopilot.sandbox.controller import ProbeSnapshot, SandboxController

log = structlog.get_logger("autopilot.harness.faults")


class FaultSpec(BaseModel):
    """Ground-truth metadata for one injectable fault."""

    id: str
    name: str
    trigger: str  # how the harness injects it
    expected_symptoms: list[str]
    canonical_root_cause: str
    canonical_remediation: str
    severity: Severity


class Fault(ABC):
    spec: FaultSpec
    log_signature: str | None = None  # substring expected in stack logs while injected

    @abstractmethod
    def inject(self, ctrl: SandboxController) -> None: ...

    @abstractmethod
    def revert(self, ctrl: SandboxController) -> None: ...

    @abstractmethod
    def symptoms_present(self, snapshots: Sequence[ProbeSnapshot]) -> bool:
        """True if the expected symptom signature is visible in the observations."""


def _component_down(snap: ProbeSnapshot, component: str) -> bool:
    if snap.healthz_status != 503 or not snap.healthz_body:
        return False
    return not snap.healthz_body.get("components", {}).get(component, {}).get("ok", True)


class DbConnectionExhaustion(Fault):
    spec = FaultSpec(
        id="db_pool_exhaustion",
        name="DB connection pool exhaustion",
        trigger="open 8 idle pg_sleep sessions as role 'app', saturating max_connections=10 "
        "(3 slots are superuser-reserved)",
        expected_symptoms=[
            "healthz reports db component down (503)",
            "/work returns 500 with a db connection error",
            "logs mention remaining connection slots / too many clients",
        ],
        canonical_root_cause="Database connection slots exhausted by long-running idle "
        "sessions holding all non-reserved connections",
        canonical_remediation="Terminate the idle long-running sessions "
        "(pg_terminate_backend) to free connection slots",
        severity=Severity.high,
    )
    log_signature = "remaining connection slots"

    def inject(self, ctrl: SandboxController) -> None:
        for _ in range(8):
            ctrl.exec(
                "db", "psql", "-U", "app", "-d", "autopilot",
                "-c", "SELECT pg_sleep(600);", detach=True,
            )

    def revert(self, ctrl: SandboxController) -> None:
        ctrl.psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = 'app' AND query LIKE '%pg_sleep%';"
        )

    def symptoms_present(self, snapshots: Sequence[ProbeSnapshot]) -> bool:
        return any(_component_down(s, "db") for s in snapshots)


class BadConfigRollout(Fault):
    spec = FaultSpec(
        id="bad_config_rollout",
        name="Bad config rollout",
        trigger="roll out app config with feature_mode='turbo_v2' (invalid) and restart app",
        expected_symptoms=[
            "/work returns 500 with 'invalid feature_mode'",
            "healthz stays ok (dependencies are fine) — errors are config-driven",
        ],
        canonical_root_cause="A configuration rollout set feature_mode to an unsupported "
        "value, making the app reject all work requests",
        canonical_remediation="Roll back app config to feature_mode='standard' and restart "
        "the app service",
        severity=Severity.high,
    )
    log_signature = "invalid_feature_mode"

    def inject(self, ctrl: SandboxController) -> None:
        config = ctrl.default_app_config()
        config["feature_mode"] = "turbo_v2"
        ctrl.write_app_config(config)
        ctrl.restart("app")

    def revert(self, ctrl: SandboxController) -> None:
        ctrl.write_app_config(ctrl.default_app_config())
        ctrl.restart("app")

    def symptoms_present(self, snapshots: Sequence[ProbeSnapshot]) -> bool:
        return any(
            s.work_status == 500 and "feature_mode" in str(s.work_body) for s in snapshots
        )


class DownstreamDependencyTimeout(Fault):
    spec = FaultSpec(
        id="downstream_timeout",
        name="Downstream dependency timeout",
        trigger="pause the downstream container so app calls hang until timeout",
        expected_symptoms=[
            "/work returns 504 downstream_timeout",
            "request latency spikes to the configured timeout",
        ],
        canonical_root_cause="The downstream dependency stopped responding; app requests "
        "to it time out",
        canonical_remediation="Restore the downstream service (unpause/restart the "
        "downstream container)",
        severity=Severity.medium,
    )
    log_signature = "downstream_timeout"

    def inject(self, ctrl: SandboxController) -> None:
        ctrl.pause("downstream")

    def revert(self, ctrl: SandboxController) -> None:
        ctrl.unpause("downstream")

    def symptoms_present(self, snapshots: Sequence[ProbeSnapshot]) -> bool:
        return any(s.work_status == 504 for s in snapshots)


class QueueConsumerStall(Fault):
    spec = FaultSpec(
        id="queue_consumer_stall",
        name="Queue consumer stall",
        trigger="pause the worker container so jobs are produced but never consumed",
        expected_symptoms=[
            "queue_depth grows monotonically",
            "jobs_processed counter stops advancing",
            "/work and healthz stay green (silent backlog)",
        ],
        canonical_root_cause="The queue consumer (worker) stalled; jobs accumulate "
        "unprocessed in the queue",
        canonical_remediation="Restart/unpause the worker service to resume consumption",
        severity=Severity.medium,
    )
    log_signature = None  # silent fault: signal is metric divergence, not an error log

    def inject(self, ctrl: SandboxController) -> None:
        ctrl.pause("worker")

    def revert(self, ctrl: SandboxController) -> None:
        ctrl.unpause("worker")

    def symptoms_present(self, snapshots: Sequence[ProbeSnapshot]) -> bool:
        with_metrics = [
            s for s in snapshots if s.metrics and s.metrics.get("queue_depth") is not None
        ]
        if len(with_metrics) < 2:
            return False
        first, last = with_metrics[0], with_metrics[-1]
        depth_grew = last.metrics["queue_depth"] > first.metrics["queue_depth"]
        processed_stalled = last.metrics["jobs_processed"] == first.metrics["jobs_processed"]
        return depth_grew and processed_stalled


class ExpiredCredential(Fault):
    spec = FaultSpec(
        id="expired_credential",
        name="Expired database credential",
        trigger="rotate role 'app' password on the db side without updating the app secret",
        expected_symptoms=[
            "healthz reports db component down (503)",
            "logs show 'password authentication failed' for user app",
        ],
        canonical_root_cause="The app's database credential is no longer valid (password "
        "rotated/expired on the database side)",
        canonical_remediation="Restore a valid credential for role 'app' (reset password "
        "to the secret the app uses)",
        severity=Severity.high,
    )
    log_signature = "password authentication failed"

    def inject(self, ctrl: SandboxController) -> None:
        ctrl.psql("ALTER ROLE app WITH PASSWORD 'rotated-secret';")

    def revert(self, ctrl: SandboxController) -> None:
        ctrl.psql("ALTER ROLE app WITH PASSWORD 'app_pw';")

    def symptoms_present(self, snapshots: Sequence[ProbeSnapshot]) -> bool:
        return any(_component_down(s, "db") for s in snapshots)


FAULTS: dict[str, Fault] = {
    f.spec.id: f
    for f in [
        DbConnectionExhaustion(),
        BadConfigRollout(),
        DownstreamDependencyTimeout(),
        QueueConsumerStall(),
        ExpiredCredential(),
    ]
}


def get_fault(fault_id: str) -> Fault:
    try:
        return FAULTS[fault_id]
    except KeyError:
        raise KeyError(f"unknown fault id '{fault_id}'; known: {sorted(FAULTS)}") from None
