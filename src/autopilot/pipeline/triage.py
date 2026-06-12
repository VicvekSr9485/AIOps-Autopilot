"""Triage & root-cause reasoner: Incident -> ranked RootCauseHypothesis list.

Cost/safety contract:
- This stage makes the ONLY `reasoning`-tier (qwen3.7-max) calls in the whole
  pipeline, and at most `max_attempts` of them (structured-output retries).
- Tool access is stage-scoped via exposure.filter_servers("triage"): telemetry
  + knowledge only — Infra/Ops is structurally out of reach here.
- Telemetry is summarized deterministically BEFORE prompting (never raw-dumped),
  and a hard token cap aborts the stage if attempts blow the budget.
- The model returns STRICT JSON parsed into Pydantic; ranking and incident_id
  are enforced/injected server-side, never trusted from the model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from autopilot.llm.client import QwenClient
from autopilot.mcp_servers.exposure import filter_servers
from autopilot.models import EvidenceRef, Incident, RootCauseHypothesis, TriageResult
from autopilot.pipeline.structured import StructuredOutputError, complete_structured
from autopilot.pipeline.summarize import render_raw_telemetry, summarize_telemetry
from autopilot.tracing import span

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = structlog.get_logger("autopilot.pipeline.triage")

STEP = "triage.root_cause"
DEFAULT_MAX_ATTEMPTS = 3  # hard cap on reasoning-tier calls for this stage
DEFAULT_TOKEN_CAP = 16_000  # hard cap on (input+output) tokens across attempts
MAX_HYPOTHESES = 5

# "summarized" is the production behavior. "raw" exists ONLY for the benchmark's
# summarization ablation (mode B): telemetry enters the prompt un-compacted so
# the token saving of the summarization design can be measured.
ContextMode = Literal["summarized", "raw"]


class TriageError(RuntimeError):
    """Triage could not produce a valid ranked hypothesis list within its caps."""


# LLM-facing payload models: separate from domain models because the model must
# not supply incident_id (injected) and we tolerate/ignore extra fields.


class _LLMEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    pointer: str
    excerpt: str = ""


class _LLMHypothesis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cause: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[_LLMEvidence] = Field(default_factory=list)
    reasoning_summary: str = ""


class _LLMTriage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hypotheses: list[_LLMHypothesis] = Field(min_length=1, max_length=MAX_HYPOTHESES)


_SYSTEM_PROMPT = (
    "You are an SRE root-cause analyst. Given an incident summary, relevant "
    "runbooks, and similar past incidents, produce ranked root-cause hypotheses.\n"
    "Respond with STRICT JSON only — no prose, no markdown fences — matching:\n"
    '{"hypotheses": [{"cause": str, "confidence": float 0..1, '
    '"evidence": [{"kind": "alert"|"log"|"metric", "pointer": str, "excerpt": str}], '
    '"reasoning_summary": str}]}\n'
    f"1 to {MAX_HYPOTHESES} hypotheses, most likely first. Every hypothesis MUST "
    "cite evidence pointers from the provided telemetry (e.g. 'log:app' or "
    "'metric:queue_depth'). Confidence reflects how decisively the evidence "
    "separates this cause from the alternatives."
)


async def _tool_json(server: FastMCP, tool: str, args: dict[str, Any]) -> dict:
    content = await server.call_tool(tool, args)
    return json.loads(content[0].text)


def _to_result(incident_id: str, parsed: _LLMTriage,
               consulted_runbooks: list[str]) -> TriageResult:
    hypotheses = []
    for h in parsed.hypotheses:
        evidence = [
            EvidenceRef(kind=e.kind, pointer=e.pointer, excerpt=e.excerpt[:300])
            for e in h.evidence
            if e.kind in ("alert", "log", "metric")
        ]
        hypotheses.append(
            RootCauseHypothesis(
                incident_id=incident_id,  # injected server-side, never model-supplied
                cause=h.cause,
                confidence=h.confidence,
                evidence=evidence,
                reasoning_summary=h.reasoning_summary,
            )
        )
    hypotheses.sort(key=lambda h: h.confidence, reverse=True)  # don't trust LLM order
    return TriageResult(incident_id=incident_id, hypotheses=hypotheses,
                        consulted_runbooks=consulted_runbooks)


async def _gather_context(
    incident: Incident, scoped: dict[str, FastMCP],
    context_mode: ContextMode = "summarized",
) -> tuple[str, list[str]]:
    """Enrich via the stage's scoped tools (knowledge + telemetry) — deterministic,
    zero LLM tokens. Returns (prompt context, runbook excerpts) — the excerpts are
    carried on the TriageResult so the toolless planner can reuse them."""
    if context_mode == "raw":
        summary = render_raw_telemetry(incident.telemetry)  # ablation mode B only
    else:
        summary = summarize_telemetry(incident.telemetry)
    top_symptoms = " ".join(
        [incident.telemetry.alert.name, incident.telemetry.alert.description]
        + [r.message for r in incident.telemetry.logs[:5]]
    )[:500]

    sections = [f"INCIDENT {incident.id}\n{summary}"]
    runbook_notes: list[str] = []

    if "knowledge" in scoped:
        runbooks = await _tool_json(
            scoped["knowledge"], "search_runbooks", {"query": top_symptoms, "k": 3}
        )
        runbook_notes = [
            f"{r['title']} (score={r['score']}): {r['excerpt'][:400]}"
            for r in runbooks["results"]
        ]
        sections.append(
            "RELEVANT RUNBOOKS:\n" + "\n".join(f"- {note}" for note in runbook_notes)
        )
        past = await _tool_json(
            scoped["knowledge"], "search_past_incidents", {"query": top_symptoms, "k": 2}
        )
        if past["results"]:
            sections.append(
                "SIMILAR PAST INCIDENTS:\n" + "\n".join(
                    f"- {p['title']} (score={p['score']}): {p['excerpt'][:300]}"
                    for p in past["results"]
                )
            )

    if "telemetry" in scoped:
        alerts_now = await _tool_json(
            scoped["telemetry"], "get_active_alerts", {"samples": 2, "interval_s": 0}
        )
        sections.append(
            f"LIVE RE-CHECK: {alerts_now['failing_probes']}/{alerts_now['probes']} "
            f"probes failing now; active alerts: "
            f"{[a['name'] for a in alerts_now['alerts']] or 'none'}"
        )

    return "\n\n".join(sections), runbook_notes


async def run_triage(
    incident: Incident,
    servers: Mapping[str, FastMCP],
    client: QwenClient,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    token_cap: int = DEFAULT_TOKEN_CAP,
    context_mode: ContextMode = "summarized",
) -> TriageResult:
    scoped = filter_servers("triage", servers)  # telemetry + knowledge; never infra

    with span("triage", incident_id=incident.id):
        context, runbook_notes = await _gather_context(incident, scoped, context_mode)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ]
        try:
            parsed, tokens_spent = complete_structured(
                client, "reasoning", messages, _LLMTriage,
                step=STEP, max_attempts=max_attempts, token_cap=token_cap,
            )
        except StructuredOutputError as e:
            raise TriageError(str(e)) from None

        result = _to_result(incident.id, parsed, runbook_notes)
        log.info(
            "triage_hypotheses_ranked", step=STEP, incident_id=incident.id,
            hypotheses=len(result.hypotheses),
            top_confidence=result.top.confidence, tokens_spent=tokens_spent,
        )
        return result
