"""Ingestion stage: raw heterogeneous capture (compose log text + probe
snapshots) -> typed Incident, wrapped in a structlog span.

Normalization itself lives in ingestion/normalize.py and degrades gracefully:
unparseable log lines are skipped, snapshots may be empty (the synthesized
alert then reports zero observed failures at low severity), metrics may be
absent. This wrapper is the pipeline's stage boundary."""

from __future__ import annotations

from collections.abc import Sequence

from autopilot.ingestion.normalize import build_incident
from autopilot.models import Incident
from autopilot.sandbox.controller import ProbeSnapshot
from autopilot.tracing import span


def ingest(
    log_text: str,
    snapshots: Sequence[ProbeSnapshot],
    incident_id: str | None = None,
) -> Incident:
    with span("ingestion", snapshots=len(snapshots), log_chars=len(log_text)):
        return build_incident(log_text, snapshots, incident_id=incident_id)
