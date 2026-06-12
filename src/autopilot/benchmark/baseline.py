"""Single-prompt BASELINE: the thing the pipeline must beat.

Contract (mirrored in .claude/rules/benchmark.md):
- ONE prompt containing the (summarized) telemetry; the model returns root
  cause + remediation steps in a single response. No stages, no tools, no
  retrieval, no HITL gate, no verification-driven rollback.
- Same reasoning-tier model as the pipeline's root-cause step (generous to the
  baseline) and the same bounded strict-JSON retry discipline, so the
  comparison isolates ARCHITECTURE, not model choice or parsing luck.
- Its remediation is applied to the sandbox by the benchmark harness exactly as
  proposed — gatelessness is the point being measured, so bad plans land as
  false remediations instead of being filtered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

import structlog
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from autopilot.llm.client import QwenClient
from autopilot.models import Incident, RemediationStep
from autopilot.pipeline.structured import StructuredOutputError, complete_structured
from autopilot.pipeline.summarize import summarize_telemetry
from autopilot.tracing import span

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = structlog.get_logger("autopilot.benchmark.baseline")

STEP = "baseline.single_prompt"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TOKEN_CAP = 16_000


class _LLMBaselineStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["restart_service", "scale_service", "apply_config", "rollback"]
    target: Literal["app", "worker", "downstream", "db", "queue"] = "app"
    params: dict[str, Any] = Field(default_factory=dict)


class _LLMBaseline(BaseModel):
    model_config = ConfigDict(extra="ignore")

    root_cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    steps: list[_LLMBaselineStep] = Field(min_length=1, max_length=3)


_SYSTEM_PROMPT = (
    "You are a single-prompt SRE assistant for a sandboxed compose stack. From "
    "the incident below, decide the root cause AND the remediation in one shot.\n"
    "Available actions (the ONLY ones that exist):\n"
    "- restart_service: params {} — restart one service\n"
    "- scale_service: params {\"replicas\": 0..3} — scale a service\n"
    "- apply_config: params = partial app config (keys: feature_mode, "
    "downstream_url, downstream_timeout_s); targets the app service\n"
    "- rollback: params {} — restore the canonical app config; targets app\n"
    "Services: app, worker, downstream, db, queue.\n"
    "Respond with STRICT JSON only — no prose, no markdown — matching:\n"
    '{"root_cause": str, "confidence": float 0..1, '
    '"steps": [{"action": str, "target": str, "params": object}]}'
)


class BaselineResult(BaseModel):
    incident_id: str
    root_cause: str = ""
    confidence: float = 0.0
    steps: list[RemediationStep] = Field(default_factory=list)
    schema_failed: bool = False
    error: str | None = None


def run_baseline(
    incident: Incident,
    client: QwenClient,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    token_cap: int = DEFAULT_TOKEN_CAP,
) -> BaselineResult:
    with span("baseline", incident_id=incident.id):
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"INCIDENT {incident.id}\n"
                f"{summarize_telemetry(incident.telemetry)}"
            )},
        ]
        try:
            payload, tokens_spent = complete_structured(
                client, "reasoning", messages, _LLMBaseline,
                step=STEP, max_attempts=max_attempts, token_cap=token_cap,
            )
        except StructuredOutputError as e:
            log.warning("baseline_schema_failed", step=STEP,
                        incident_id=incident.id, error=str(e)[:200])
            return BaselineResult(incident_id=incident.id, schema_failed=True,
                                  error=str(e)[:300])

        steps = [
            RemediationStep(
                order=i + 1,
                action=s.action,
                target="app" if s.action in ("apply_config", "rollback") else s.target,
                command=json.dumps(s.params, sort_keys=True) if s.params else None,
            )
            for i, s in enumerate(payload.steps)
        ]
        log.info("baseline_answered", step=STEP, incident_id=incident.id,
                 confidence=payload.confidence, steps=len(steps),
                 tokens_spent=tokens_spent)
        return BaselineResult(
            incident_id=incident.id,
            root_cause=payload.root_cause,
            confidence=payload.confidence,
            steps=steps,
        )


class BaselineApplication(BaseModel):
    applied: bool
    success: bool
    invalid_tool_calls: int = 0
    detail: str = ""


async def apply_baseline(
    result: BaselineResult, servers: Mapping[str, FastMCP]
) -> BaselineApplication:
    """Harness-side application of the baseline's plan — deliberately gateless
    and rollback-less (the baseline has neither). Sandbox-only still holds
    structurally: the infra server's closed enums and namespace injection sit
    below this call, exactly as they do for the executor."""
    if not result.steps:
        return BaselineApplication(applied=False, success=False,
                                   detail="no steps to apply")
    infra = servers["infra"]
    invalid = 0
    for s in sorted(result.steps, key=lambda s: s.order):
        params = json.loads(s.command) if s.command else {}
        if s.action == "restart_service":
            args: dict[str, Any] = {"service": s.target}
        elif s.action == "scale_service":
            args = {"service": s.target, "replicas": params.get("replicas", 1)}
        elif s.action == "apply_config":
            args = {"patch": params}
        else:  # rollback
            args = {}
        try:
            content = await infra.call_tool(s.action, {**args, "dry_run": False})
            outcome = json.loads(content[0].text)
            if not outcome.get("success"):
                return BaselineApplication(
                    applied=True, success=False, invalid_tool_calls=invalid,
                    detail=f"step {s.order} failed: {outcome.get('detail', '')[:200]}",
                )
        except ToolError as e:
            invalid += 1
            return BaselineApplication(
                applied=True, success=False, invalid_tool_calls=invalid,
                detail=f"step {s.order} invalid tool call: {str(e)[:200]}",
            )
    return BaselineApplication(applied=True, success=True,
                               invalid_tool_calls=invalid, detail="all steps applied")
