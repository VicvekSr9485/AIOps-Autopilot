"""Deterministic telemetry summarization (cost rule: bulky logs/metrics are
compacted BEFORE entering any max-tier prompt — never raw-dumped).

No LLM involved: dedup + windowing keeps the summary bounded (~2 KB) however
large the capture is, and costs zero tokens to produce.
"""

from __future__ import annotations

import structlog

from autopilot.models import Telemetry

log = structlog.get_logger("autopilot.pipeline.summarize")

MAX_LOG_GROUPS = 12
MAX_MESSAGE_CHARS = 200


def summarize_telemetry(telemetry: Telemetry, max_log_groups: int = MAX_LOG_GROUPS) -> str:
    alert = telemetry.alert
    lines = [
        f"ALERT: {alert.name} (severity={alert.severity.value}, source={alert.source})",
        f"  {alert.description}" if alert.description else "  (no description)",
    ]

    # Metrics: one windowed line per series (first -> last, delta), never raw points.
    by_name: dict[str, list[float]] = {}
    for point in telemetry.metrics:
        by_name.setdefault(point.name, []).append(point.value)
    if by_name:
        lines.append(f"METRICS ({len(telemetry.metrics)} points, windowed):")
        for name, values in sorted(by_name.items()):
            lines.append(
                f"  {name}: first={values[0]:g} last={values[-1]:g} "
                f"delta={values[-1] - values[0]:+g} samples={len(values)}"
            )
    else:
        lines.append("METRICS: none captured")

    # Logs: deduplicated (service, message) groups with counts, most frequent first.
    groups: dict[tuple[str, str], int] = {}
    for record in telemetry.logs:
        key = (record.service, record.message[:MAX_MESSAGE_CHARS])
        groups[key] = groups.get(key, 0) + 1
    if groups:
        ranked = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))
        shown = ranked[:max_log_groups]
        lines.append(
            f"LOGS ({len(telemetry.logs)} lines -> {len(groups)} distinct, "
            f"top {len(shown)} shown):"
        )
        for (service, message), count in shown:
            lines.append(f"  [{service}] x{count}: {message}")
        if len(ranked) > len(shown):
            lines.append(f"  ... {len(ranked) - len(shown)} more distinct messages omitted")
    else:
        lines.append("LOGS: none captured")

    summary = "\n".join(lines)
    log.info(
        "telemetry_summarized", step="pipeline.summarize",
        logs_in=len(telemetry.logs), metrics_in=len(telemetry.metrics),
        chars_out=len(summary),
    )
    return summary


def render_raw_telemetry(telemetry: Telemetry) -> str:
    """ABLATION ONLY (benchmark context mode B): the un-summarized rendering a
    naive agent would put in context — every log line and metric point verbatim.
    Production stages must keep using summarize_telemetry(); this exists to
    quantify the token saving of the summarization design."""
    alert = telemetry.alert
    lines = [
        f"ALERT: {alert.name} (severity={alert.severity.value}, source={alert.source})",
        f"  {alert.description}" if alert.description else "  (no description)",
        f"METRICS ({len(telemetry.metrics)} raw points):",
    ]
    lines += [
        f"  {p.captured_at.isoformat()} {p.name}={p.value:g}" for p in telemetry.metrics
    ]
    lines.append(f"LOGS ({len(telemetry.logs)} raw lines):")
    lines += [f"  [{r.service}] {r.raw or r.message}" for r in telemetry.logs]
    rendered = "\n".join(lines)
    log.info(
        "telemetry_rendered_raw", step="pipeline.summarize",
        logs_in=len(telemetry.logs), metrics_in=len(telemetry.metrics),
        chars_out=len(rendered), mode="ablation_raw",
    )
    return rendered
