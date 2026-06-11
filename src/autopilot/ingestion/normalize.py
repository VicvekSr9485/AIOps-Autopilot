"""Normalize raw sandbox captures (alert + compose logs + metric snapshots) into a
typed Incident. This is the ONLY door into the agent pipeline — and it must never
carry ground-truth fault metadata.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

import structlog

from autopilot.models import AlertEvent, Incident, LogRecord, MetricPoint, Telemetry
from autopilot.sandbox.controller import ProbeSnapshot

log = structlog.get_logger("autopilot.ingestion")

_METRIC_FIELDS = (
    "requests_total",
    "errors_total",
    "work_success_total",
    "queue_depth",
    "jobs_processed",
)


def parse_compose_logs(text: str) -> list[LogRecord]:
    """Parse `docker compose logs --no-color -t` output: '<container> | <ts> <message>'."""
    records: list[LogRecord] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        prefix, rest = line.split("|", 1)
        service = prefix.strip().removeprefix("autopilot-sbx-")
        rest = rest.strip()

        timestamp: datetime | None = None
        message = rest
        first, _, remainder = rest.partition(" ")
        try:
            timestamp = datetime.fromisoformat(first.replace("Z", "+00:00"))
            message = remainder.strip()
        except ValueError:
            pass

        try:  # our services log JSON lines; surface the event/error compactly
            payload = json.loads(message)
            if isinstance(payload, dict) and "event" in payload:
                details = {
                    k: v for k, v in payload.items() if k not in ("ts", "service", "event")
                }
                message = payload["event"] + (f" {json.dumps(details)}" if details else "")
        except (json.JSONDecodeError, TypeError):
            pass

        records.append(
            LogRecord(service=service, message=message, timestamp=timestamp, raw=line)
        )
    return records


def metrics_from_snapshots(snapshots: Sequence[ProbeSnapshot]) -> list[MetricPoint]:
    points: list[MetricPoint] = []
    for snap in snapshots:
        if not snap.metrics:
            continue
        for name in _METRIC_FIELDS:
            value = snap.metrics.get(name)
            if value is not None:
                points.append(
                    MetricPoint(
                        name=name,
                        value=float(value),
                        captured_at=snap.captured_at,
                        labels={"service": "app", "env": "sandbox"},
                    )
                )
    return points


def synthesize_alert(snapshots: Sequence[ProbeSnapshot]) -> AlertEvent:
    """Derive a generic alert purely from observed signals — never from ground truth."""
    from autopilot.models import Severity  # local import to keep module deps flat

    total = max(len(snapshots), 1)
    health_failures = sum(1 for s in snapshots if s.healthz_status != 200)
    work_failures = sum(1 for s in snapshots if s.work_status != 200)

    if health_failures:
        name, worst = "sandbox.app.health_degraded", health_failures
    elif work_failures:
        name, worst = "sandbox.app.work_errors", work_failures
    else:
        name, worst = "sandbox.app.anomaly_suspected", 0

    if worst == total:
        severity = Severity.high
    elif worst > 0:
        severity = Severity.medium
    else:
        severity = Severity.low

    fired_at = snapshots[0].captured_at if snapshots else None
    alert = AlertEvent(
        name=name,
        severity=severity,
        source="sandbox-probe",
        description=(
            f"{health_failures}/{total} health checks failing, "
            f"{work_failures}/{total} work requests failing"
        ),
        labels={"service": "app", "env": "sandbox"},
        **({"fired_at": fired_at} if fired_at else {}),
    )
    log.info("alert_synthesized", step="ingestion", alert=alert.name, severity=severity.value)
    return alert


def build_incident(
    log_text: str,
    snapshots: Sequence[ProbeSnapshot],
    incident_id: str | None = None,
) -> Incident:
    alert = synthesize_alert(snapshots)
    telemetry = Telemetry(
        alert=alert,
        logs=parse_compose_logs(log_text),
        metrics=metrics_from_snapshots(snapshots),
    )
    kwargs = {"id": incident_id} if incident_id else {}
    incident = Incident(
        title=f"[{alert.severity.value}] {alert.name}: {alert.description}",
        telemetry=telemetry,
        **kwargs,
    )
    log.info(
        "incident_built",
        step="ingestion",
        incident_id=incident.id,
        logs=len(telemetry.logs),
        metrics=len(telemetry.metrics),
    )
    return incident
