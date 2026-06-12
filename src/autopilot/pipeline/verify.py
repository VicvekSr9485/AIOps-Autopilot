"""Verifier: re-check sandbox signals after execution and decide resolved-or-not.

Stage-scoped to telemetry only (exposure: verification -> telemetry): the
verifier observes through get_active_alerts/query_metrics probes — it cannot
act. Checks: every probe healthy, no active alerts, AND the backlog draining
(silent faults keep probes green; a queue that grows — or sits stuck above
zero — while jobs_processed is flat means the incident is NOT resolved).

Convergence window: a just-applied remediation needs time to take effect on a
REAL stack (a restarted container passes healthchecks seconds later; a revived
consumer drains its backlog over seconds). With settle_timeout_s > 0 the
checks are re-polled until they all pass or the bounded deadline expires, and
the FINAL measurement is what gets recorded — sampling a still-restarting
stack would measure restart latency, not remediation correctness. The default
is 0 (single immediate measurement), which is exact for the mock sandbox's
instant state flips and keeps unit tests fast.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

import structlog

from autopilot.mcp_servers.exposure import filter_servers
from autopilot.models import VerificationCheck, VerificationResult
from autopilot.tracing import span

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = structlog.get_logger("autopilot.pipeline.verify")


async def _run_checks(
    telemetry: FastMCP, samples: int, interval_s: float
) -> list[VerificationCheck]:
    content = await telemetry.call_tool(
        "get_active_alerts", {"samples": samples, "interval_s": interval_s}
    )
    alerts = json.loads(content[0].text)

    checks = [
        VerificationCheck(
            name="all_probes_healthy",
            passed=alerts["failing_probes"] == 0,
            detail=f"{alerts['failing_probes']}/{alerts['probes']} probes failing",
        ),
        VerificationCheck(
            name="no_active_alerts",
            passed=not alerts["alerts"],
            detail=", ".join(a["name"] for a in alerts["alerts"]) or "none firing",
        ),
    ]

    content = await telemetry.call_tool(
        "query_metrics",
        {"names": ["queue_depth", "jobs_processed"],
         "samples": samples, "interval_s": interval_s},
    )
    series = {s["name"]: s for s in json.loads(content[0].text)["series"]}
    depth, processed = series.get("queue_depth"), series.get("jobs_processed")
    if depth and processed:
        growing = depth["delta"] > 0 and processed["delta"] == 0
        stuck = (depth["last"] > 0 and depth["delta"] == 0
                 and processed["delta"] == 0)
        checks.append(VerificationCheck(
            name="backlog_draining",
            passed=not growing and not stuck,
            detail=(f"queue_depth {depth['first']:g}->{depth['last']:g}, "
                    f"jobs_processed delta {processed['delta']:+g}"),
        ))
    return checks


async def verify(
    incident_id: str,
    servers: Mapping[str, FastMCP],
    *,
    samples: int = 3,
    interval_s: float = 1.0,
    settle_timeout_s: float = 0.0,
    settle_poll_s: float = 2.0,
) -> VerificationResult:
    scoped = filter_servers("verification", servers)  # telemetry only
    telemetry = scoped["telemetry"]

    with span("verification", incident_id=incident_id):
        deadline = time.monotonic() + settle_timeout_s
        attempts = 0
        while True:
            attempts += 1
            checks = await _run_checks(telemetry, samples, interval_s)
            if all(c.passed for c in checks) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(settle_poll_s)

        result = VerificationResult(
            incident_id=incident_id,
            resolved=all(c.passed for c in checks),
            checks=checks,
        )
        log.info("verification_done", step="verification", incident_id=incident_id,
                 resolved=result.resolved, settle_attempts=attempts,
                 failed=[c.name for c in checks if not c.passed])
        return result
