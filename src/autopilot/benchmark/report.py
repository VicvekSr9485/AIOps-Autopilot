"""Benchmark report rendering: machine-readable results.json, human-readable
report.md, and per-scenario trace artifacts under traces/."""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from autopilot.benchmark.metrics import ApproachSummary, BenchmarkReport

log = structlog.get_logger("autopilot.benchmark.report")


def _pct(v: float) -> str:
    return f"{100 * v:.0f}%"


def _yn(v: bool) -> str:
    return "yes" if v else "no"


def _approach_table(approaches: list[ApproachSummary]) -> list[str]:
    rows = [
        ("Scenarios", lambda a: str(a.scenarios)),
        ("Root-cause top-1 accuracy", lambda a: _pct(a.root_cause_top1_acc)),
        ("Root-cause top-3 accuracy", lambda a: _pct(a.root_cause_top3_acc)),
        ("Remediation correct (sandbox-verified)",
         lambda a: _pct(a.remediation_correct_rate)),
        ("Auto-resolution rate", lambda a: _pct(a.auto_resolution_rate)),
        ("False-remediation rate", lambda a: _pct(a.false_remediation_rate)),
        ("Escalation rate", lambda a: _pct(a.escalation_rate)),
        ("Schema-failure rate", lambda a: _pct(a.schema_failure_rate)),
        ("Invalid tool calls", lambda a: str(a.invalid_tool_calls)),
        ("Tokens / incident (mean)", lambda a: f"{a.tokens_mean:.0f}"),
        ("Tokens / incident (p95)", lambda a: f"{a.tokens_p95:.0f}"),
        ("Total tokens", lambda a: str(a.total_tokens)),
        ("Est. cost (USD)", lambda a: f"${a.est_cost_usd:.4f}"),
        ("LLM calls to diagnosis (mean)",
         lambda a: f"{a.mean_steps_to_diagnosis:.1f}"),
        ("Time to diagnosis (mean s)",
         lambda a: f"{a.mean_time_to_diagnosis_s:.3f}"),
    ]
    header = "| Metric | " + " | ".join(a.approach for a in approaches) + " |"
    sep = "|---" * (len(approaches) + 1) + "|"
    lines = [header, sep]
    for label, fmt in rows:
        lines.append(f"| {label} | " + " | ".join(fmt(a) for a in approaches) + " |")
    return lines


def render_markdown(report: BenchmarkReport) -> str:
    lines = [
        "# Benchmark report — agent pipeline vs single-prompt baseline",
        "",
        f"- Run: {report.started_at.isoformat()} → {report.finished_at.isoformat()}"
        f" ({report.mode} mode)",
        "- Models (constant for the whole run, asserted): "
        + ", ".join(f"`{role}` → `{model}`"
                    for role, model in sorted(report.models.items())),
        f"- Model consistency check: "
        f"{'PASSED' if report.model_consistency_ok else 'FAILED'}",
        "",
        "## Approach comparison",
        "",
        *_approach_table(report.approaches),
        "",
        "## Per-scenario results",
        "",
        "| Fault | Approach | RC top-1 | RC top-3 | Remediation | Resolved |"
        " Escalated | Rolled back | Tokens | Est. USD |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in report.scenarios:
        if s.context_mode != "summarized":
            continue  # ablation runs get their own section
        if s.remediation_correct:
            remediation = "correct"
        elif s.false_remediation:
            remediation = "WRONG"
        else:
            remediation = "not executed"
        lines.append(
            f"| {s.fault_id} | {s.approach} | {_yn(s.root_cause_top1)} "
            f"| {_yn(s.root_cause_top3)} | {remediation} "
            f"| {_yn(s.resolved)} | {_yn(s.escalated)} | {_yn(s.rolled_back)} "
            f"| {s.total_tokens} | ${s.est_cost_usd:.4f} |"
        )

    if report.ablation:
        ab = report.ablation
        lines += [
            "",
            "## Summarization ablation (pipeline tokens per incident)",
            "",
            "Context mode A = tool/telemetry outputs summarized before entering"
            " the prompt (production default); mode B = raw outputs in context.",
            "",
            "| Fault | A: summarized | B: raw | Saving |",
            "|---|---|---|---|",
            *(f"| {r.fault_id} | {r.tokens_summarized} | {r.tokens_raw} "
              f"| {r.saving_pct:.1f}% |" for r in ab.scenarios),
            f"| **mean** | **{ab.mean_tokens_summarized:.0f}** "
            f"| **{ab.mean_tokens_raw:.0f}** | **{ab.mean_saving_pct:.1f}%** |",
        ]

    cost = report.cost
    lines += [
        "",
        "## Run-level cost (local estimate)",
        "",
        f"- Total tokens: {cost.total_tokens}",
        f"- Estimated cost: ${cost.est_cost_usd:.4f}",
        f"- Free-tier vs voucher split: {cost.free_tokens_used} free / "
        f"{cost.voucher_tokens_used} voucher tokens",
    ]
    for model, s in sorted(cost.per_model.items()):
        lines.append(
            f"  - {model}: {s.calls} calls, in={s.input_tokens} "
            f"out={s.output_tokens}, est ${s.est_cost_usd:.4f}, "
            f"free={s.free_tokens_used} voucher={s.voucher_tokens_used}"
        )
    lines += ["", f"> {cost.caveat}", ""]
    return "\n".join(lines)


def write_artifacts(
    report: BenchmarkReport, traces: dict[str, dict], out_dir: Path
) -> dict[str, Path]:
    """Write results.json + report.md + traces/<key>.json; returns the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = out_dir / "traces"
    traces_dir.mkdir(exist_ok=True)

    results_path = out_dir / "results.json"
    results_path.write_text(report.model_dump_json(indent=2) + "\n")
    md_path = out_dir / "report.md"
    md_path.write_text(render_markdown(report))
    written = {"results": results_path, "report": md_path}
    for key, trace in traces.items():
        path = traces_dir / f"{key}.json"
        path.write_text(json.dumps(trace, indent=2, default=str) + "\n")
        written[f"trace:{key}"] = path

    log.info("benchmark_artifacts_written", step="benchmark.report",
             out_dir=str(out_dir), files=len(written))
    return written
