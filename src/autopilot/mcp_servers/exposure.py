"""Stage-scoped tool exposure: each pipeline stage sees the minimal MCP server
subset its job needs, not the full catalog.

- triage / root_cause — observe and recall: telemetry + knowledge
- planner — reasons over evidence already gathered; NO tools at all
- executor — acts (post-HITL): infra/ops only
- verification — re-checks signals: telemetry only

Smaller toolsets cut prompt tokens per LLM call and shrink the action surface:
a stage that cannot see a tool cannot call it. New stages must be added to
STAGE_SERVERS explicitly — the default is no tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import structlog
from mcp.server.fastmcp import FastMCP

log = structlog.get_logger("autopilot.mcp.exposure")

Stage = Literal["triage", "root_cause", "planner", "executor", "verification"]

SERVER_NAMES = frozenset({"telemetry", "infra", "knowledge"})

STAGE_SERVERS: dict[str, frozenset[str]] = {
    "triage": frozenset({"telemetry", "knowledge"}),
    "root_cause": frozenset({"telemetry", "knowledge"}),
    "planner": frozenset(),
    "executor": frozenset({"infra"}),
    "verification": frozenset({"telemetry"}),
}


def servers_for_stage(stage: str) -> frozenset[str]:
    """Names of the MCP servers a pipeline stage may expose to its LLM call."""
    try:
        return STAGE_SERVERS[stage]
    except KeyError:
        raise KeyError(
            f"unknown pipeline stage {stage!r}; known: {sorted(STAGE_SERVERS)}"
        ) from None


def filter_servers(stage: str, servers: Mapping[str, FastMCP]) -> dict[str, FastMCP]:
    """Subset a built {name: server} map down to what `stage` is allowed to see."""
    allowed = servers_for_stage(stage)
    exposed = {name: srv for name, srv in servers.items() if name in allowed}
    log.info("stage_tools_scoped", step="mcp.exposure", stage=stage,
             exposed=sorted(exposed), withheld=sorted(set(servers) - set(exposed)))
    return exposed
