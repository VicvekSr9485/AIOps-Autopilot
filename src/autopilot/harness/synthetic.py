"""Synthetic raw captures (compose log text + probe snapshots) mimicking what
each of the 5 library faults looks like FROM THE OUTSIDE — so ingestion/triage
tests and the mock-mode benchmark run offline without Docker. Built from
observable symptoms only; no ground-truth fault metadata appears anywhere in
these captures."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from autopilot.sandbox.controller import ProbeSnapshot

T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)

FAULT_IDS = [
    "db_pool_exhaustion",
    "bad_config_rollout",
    "downstream_timeout",
    "queue_consumer_stall",
    "expired_credential",
    "config_rollout_worker_wedge",
    "db_outage_ambiguous",
    "worker_scaled_to_zero",
]


def healthy_snap(**metrics) -> ProbeSnapshot:
    return ProbeSnapshot(
        captured_at=T0,
        healthz_status=200,
        healthz_body={"status": "ok",
                      "components": {"db": {"ok": True}, "queue": {"ok": True}}},
        work_status=200,
        work_body={"status": "done"},
        metrics={"requests_total": 1, "errors_total": 0, "work_success_total": 1,
                 "queue_depth": 0, "jobs_processed": 0, **metrics},
    )


def _log_line(event: str, i: int = 0, service: str = "app", **fields) -> str:
    ts = (T0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    payload = {"ts": ts, "service": service, "event": event, **fields}
    return f"autopilot-sbx-{service}  | {ts} {json.dumps(payload)}"


def _snap(i: int, healthz: int = 200, work: int = 200, db_ok: bool = True,
          db_error: str = "", work_body: dict | None = None,
          metrics: dict | None = None) -> ProbeSnapshot:
    components = {"db": {"ok": db_ok}, "queue": {"ok": True}}
    if not db_ok:
        components["db"]["error"] = db_error
    return ProbeSnapshot(
        captured_at=T0 + timedelta(seconds=i),
        healthz_status=healthz,
        healthz_body={"status": "ok" if healthz == 200 else "degraded",
                      "components": components},
        work_status=work,
        work_body=work_body or {"status": "done"},
        metrics=metrics,
    )


def _metrics(i: int, queue_depth: int, jobs_processed: int) -> dict:
    return {"requests_total": 10 + i, "errors_total": 0, "work_success_total": 10 + i,
            "queue_depth": queue_depth, "jobs_processed": jobs_processed}


def scenario_capture(fault_id: str) -> tuple[str, list[ProbeSnapshot]]:
    """(compose log text, probe snapshots) as the harness would capture them."""
    if fault_id == "db_pool_exhaustion":
        err = "FATAL: remaining connection slots are reserved for roles with privileges"
        logs = [_log_line("healthz_component_failed", i, component="db", error=err)
                for i in range(4)]
        logs += [_log_line("work_failed", i + 4, reason="db_error", error=err)
                 for i in range(8)]
        snaps = [_snap(i, healthz=503, work=500, db_ok=False, db_error=err,
                       work_body={"error": f"db error: {err}"}) for i in range(4)]
        return "\n".join(logs), snaps

    if fault_id == "bad_config_rollout":
        logs = [_log_line("work_failed", i, reason="invalid_feature_mode",
                          feature_mode="turbo_v2") for i in range(10)]
        snaps = [_snap(i, work=500,
                       work_body={"error": "invalid feature_mode 'turbo_v2'"})
                 for i in range(4)]
        return "\n".join(logs), snaps

    if fault_id == "downstream_timeout":
        logs = [_log_line("work_failed", i, reason="downstream_timeout",
                          error="timed out after 1.5s") for i in range(6)]
        snaps = [_snap(i, work=504, work_body={"error": "downstream_timeout"})
                 for i in range(4)]
        return "\n".join(logs), snaps

    if fault_id == "queue_consumer_stall":
        # Silent fault: everything green, queue_depth grows, jobs_processed flat.
        logs = [_log_line("work_done", i, items=40 + i) for i in range(6)]
        snaps = [_snap(i, metrics=_metrics(i, queue_depth=2 + 3 * i, jobs_processed=37))
                 for i in range(4)]
        return "\n".join(logs), snaps

    if fault_id == "expired_credential":
        err = 'FATAL: password authentication failed for user "app"'
        logs = [_log_line("healthz_component_failed", i, component="db", error=err)
                for i in range(5)]
        logs += [_log_line("work_failed", i + 5, reason="db_error", error=err)
                 for i in range(5)]
        snaps = [_snap(i, healthz=503, work=500, db_ok=False, db_error=err,
                       work_body={"error": f"db error: {err}"}) for i in range(4)]
        return "\n".join(logs), snaps

    if fault_id == "config_rollout_worker_wedge":
        # Bad rollout blocks all work requests AND wedged the consumer: the
        # backlog enqueued before the rollout sits stuck (depth flat above
        # zero, jobs_processed flat) while producers fail at the config check.
        logs = [_log_line("work_failed", i, reason="invalid_feature_mode",
                          feature_mode="turbo_v2") for i in range(8)]
        snaps = [_snap(i, work=500,
                       work_body={"error": "invalid feature_mode 'turbo_v2'"},
                       metrics=_metrics(i, queue_depth=12, jobs_processed=37))
                 for i in range(4)]
        return "\n".join(logs), snaps

    if fault_id == "db_outage_ambiguous":
        # Degraded alert-time observability: the capture carries only generic
        # driver-level connection errors. The decisive FATAL detail exists
        # solely in the db service's LIVE logs (telemetry query during triage)
        # — a no-tool reader of this bundle genuinely cannot disambiguate
        # pool exhaustion from, say, a credential failure.
        err = "db connection failure"
        logs = [_log_line("healthz_component_failed", i, component="db", error=err)
                for i in range(4)]
        logs += [_log_line("work_failed", i + 4, reason="db_error", error=err)
                 for i in range(6)]
        snaps = [_snap(i, healthz=503, work=500, db_ok=False, db_error=err,
                       work_body={"error": f"db error: {err}"}) for i in range(4)]
        return "\n".join(logs), snaps

    if fault_id == "worker_scaled_to_zero":
        # Probes stay green; the worker logged a SIGTERM shutdown and went
        # silent; the backlog grows with nothing consuming.
        logs = [_log_line("work_done", i, items=50 + i) for i in range(5)]
        logs.append(_log_line("worker_shutdown", 5, service="worker",
                              signal="SIGTERM"))
        snaps = [_snap(i, metrics=_metrics(i, queue_depth=3 + 4 * i,
                                           jobs_processed=52))
                 for i in range(4)]
        return "\n".join(logs), snaps

    raise KeyError(f"unknown fault id {fault_id!r}")
