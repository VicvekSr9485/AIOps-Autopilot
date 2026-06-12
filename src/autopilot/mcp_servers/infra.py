"""Infra/Ops MCP server: the ONLY action surface, scoped to the sandbox stack.

Every mutating tool:
- takes `dry_run` (default TRUE — callers must opt in to act),
- is idempotent (converges to a declared target state; config tools no-op when
  the state already matches),
- exposes targets only as the closed `SandboxService` enum (no free-text field
  exists with which to aim outside the sandbox) and re-validates at runtime.

Deterministic values are injected server-side, never model-supplied: the
compose namespace comes from the controller bound at build time, and the
config tools' target ("app") is fixed in the tool body.

NOTE: no `from __future__ import annotations` here — FastMCP 1.9.4 inspects real
(non-string) annotations when registering tools.
"""

from datetime import datetime
from typing import Annotated

import structlog
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from autopilot.mcp_servers.guards import (
    SANDBOX_COMPOSE_PROJECT,
    SandboxService,
    ensure_sandbox_service,
    truncate,
)
from autopilot.sandbox.controller import SandboxController

log = structlog.get_logger("autopilot.mcp.infra")


class OpResult(BaseModel):
    tool: str
    target: str
    namespace: str = SANDBOX_COMPOSE_PROJECT  # injected server-side, never a param
    dry_run: bool
    changed: bool  # state differed from target (or would, under dry_run)
    executed: bool  # an action actually ran (always False under dry_run)
    success: bool
    detail: str


class AppConfigPatch(BaseModel):
    """Partial app config; unknown keys are rejected at the schema boundary."""

    model_config = ConfigDict(extra="forbid")

    feature_mode: str | None = None
    downstream_url: str | None = None
    downstream_timeout_s: float | None = None


class HealthCheckResult(BaseModel):
    healthy: bool
    healthz_status: int | None
    work_status: int | None
    components: dict[str, bool]  # component name -> ok
    captured_at: datetime


def _log_op(result: OpResult) -> OpResult:
    log.info("mcp_tool", step="mcp.infra", tool=result.tool, target=result.target,
             dry_run=result.dry_run, changed=result.changed, executed=result.executed,
             success=result.success)
    return result


def build_infra_server(ctrl: SandboxController | None = None) -> FastMCP:
    ctrl = ctrl or SandboxController()
    mcp = FastMCP(
        "autopilot-infra",
        instructions="Mutating operations against the autopilot sandbox compose stack "
        "ONLY. All mutating tools default to dry_run=true; pass dry_run=false to act. "
        "Targets outside the sandbox are refused.",
    )

    @mcp.tool()
    def restart_service(service: SandboxService, dry_run: bool = True) -> OpResult:
        """Restart one sandbox service. Idempotent: converges to a freshly-running
        service. dry_run=true (default) only reports what would happen."""
        service = ensure_sandbox_service(service)
        if not dry_run:
            ctrl.restart(service)
        verb = "restarted" if not dry_run else "would restart"
        return _log_op(OpResult(tool="restart_service", target=service, dry_run=dry_run,
                                changed=True, executed=not dry_run, success=True,
                                detail=f"{verb} sandbox service '{service}'"))

    @mcp.tool()
    def scale_service(service: SandboxService,
                      replicas: Annotated[int, Field(ge=0, le=3)],
                      dry_run: bool = True) -> OpResult:
        """Scale one sandbox service to N replicas (0 stops it). Idempotent: converges
        to the requested replica count. NOTE: sandbox services pin container names, so
        compose rejects replicas > 1. dry_run=true (default) only reports."""
        service = ensure_sandbox_service(service)
        detail = f"scale sandbox service '{service}' to {replicas} replica(s)"
        if dry_run:
            return _log_op(OpResult(tool="scale_service", target=service, dry_run=True,
                                    changed=True, executed=False, success=True,
                                    detail=f"would {detail}"))
        try:
            ctrl.scale(service, replicas)
            return _log_op(OpResult(tool="scale_service", target=service, dry_run=False,
                                    changed=True, executed=True, success=True,
                                    detail=f"did {detail}"))
        except RuntimeError as e:
            return _log_op(OpResult(tool="scale_service", target=service, dry_run=False,
                                    changed=False, executed=True, success=False,
                                    detail=truncate(f"compose refused: {e}", 400)))

    @mcp.tool()
    def apply_config(patch: AppConfigPatch, dry_run: bool = True) -> OpResult:
        """Apply a partial config change to the sandbox app (then restart it so the
        config is re-read). Idempotent: no-ops when the active config already matches.
        dry_run=true (default) only reports the keys that would change."""
        current = ctrl.read_app_config()
        desired = dict(current)
        desired.update(patch.model_dump(exclude_none=True))
        changed_keys = sorted(k for k in desired if desired[k] != current.get(k))
        if not changed_keys:
            return _log_op(OpResult(tool="apply_config", target="app", dry_run=dry_run,
                                    changed=False, executed=False, success=True,
                                    detail="config already matches; nothing to do"))
        detail = f"set {', '.join(f'{k}={desired[k]!r}' for k in changed_keys)} and restart app"
        if not dry_run:
            ctrl.write_app_config(desired)
            ctrl.restart("app")
        return _log_op(OpResult(tool="apply_config", target="app", dry_run=dry_run,
                                changed=True, executed=not dry_run, success=True,
                                detail=("did " if not dry_run else "would ") + detail))

    @mcp.tool()
    def rollback(dry_run: bool = True) -> OpResult:
        """Roll the sandbox app config back to the canonical (last known-good) version
        and restart the app. Idempotent: no-ops when already canonical. dry_run=true
        (default) only reports."""
        current = ctrl.read_app_config()
        canonical = ctrl.default_app_config()
        if current == canonical:
            return _log_op(OpResult(tool="rollback", target="app", dry_run=dry_run,
                                    changed=False, executed=False, success=True,
                                    detail="config already canonical; nothing to do"))
        diff_keys = sorted(k for k in canonical if canonical[k] != current.get(k))
        detail = f"restore canonical config (reverting {', '.join(diff_keys)}) and restart app"
        if not dry_run:
            ctrl.write_app_config(canonical)
            ctrl.restart("app")
        return _log_op(OpResult(tool="rollback", target="app", dry_run=dry_run,
                                changed=True, executed=not dry_run, success=True,
                                detail=("did " if not dry_run else "would ") + detail))

    @mcp.tool()
    def health_check() -> HealthCheckResult:
        """Probe sandbox health (read-only): /healthz component status plus whether
        /work currently succeeds."""
        snap = ctrl.probe()
        components = {
            name: bool(info.get("ok"))
            for name, info in (snap.healthz_body or {}).get("components", {}).items()
        }
        result = HealthCheckResult(healthy=snap.healthy, healthz_status=snap.healthz_status,
                                   work_status=snap.work_status, components=components,
                                   captured_at=snap.captured_at)
        log.info("mcp_tool", step="mcp.infra", tool="health_check", healthy=snap.healthy)
        return result

    return mcp
