"""Telemetry MCP server: read-only window onto the sandbox stack.

Tools only observe (compose logs, /healthz, /work, /metrics probes) and ALWAYS
summarize before returning: logs come back as deduplicated message groups with
counts (hard-capped), metrics as first/last/delta windows — never thousands of
raw lines into a model prompt.

NOTE: no `from __future__ import annotations` here — FastMCP 1.9.4 inspects real
(non-string) annotations when registering tools.
"""

import json
import time
from datetime import UTC, datetime, timedelta

import structlog
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from autopilot.ingestion.normalize import parse_compose_logs, synthesize_alert
from autopilot.mcp_servers.guards import SandboxViolation, ensure_sandbox_service, truncate
from autopilot.models import AlertEvent
from autopilot.sandbox.controller import ProbeSnapshot, SandboxController

log = structlog.get_logger("autopilot.mcp.telemetry")

MAX_LOG_GROUPS = 200  # hard ceiling on returned log groups, whatever the caller asks
METRIC_NAMES = ("requests_total", "errors_total", "work_success_total",
                "queue_depth", "jobs_processed")
TRACEABLE_PATHS = ("/work", "/healthz", "/metrics")


class LogGroup(BaseModel):
    service: str
    message: str  # truncated, deduplicated message text
    count: int = Field(ge=1)
    last_seen: datetime | None = None


class LogQueryResult(BaseModel):
    window_minutes: int
    total_lines: int  # parsed lines in the window before filtering
    matched: int  # lines after service/contains filters
    groups_returned: int
    truncated: bool  # True if matched lines were collapsed/capped
    per_service: dict[str, int]
    groups: list[LogGroup]


class MetricSeries(BaseModel):
    name: str
    first: float
    last: float
    delta: float
    min: float
    max: float


class MetricsQueryResult(BaseModel):
    samples: int
    interval_s: float
    series: list[MetricSeries]
    unavailable: list[str]  # requested metrics with no data points


class ActiveAlertsResult(BaseModel):
    probes: int
    failing_probes: int
    alerts: list[AlertEvent]  # empty when every probe is healthy


class TraceEvent(BaseModel):
    service: str
    message: str
    timestamp: datetime | None = None


class TraceResult(BaseModel):
    path: str
    status: int | None
    latency_ms: float
    ok: bool
    body_excerpt: str
    error: str | None = None
    events: list[TraceEvent]  # correlated stack log lines emitted during the request


def _probe_series(ctrl: SandboxController, samples: int, interval_s: float
                  ) -> list[ProbeSnapshot]:
    samples = max(2, min(samples, 10))
    interval_s = max(0.0, min(interval_s, 5.0))
    snapshots = []
    for i in range(samples):
        snapshots.append(ctrl.probe())
        if i < samples - 1 and interval_s:
            time.sleep(interval_s)
    return snapshots


