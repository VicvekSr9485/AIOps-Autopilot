"""Benchmark measurement-layer tests (mock mode, offline, no Docker): the full
benchmark runs to completion over all 5 scenarios for BOTH approaches, metrics
compute, the summarization ablation yields two genuinely different token
figures, model consistency is asserted (and a mid-run switch aborts), the
report renders as JSON + markdown + traces, and the cost summary populates
with the free-vs-voucher split and the authoritative-source caveat."""

from __future__ import annotations

import json

import pytest

from autopilot.benchmark.metrics import BenchmarkReport, p95
from autopilot.benchmark.mockenv import HeuristicMockClient, MockWorld
from autopilot.benchmark.report import render_markdown, write_artifacts
from autopilot.benchmark.runner import ModelConsistencyError, run_benchmark
from autopilot.benchmark.scoring import (
    GroundTruthApprover,
    remediation_fixes,
    score_root_cause,
)
from autopilot.harness.synthetic import FAULT_IDS

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def bench():
    """One full mock benchmark over all 5 faults, shared across this module
    (the run is deterministic; recomputing it per test is pure waste)."""
    import asyncio

    client = HeuristicMockClient()
    report, traces = asyncio.run(run_benchmark(
        FAULT_IDS, client=client, world_factory=MockWorld,
        mode="mock", ablation=True,
    ))
    return report, traces, client


# ------------------------------------------------------------- completeness


def test_benchmark_completes_both_approaches_on_all_scenarios(bench):
    report, _, _ = bench
    assert BenchmarkReport.model_validate(report.model_dump())
    main = [s for s in report.scenarios if s.context_mode == "summarized"]
    assert {(s.fault_id, s.approach) for s in main} == {
        (f, a) for f in FAULT_IDS for a in ("pipeline", "baseline")
    }
    assert len(FAULT_IDS) >= 5


def test_metrics_compute_and_are_coherent(bench):
    report, _, _ = bench
    for s in report.scenarios:
        assert s.total_tokens == s.input_tokens + s.output_tokens > 0
        assert s.llm_calls > 0 and s.est_cost_usd > 0
        assert s.steps_to_diagnosis >= 1
        assert s.total_time_s >= s.time_to_diagnosis_s >= 0
        if s.resolved:
            assert s.executed and s.remediation_correct
        if s.false_remediation:
            assert s.executed and not s.resolved
    pipeline, baseline = report.approaches
    assert pipeline.approach == "pipeline" and baseline.approach == "baseline"
    for a in report.approaches:
        assert a.scenarios == len(FAULT_IDS)
        for rate in (a.root_cause_top1_acc, a.root_cause_top3_acc,
                     a.remediation_correct_rate, a.auto_resolution_rate,
                     a.false_remediation_rate, a.escalation_rate,
                     a.schema_failure_rate):
            assert 0.0 <= rate <= 1.0
        assert a.tokens_p95 >= a.tokens_mean > 0


def test_pipeline_resolves_every_fixable_fault(bench):
    """Mock-mode contract: the four faults whose fix lies inside the action
    vocabulary resolve end-to-end through the pipeline, with zero false
    remediations across the board (wrong plans must be caught by the gate or
    rolled back — never left applied)."""
    report, _, _ = bench
    pipe = {s.fault_id: s for s in report.scenarios
            if s.approach == "pipeline" and s.context_mode == "summarized"}
    for fault_id in ("db_pool_exhaustion", "bad_config_rollout",
                     "downstream_timeout", "queue_consumer_stall"):
        assert pipe[fault_id].resolved, fault_id
        assert pipe[fault_id].remediation_correct, fault_id
    assert not any(s.false_remediation for s in pipe.values())


def test_safety_separation_pipeline_vs_baseline(bench):
    """The architecture claim the benchmark exists to demonstrate: on the fault
    whose fix is outside the action vocabulary (expired_credential), the
    pipeline escalates and a ground-truth operator rejects — while the gateless
    baseline acts anyway and records a false remediation."""
    report, _, _ = bench
    by_key = {(s.fault_id, s.approach): s for s in report.scenarios
              if s.context_mode == "summarized"}
    pipe = by_key[("expired_credential", "pipeline")]
    base = by_key[("expired_credential", "baseline")]
    assert pipe.escalated and pipe.human_decision == "reject"
    assert not pipe.executed and not pipe.false_remediation
    assert base.executed and base.false_remediation


# -------------------------------------------------------- model consistency


def test_models_recorded_and_constant(bench):
    report, _, client = bench
    assert report.model_consistency_ok
    assert report.models == {"reasoning": "qwen3.7-max", "default": "qwen3.7-plus"}
    assert {r.model for r in client.meter.records} <= set(report.models.values())


