"""Verifier: re-check sandbox signals after execution and decide resolved-or-not.

Stage-scoped to telemetry only (exposure: verification -> telemetry): the
verifier observes through get_active_alerts probes — it cannot act.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

import structlog

from autopilot.mcp_servers.exposure import filter_servers
from autopilot.models import VerificationCheck, VerificationResult
from autopilot.tracing import span

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = structlog.get_logger("autopilot.pipeline.verify")


async def verify(
    incident_id: str,
    servers: Mapping[str, FastMCP],
    *,
    samples: int = 3,
    interval_s: float = 1.0,
) -> VerificationResult:
    scoped = filter_servers("verification", servers)  # telemetry only
    telemetry = scoped["telemetry"]

    with span("verification", incident_id=incident_id):
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
        result = VerificationResult(
            incident_id=incident_id,
            resolved=all(c.passed for c in checks),
            checks=checks,
        )
        log.info("verification_done", step="verification", incident_id=incident_id,
                 resolved=result.resolved,
                 failed=[c.name for c in checks if not c.passed])
        return result
