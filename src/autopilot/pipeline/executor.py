"""Executor: the ONLY pipeline component granted Infra/Ops tools
(exposure.filter_servers("executor")). Refuses unapproved proposals.

Per step: dry-run first (the tool reports exactly what would happen), then
apply. A failed dry-run or apply halts execution. Sandbox-only is enforced
below this layer — closed-enum targets + server-side namespace injection in
the Infra MCP server — so even a malformed proposal cannot aim elsewhere.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog
from mcp.server.fastmcp.exceptions import ToolError

from autopilot.mcp_servers.exposure import filter_servers
from autopilot.models import ExecutionResult, RemediationProposal, StepOutcome, utcnow
from autopilot.tracing import span

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = structlog.get_logger("autopilot.pipeline.executor")


class ExecutionRefused(RuntimeError):
    """Proposal not approved, or steps don't map to executable actions."""


def _step_call(step) -> tuple[str, dict[str, Any]]:
    """Map a RemediationStep onto an Infra/Ops tool invocation."""
    params = json.loads(step.command) if step.command else {}
    if step.action == "restart_service":
        return "restart_service", {"service": step.target}
    if step.action == "scale_service":
        if "replicas" not in params:
            raise ExecutionRefused(f"step {step.order}: scale_service needs replicas")
        return "scale_service", {"service": step.target, "replicas": params["replicas"]}
    if step.action == "apply_config":
        return "apply_config", {"patch": params}
    if step.action == "rollback":
        return "rollback", {}
    raise ExecutionRefused(f"step {step.order}: unknown action {step.action!r}")


async def _invoke(infra: FastMCP, tool: str, args: dict[str, Any]) -> dict:
    try:
        content = await infra.call_tool(tool, args)
        return json.loads(content[0].text)
    except ToolError as e:
        # A tool exception is a failed step, not a crashed pipeline.
        return {"success": False, "detail": str(e)[:300]}


async def execute(
    proposal: RemediationProposal,
    servers: Mapping[str, FastMCP],
    *,
    use_rollback_plan: bool = False,
) -> ExecutionResult:
    if proposal.requires_approval and not use_rollback_plan:
        raise ExecutionRefused(
            f"proposal {proposal.id} still requires approval; route it through the "
            "HITL gate first"
        )
    scoped = filter_servers("executor", servers)  # infra/ops only
    infra = scoped["infra"]

    steps = proposal.rollback_plan if use_rollback_plan else proposal.steps
    phase = "rollback" if use_rollback_plan else "remediation"
    started = utcnow()
    outcomes: list[StepOutcome] = []
    error: str | None = None

    with span("executor", incident_id=proposal.incident_id, phase=phase,
              steps=len(steps)):
        for step in sorted(steps, key=lambda s: s.order):
            try:
                tool, args = _step_call(step)
            except ExecutionRefused as e:
                error = str(e)
                outcomes.append(StepOutcome(step_order=step.order, success=False,
                                            output=error))
                break

            dry = await _invoke(infra, tool, {**args, "dry_run": True})
            if not dry.get("success"):
                error = f"step {step.order} dry-run refused: {dry.get('detail', '')}"
                outcomes.append(StepOutcome(step_order=step.order, success=False,
                                            output=error))
                break

            applied = await _invoke(infra, tool, {**args, "dry_run": False})
            outcomes.append(StepOutcome(
                step_order=step.order,
                success=bool(applied.get("success")),
                output=applied.get("detail", ""),
            ))
            log.info("executor_step", step="executor",
                     incident_id=proposal.incident_id, phase=phase,
                     order=step.order, tool=tool, success=applied.get("success"))
            if not applied.get("success"):
                error = f"step {step.order} failed: {applied.get('detail', '')}"
                break

    return ExecutionResult(
        proposal_id=proposal.id,
        incident_id=proposal.incident_id,
        success=error is None and all(o.success for o in outcomes) and bool(outcomes),
        step_outcomes=outcomes,
        started_at=started,
        finished_at=utcnow(),
        error=error,
    )
