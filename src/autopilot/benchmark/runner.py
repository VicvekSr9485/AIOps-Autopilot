"""Benchmark runner: agent pipeline vs single-prompt baseline over seeded fault
scenarios, plus the summarization ablation (context mode A vs B).

Measurement discipline:
- ONE QwenClient (one CostMeter) spans the whole run; per-scenario spend is a
  slice of the meter so the run-level free/voucher split stays authoritative
  (locally — the Qwen Cloud Usage page is the real source of truth).
- MODEL CONSISTENCY: the role->model pair is snapshotted at start, re-checked
  after every scenario against both the config and every metered call, and the
  exact strings are recorded in the report. Any mid-run switch aborts the run.
- The HITL gate is auto-answered by scoring.GroundTruthApprover (a perfect
  operator), so escalation behavior is measured without a human present.
- Remediation correctness is VERIFIED, never declared: the action is applied to
  the (mock or live) sandbox and health is re-checked.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

import structlog

from autopilot.benchmark.baseline import apply_baseline, run_baseline
from autopilot.benchmark.metrics import (
    AblationScenario,
    AblationSummary,
    ApproachSummary,
    BenchmarkReport,
    CostSummary,
    ScenarioMetrics,
    classify_outcome,
)
from autopilot.benchmark.scoring import (
    GroundTruthApprover,
    escalation_is_correct,
    score_root_cause,
)
from autopilot.llm.client import QwenClient
from autopilot.models import Incident, utcnow
from autopilot.pipeline.remediation import PlanningError
from autopilot.pipeline.run import run_incident
from autopilot.pipeline.triage import TriageError
from autopilot.pipeline.verify import verify
from autopilot.tracing import span

log = structlog.get_logger("autopilot.benchmark.runner")


class ModelConsistencyError(RuntimeError):
    """The role->model tiering changed (or an unexpected model was called)
    mid-run. Benchmark numbers from mixed models are meaningless — abort."""


class World(Protocol):
    """One scenario's wiring: an incident plus servers over a sandbox-ish
    controller. MockWorld (offline) and LiveWorld (Docker) both satisfy this."""

    incident: Incident
    servers: dict
    context: Any

    def cleanup(self) -> None: ...


WorldFactory = Callable[[str], World]


class ModelGuard:
    def __init__(self, client: QwenClient):
        self.snapshot: dict[str, str] = dict(client.config.model_by_role)
        self._client = client

    def check(self, where: str) -> None:
        current = dict(self._client.config.model_by_role)
        if current != self.snapshot:
            raise ModelConsistencyError(
                f"model tiering changed during the run at {where}: "
                f"started with {self.snapshot}, now {current}"
            )
        allowed = set(self.snapshot.values())
        for rec in self._client.meter.records:
            if rec.model not in allowed:
                raise ModelConsistencyError(
                    f"unexpected model {rec.model!r} metered at step "
                    f"{rec.step!r} (allowed: {sorted(allowed)})"
                )


def _meter_slice(client: QwenClient, start: int) -> list:
    return client.meter.records[start:]


def _spend_fields(records: list) -> dict:
    distinct_stages = len({r.step for r in records})
    return {
        "llm_calls": len(records),
        "input_tokens": sum(r.input_tokens for r in records),
        "output_tokens": sum(r.output_tokens for r in records),
        "total_tokens": sum(r.input_tokens + r.output_tokens for r in records),
        "est_cost_usd": round(sum(r.est_cost_usd for r in records), 6),
        # extra attempts beyond one per stage are structured-output retries
        "schema_retries": max(0, len(records) - distinct_stages),
    }


def _count_invalid_tool_calls(execution) -> int:
    if execution is None:
        return 0
    return sum(
        1 for o in execution.step_outcomes
        if not o.success and any(m in o.output for m in
                                 ("refused", "invalid", "unknown action"))
    )


async def _run_pipeline_scenario(
    fault_id: str, world: World, client: QwenClient,
    context_mode: Literal["summarized", "raw"] = "summarized",
) -> tuple[ScenarioMetrics, dict]:
    approver = GroundTruthApprover(fault_id)
    meter_start = len(client.meter.records)
    t0 = time.perf_counter()
    try:
        report = await run_incident(
            world.incident, world.servers, client, approver, world.context,
            verify_interval_s=0.0, context_mode=context_mode,
        )
    except (TriageError, PlanningError) as e:
        records = _meter_slice(client, meter_start)
        metrics = ScenarioMetrics(
            fault_id=fault_id, approach="pipeline", context_mode=context_mode,
            schema_failed=True, total_time_s=round(time.perf_counter() - t0, 4),
            **_spend_fields(records),
        )
        return metrics, {"error": str(e)[:500]}

    total_time = round(time.perf_counter() - t0, 4)
    records = _meter_slice(client, meter_start)
    top1, top3 = score_root_cause(
        fault_id, [h.cause for h in report.triage.hypotheses])
    executed = report.execution is not None
    metrics = ScenarioMetrics(
        fault_id=fault_id, approach="pipeline", context_mode=context_mode,
        root_cause_top1=top1, root_cause_top3=top3,
        remediation_correct=report.resolved,
        executed=executed,
        resolved=report.resolved,
        auto_resolved=report.resolved and report.gate.route == "auto",
        false_remediation=executed and not report.resolved,
        escalated=report.gate.route == "human",
        outcome=classify_outcome(
            executed=executed, resolved=report.resolved,
            escalated=report.gate.route == "human",
            escalation_correct=escalation_is_correct(fault_id)),
        human_decision=report.gate.human_action,
        rolled_back=report.rolled_back,
        invalid_tool_calls=_count_invalid_tool_calls(report.execution),
        steps_to_diagnosis=sum(
            1 for r in records if r.step == "triage.root_cause"),
        time_to_diagnosis_s=report.stage_seconds.get("triage", 0.0),
        total_time_s=total_time,
        **_spend_fields(records),
    )
    trace = {
        "report": report.model_dump(mode="json"),
        "llm_records": [r.model_dump() for r in records],
        "oracle_decisions": approver.decisions,
    }
    return metrics, trace


async def _run_baseline_scenario(
    fault_id: str, world: World, client: QwenClient,
) -> tuple[ScenarioMetrics, dict]:
    meter_start = len(client.meter.records)
    t0 = time.perf_counter()
    result = run_baseline(world.incident, client)
    diagnosis_time = round(time.perf_counter() - t0, 4)

    application = None
    resolved = False
    if not result.schema_failed:
        application = await apply_baseline(result, world.servers)
        if application.applied:
            verification = await verify(world.incident.id, world.servers,
                                        interval_s=0.0)
            resolved = application.success and verification.resolved

    records = _meter_slice(client, meter_start)
    top1, top3 = score_root_cause(fault_id, [result.root_cause])
    executed = bool(application and application.applied)
    metrics = ScenarioMetrics(
        fault_id=fault_id, approach="baseline",
        root_cause_top1=top1, root_cause_top3=top3,
        remediation_correct=resolved,
        executed=executed,
        resolved=resolved,
        auto_resolved=resolved,  # the baseline never has a human in the loop
        false_remediation=executed and not resolved,
        escalated=False,  # the baseline has no escalation path at all
        outcome=classify_outcome(
            executed=executed, resolved=resolved, escalated=False,
            escalation_correct=escalation_is_correct(fault_id)),
        schema_failed=result.schema_failed,
        invalid_tool_calls=application.invalid_tool_calls if application else 0,
        steps_to_diagnosis=sum(
            1 for r in records if r.step == "baseline.single_prompt"),
        time_to_diagnosis_s=diagnosis_time,
        total_time_s=round(time.perf_counter() - t0, 4),
        **_spend_fields(records),
    )
    trace = {
        "baseline": result.model_dump(mode="json"),
        "application": application.model_dump() if application else None,
        "llm_records": [r.model_dump() for r in records],
    }
    return metrics, trace


async def run_benchmark(
    fault_ids: list[str],
    *,
    client: QwenClient,
    world_factory: WorldFactory,
    mode: Literal["mock", "real"],
    ablation: bool = True,
) -> tuple[BenchmarkReport, dict[str, dict]]:
    """Returns (report, traces) where traces maps '<approach>_<fault_id>' to the
    full per-scenario artifact (written to disk by report.write_artifacts)."""
    started_at = utcnow()
    guard = ModelGuard(client)
    scenarios: list[ScenarioMetrics] = []
    ablation_rows: list[AblationScenario] = []
    traces: dict[str, dict] = {}

    with span("benchmark_run", mode=mode, faults=len(fault_ids)):
        for fault_id in fault_ids:
            for approach in ("pipeline", "baseline"):
                world = world_factory(fault_id)
                try:
                    if approach == "pipeline":
                        metrics, trace = await _run_pipeline_scenario(
                            fault_id, world, client)
                    else:
                        metrics, trace = await _run_baseline_scenario(
                            fault_id, world, client)
                finally:
                    world.cleanup()
                scenarios.append(metrics)
                traces[f"{approach}_{fault_id}"] = trace
                guard.check(f"{approach}/{fault_id}")
                log.info("benchmark_scenario_done", step="benchmark",
                         fault_id=fault_id, approach=approach,
                         resolved=metrics.resolved,
                         tokens=metrics.total_tokens)

            if ablation:
                world = world_factory(fault_id)
                try:
                    raw_metrics, raw_trace = await _run_pipeline_scenario(
                        fault_id, world, client, context_mode="raw")
                finally:
                    world.cleanup()
                summarized = next(
                    s for s in scenarios
                    if s.fault_id == fault_id and s.approach == "pipeline"
                    and s.context_mode == "summarized")
                ablation_rows.append(AblationScenario(
                    fault_id=fault_id,
                    tokens_summarized=summarized.total_tokens,
                    tokens_raw=raw_metrics.total_tokens,
                ))
                traces[f"ablation_raw_{fault_id}"] = raw_trace
                guard.check(f"ablation/{fault_id}")

    summary = client.meter.summary()
    cost = CostSummary(
        total_tokens=sum(s.input_tokens + s.output_tokens for s in summary.values()),
        est_cost_usd=round(sum(s.est_cost_usd for s in summary.values()), 6),
        per_model=summary,
        free_tokens_used=sum(s.free_tokens_used for s in summary.values()),
        voucher_tokens_used=sum(s.voucher_tokens_used for s in summary.values()),
    )
    main_rows = [s for s in scenarios if s.context_mode == "summarized"]
    report = BenchmarkReport(
        started_at=started_at,
        finished_at=utcnow(),
        mode=mode,
        models=dict(guard.snapshot),
        model_consistency_ok=True,  # guard.check raised otherwise
        scenarios=scenarios,
        approaches=[
            ApproachSummary.from_scenarios(
                "pipeline",
                [s for s in main_rows if s.approach == "pipeline"]),
            ApproachSummary.from_scenarios(
                "baseline",
                [s for s in main_rows if s.approach == "baseline"]),
        ],
        ablation=AblationSummary.from_scenarios(ablation_rows) if ablation_rows
        else None,
        cost=cost,
    )
    return report, traces
