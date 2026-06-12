"""Live benchmark environment: real fault injection into the docker-compose
sandbox via the ScenarioRunner, real MCP servers over the SandboxController.

Used ONLY by the final real-model benchmark run (`python -m autopilot.benchmark
--real`). Each world reset-injects its fault from a deterministic baseline;
cleanup best-effort reverts whatever the agent left behind (the next world's
reset is the hard guarantee)."""

from __future__ import annotations

import structlog

from autopilot.harness.runner import ScenarioRunner
from autopilot.mcp_servers.context import RunContext
from autopilot.mcp_servers.infra import build_infra_server
from autopilot.mcp_servers.knowledge import build_knowledge_server
from autopilot.mcp_servers.store import KnowledgeStore
from autopilot.mcp_servers.telemetry import build_telemetry_server

log = structlog.get_logger("autopilot.benchmark.liveenv")


class LiveWorld:
    """One scenario against the REAL sandbox stack (Docker required)."""

    def __init__(self, fault_id: str):
        self.fault_id = fault_id
        self.runner = ScenarioRunner()
        self.incident = self.runner.run(fault_id)  # reset -> inject -> capture
        self.ctrl = self.runner.ctrl
        self.store = KnowledgeStore(":memory:")
        self.context = RunContext()
        self.servers = {
            "telemetry": build_telemetry_server(self.ctrl),
            "infra": build_infra_server(self.ctrl),
            "knowledge": build_knowledge_server(store=self.store, context=self.context),
        }

    def cleanup(self) -> None:
        """Best-effort revert; if the agent already fixed the fault the revert
        may no-op or fail harmlessly — the next scenario starts from reset()."""
        try:
            self.runner.cleanup()
        except Exception as e:
            log.warning("live_cleanup_failed", step="benchmark.liveenv",
                        fault_id=self.fault_id, error=str(e)[:200])