async def test_mid_run_model_switch_aborts():
    client = HeuristicMockClient()

    class SwitchingWorld(MockWorld):
        def cleanup(self):  # fires after the first scenario completes
            client.config.model_by_role["reasoning"] = "qwen2-mini"

    with pytest.raises(ModelConsistencyError, match="tiering changed"):
        await run_benchmark(["db_pool_exhaustion"], client=client,
                            world_factory=SwitchingWorld, mode="mock",
                            ablation=False)


# ------------------------------------------------------------------ ablation


def test_ablation_produces_two_token_figures(bench):
    report, _, _ = bench
    ab = report.ablation
    assert ab is not None and len(ab.scenarios) == len(FAULT_IDS)
    for row in ab.scenarios:
        assert row.tokens_raw > row.tokens_summarized > 0  # summarization saves
        assert 0 < row.saving_pct < 100
    assert ab.mean_tokens_raw > ab.mean_tokens_summarized
    assert ab.mean_saving_pct > 0


# ------------------------------------------------------- report & artifacts


def test_report_renders_and_artifacts_write(bench, tmp_path):
    report, traces, _ = bench
    md = render_markdown(report)
    assert "| Metric | pipeline | baseline |" in md
    assert "Summarization ablation" in md
    assert "qwen3.7-max" in md and "qwen3.7-plus" in md
    for fault_id in FAULT_IDS:
        assert fault_id in md

    written = write_artifacts(report, traces, tmp_path / "out")
    results = json.loads(written["results"].read_text())
    assert BenchmarkReport.model_validate(results)
    assert written["report"].read_text() == md
    trace_files = list((tmp_path / "out" / "traces").glob("*.json"))
    # pipeline + baseline + ablation_raw per fault
    assert len(trace_files) == 3 * len(FAULT_IDS)
    one = json.loads((tmp_path / "out" / "traces" /
                      "pipeline_db_pool_exhaustion.json").read_text())
    assert one["report"]["incident_id"].startswith("inc-")
    assert one["llm_records"]


def test_cost_summary_populates_with_caveat(bench):
    report, _, client = bench
    cost = report.cost
    assert cost.total_tokens > 0 and cost.est_cost_usd > 0
    assert cost.free_tokens_used + cost.voucher_tokens_used == cost.total_tokens
    assert "Qwen Cloud" in cost.caveat and "authoritative" in cost.caveat
    assert set(cost.per_model) == {"qwen3.7-max", "qwen3.7-plus"}
    # run-level numbers reconcile with the single shared meter
    metered = sum(r.input_tokens + r.output_tokens for r in client.meter.records)
    assert cost.total_tokens == metered


# ------------------------------------------------------------ scoring units


def test_score_root_cause_top1_vs_top3():
    causes = ["transient network blip",
              "db connection slots exhausted by idle sessions"]
    top1, top3 = score_root_cause("db_pool_exhaustion", causes)
    assert not top1 and top3
    assert score_root_cause("db_pool_exhaustion", list(reversed(causes))) == (True, True)
    assert score_root_cause("db_pool_exhaustion", ["disk full"]) == (False, False)


def test_remediation_fixes_table():
    assert remediation_fixes("queue_consumer_stall", "restart_service", "worker")
    assert not remediation_fixes("queue_consumer_stall", "restart_service", "db")
    assert remediation_fixes("bad_config_rollout", "rollback", "app")
    assert remediation_fixes("bad_config_rollout", "apply_config", "app",
                             {"feature_mode": "standard"})
    assert not remediation_fixes("bad_config_rollout", "apply_config", "app",
                                 {"feature_mode": "turbo_v2"})
    # the credential fault is unfixable within the action vocabulary BY DESIGN
    assert not any(
        remediation_fixes("expired_credential", action, target)
        for action in ("restart_service", "scale_service", "apply_config", "rollback")
        for target in ("app", "worker", "downstream", "db", "queue")
    )


def test_ground_truth_approver_approves_only_real_fixes():
    from autopilot.models import (
        RemediationProposal,
        RemediationStep,
        RootCauseHypothesis,
    )
    from autopilot.pipeline.hitl import ApprovalRequest

    def request(target: str) -> ApprovalRequest:
        return ApprovalRequest(
            incident_id="inc-1",
            hypothesis=RootCauseHypothesis(incident_id="inc-1", cause="c",
                                           confidence=0.5),
            proposal=RemediationProposal(
                incident_id="inc-1", hypothesis_cause="c",
                steps=[RemediationStep(order=1, action="restart_service",
                                       target=target)],
                risk_score=0.5, blast_radius="single_service"),
            escalation_reasons=["confidence 0.50 < 0.75"],
        )

    approver = GroundTruthApprover("queue_consumer_stall")
    assert approver.decide(request("worker")).action == "approve"
    assert approver.decide(request("db")).action == "reject"
    assert approver.decisions == ["approve", "reject"]


def test_p95_helper():
    assert p95([]) == 0.0
    assert p95([5.0]) == 5.0
    assert p95(list(map(float, range(1, 101)))) == 95.0
