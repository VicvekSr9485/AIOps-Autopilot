"""Benchmark metric types: per-scenario records, per-approach aggregates, the
summarization-ablation summary, and the run-level report. Definitions live in
.claude/rules/benchmark.md and must stay in sync with this module."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from autopilot.llm.metering import ModelSummary

Approach = Literal["pipeline", "baseline"]

COST_CAVEAT = (
    "All token/cost figures are LOCAL estimates from CostMeter; the "
    "authoritative usage and billing numbers are the Qwen Cloud "
    "Analytics/Usage page."
)


class ScenarioMetrics(BaseModel):
    """Everything measured for one (fault, approach) run."""

    fault_id: str
    approach: Approach
    context_mode: Literal["summarized", "raw"] = "summarized"

    # quality
    root_cause_top1: bool = False
    root_cause_top3: bool = False
    remediation_correct: bool = False  # applied AND health verified restored
    executed: bool = False
    resolved: bool = False
    auto_resolved: bool = False  # resolved with no human in the loop
    false_remediation: bool = False  # acted on the sandbox but did NOT restore health
    escalated: bool = False
    human_decision: str | None = None
    rolled_back: bool = False

    # robustness
    schema_retries: int = 0  # extra LLM attempts beyond the first, per stage call
    schema_failed: bool = False  # a stage exhausted its structured-output caps
    invalid_tool_calls: int = 0  # tool invocations rejected at the tool layer

    # cost & latency
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    est_cost_usd: float = 0.0
    steps_to_diagnosis: int = 0  # LLM calls until a ranked diagnosis existed
    time_to_diagnosis_s: float = 0.0
    total_time_s: float = 0.0


def _rate(hits: int, n: int) -> float:
    return round(hits / n, 4) if n else 0.0


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


class ApproachSummary(BaseModel):
    approach: Approach
    scenarios: int
    root_cause_top1_acc: float
    root_cause_top3_acc: float
    remediation_correct_rate: float
    auto_resolution_rate: float
    false_remediation_rate: float
    escalation_rate: float
    schema_failure_rate: float
    invalid_tool_calls: int
    tokens_mean: float
    tokens_p95: float
    total_tokens: int
    est_cost_usd: float
    mean_steps_to_diagnosis: float
    mean_time_to_diagnosis_s: float

    @classmethod
    def from_scenarios(cls, approach: Approach,
                       rows: list[ScenarioMetrics]) -> ApproachSummary:
        n = len(rows)
        tokens = [float(r.total_tokens) for r in rows]
        return cls(
            approach=approach,
            scenarios=n,
            root_cause_top1_acc=_rate(sum(r.root_cause_top1 for r in rows), n),
            root_cause_top3_acc=_rate(sum(r.root_cause_top3 for r in rows), n),
            remediation_correct_rate=_rate(sum(r.remediation_correct for r in rows), n),
            auto_resolution_rate=_rate(sum(r.auto_resolved for r in rows), n),
            false_remediation_rate=_rate(sum(r.false_remediation for r in rows), n),
            escalation_rate=_rate(sum(r.escalated for r in rows), n),
            schema_failure_rate=_rate(sum(r.schema_failed for r in rows), n),
            invalid_tool_calls=sum(r.invalid_tool_calls for r in rows),
            tokens_mean=round(sum(tokens) / n, 1) if n else 0.0,
            tokens_p95=p95(tokens),
            total_tokens=sum(r.total_tokens for r in rows),
            est_cost_usd=round(sum(r.est_cost_usd for r in rows), 6),
            mean_steps_to_diagnosis=(
                round(sum(r.steps_to_diagnosis for r in rows) / n, 2) if n else 0.0),
            mean_time_to_diagnosis_s=(
                round(sum(r.time_to_diagnosis_s for r in rows) / n, 4) if n else 0.0),
        )


class AblationScenario(BaseModel):
    fault_id: str
    tokens_summarized: int
    tokens_raw: int

    @property
    def saving_pct(self) -> float:
        if self.tokens_raw == 0:
            return 0.0
        return round(100 * (1 - self.tokens_summarized / self.tokens_raw), 1)


class AblationSummary(BaseModel):
    """Summarized (mode A, production default) vs raw (mode B) tool/telemetry
    context: pipeline tokens per incident under each mode."""

    scenarios: list[AblationScenario]
    mean_tokens_summarized: float
    mean_tokens_raw: float
    mean_saving_pct: float

    @classmethod
    def from_scenarios(cls, rows: list[AblationScenario]) -> AblationSummary:
        n = len(rows)
        mean_a = sum(r.tokens_summarized for r in rows) / n if n else 0.0
        mean_b = sum(r.tokens_raw for r in rows) / n if n else 0.0
        return cls(
            scenarios=rows,
            mean_tokens_summarized=round(mean_a, 1),
            mean_tokens_raw=round(mean_b, 1),
            mean_saving_pct=round(100 * (1 - mean_a / mean_b), 1) if mean_b else 0.0,
        )


class CostSummary(BaseModel):
    total_tokens: int
    est_cost_usd: float
    per_model: dict[str, ModelSummary]
    free_tokens_used: int
    voucher_tokens_used: int
    caveat: str = COST_CAVEAT


class BenchmarkReport(BaseModel):
    started_at: datetime
    finished_at: datetime
    mode: Literal["mock", "real"]
    models: dict[str, str]  # exact role->model strings, asserted constant all run
    model_consistency_ok: bool
    scenarios: list[ScenarioMetrics]
    approaches: list[ApproachSummary]
    ablation: AblationSummary | None = None
    cost: CostSummary
    notes: list[str] = Field(default_factory=list)