def build_telemetry_server(ctrl: SandboxController | None = None) -> FastMCP:
    ctrl = ctrl or SandboxController()
    mcp = FastMCP(
        "autopilot-telemetry",
        instructions="Read-only telemetry for the autopilot sandbox stack. "
        "All outputs are pre-summarized; there is no raw-dump mode.",
    )

    @mcp.tool()
    def query_logs(service: str | None = None, contains: str | None = None,
                   since_minutes: int = 15, limit: int = 50) -> LogQueryResult:
        """Search recent sandbox logs. Returns deduplicated (service, message) groups
        with occurrence counts, most frequent first — never raw line dumps. Optional
        filters: sandbox service name, case-insensitive substring."""
        if service is not None:
            service = ensure_sandbox_service(service)
        since_minutes = max(1, min(since_minutes, 240))
        limit = max(1, min(limit, MAX_LOG_GROUPS))

        since = datetime.now(UTC) - timedelta(minutes=since_minutes)
        records = parse_compose_logs(ctrl.logs(since=since))
        total_lines = len(records)
        if service:
            records = [r for r in records if r.service == service]
        if contains:
            needle = contains.lower()
            records = [r for r in records if needle in r.message.lower()]

        per_service: dict[str, int] = {}
        grouped: dict[tuple[str, str], LogGroup] = {}
        for r in records:
            per_service[r.service] = per_service.get(r.service, 0) + 1
            key = (r.service, truncate(r.message, 300))
            group = grouped.get(key)
            if group is None:
                grouped[key] = LogGroup(service=key[0], message=key[1], count=1,
                                        last_seen=r.timestamp)
            else:
                group.count += 1
                if r.timestamp and (group.last_seen is None or r.timestamp > group.last_seen):
                    group.last_seen = r.timestamp
        groups = sorted(grouped.values(), key=lambda g: (-g.count, g.service, g.message))
        result = LogQueryResult(
            window_minutes=since_minutes,
            total_lines=total_lines,
            matched=len(records),
            groups_returned=len(groups[:limit]),
            truncated=len(records) > len(groups[:limit]),
            per_service=per_service,
            groups=groups[:limit],
        )
        log.info("mcp_tool", step="mcp.telemetry", tool="query_logs",
                 matched=result.matched, returned=result.groups_returned)
        return result

    @mcp.tool()
    def query_metrics(names: list[str] | None = None, samples: int = 3,
                      interval_s: float = 1.0) -> MetricsQueryResult:
        """Sample sandbox app metrics over a short window and summarize each series
        as first/last/delta/min/max. Known metrics: requests_total, errors_total,
        work_success_total, queue_depth, jobs_processed."""
        requested = names or list(METRIC_NAMES)
        snapshots = _probe_series(ctrl, samples, interval_s)
        series, unavailable = [], []
        for name in requested:
            values = [float(s.metrics[name]) for s in snapshots
                      if s.metrics and s.metrics.get(name) is not None]
            if not values:
                unavailable.append(name)
                continue
            series.append(MetricSeries(name=name, first=values[0], last=values[-1],
                                       delta=values[-1] - values[0],
                                       min=min(values), max=max(values)))
        log.info("mcp_tool", step="mcp.telemetry", tool="query_metrics",
                 series=len(series), unavailable=len(unavailable))
        return MetricsQueryResult(samples=len(snapshots), interval_s=interval_s,
                                  series=series, unavailable=unavailable)

    @mcp.tool()
    def get_active_alerts(samples: int = 3, interval_s: float = 1.0) -> ActiveAlertsResult:
        """Probe the sandbox stack and return currently-firing alerts synthesized
        from observed health/work signals. Empty list = all probes healthy."""
        snapshots = _probe_series(ctrl, samples, interval_s)
        failing = sum(1 for s in snapshots if not s.healthy)
        alerts = [] if failing == 0 else [synthesize_alert(snapshots)]
        log.info("mcp_tool", step="mcp.telemetry", tool="get_active_alerts",
                 failing=failing, alerts=len(alerts))
        return ActiveAlertsResult(probes=len(snapshots), failing_probes=failing,
                                  alerts=alerts)

    @mcp.tool()
    def get_trace(path: str = "/work") -> TraceResult:
        """Trace one request against the sandbox app: timed status/latency plus the
        stack log lines emitted while it ran. Only sandbox app endpoints are allowed."""
        if path not in TRACEABLE_PATHS:
            raise SandboxViolation(
                f"refusing to trace {path!r}: only sandbox app endpoints "
                f"{list(TRACEABLE_PATHS)} are traceable"
            )
        obs = ctrl.timed_request(path)
        events = [
            TraceEvent(service=r.service, message=truncate(r.message, 300),
                       timestamp=r.timestamp)
            for r in parse_compose_logs(ctrl.logs(since=obs.started_at))[:20]
        ]
        body = obs.body if isinstance(obs.body, str) else json.dumps(obs.body)
        result = TraceResult(path=path, status=obs.status, latency_ms=obs.latency_ms,
                             ok=obs.status == 200, body_excerpt=truncate(body or "", 500),
                             error=obs.error, events=events)
        log.info("mcp_tool", step="mcp.telemetry", tool="get_trace",
                 path=path, status=obs.status, latency_ms=round(obs.latency_ms, 1))
        return result

    return mcp
