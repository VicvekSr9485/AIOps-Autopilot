"""MCP surface tests (offline, mock mode, no Docker): every tool is callable over
an in-memory MCP session, output schemas validate, mutating tools honor dry_run
(default true), and the sandbox-only guard refuses out-of-scope targets."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from autopilot.mcp_servers.guards import SANDBOX_SERVICES
from autopilot.mcp_servers.infra import HealthCheckResult, OpResult, build_infra_server
from autopilot.mcp_servers.knowledge import (
    RecordOutcomeResult,
    SearchIncidentsResult,
    SearchRunbooksResult,
    build_knowledge_server,
)
from autopilot.mcp_servers.store import KnowledgeStore
from autopilot.mcp_servers.telemetry import (
    ActiveAlertsResult,
    LogQueryResult,
    MetricsQueryResult,
    TraceResult,
    build_telemetry_server,
)
from autopilot.sandbox.controller import (
    ProbeSnapshot,
    RequestObservation,
    SandboxController,
)

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------- fakes


class FakeController(SandboxController):
    """Duck-typed stand-in: canned observations, recorded mutations, no Docker."""

    def __init__(self, snapshots=None, log_text=""):
        super().__init__()
        self.calls: list[tuple] = []
        self._snapshots = list(snapshots or [healthy_snap()])
        self._log_text = log_text
        self._config = self.default_app_config()

    def _compose(self, *args, check=True):
        raise AssertionError("docker compose must never run in unit tests")

    def logs(self, since=None):
        return self._log_text

    def probe(self):
        return self._snapshots.pop(0) if len(self._snapshots) > 1 else self._snapshots[0]

    def restart(self, service):
        self.calls.append(("restart", service))

    def scale(self, service, replicas):
        self.calls.append(("scale", service, replicas))

    def read_app_config(self):
        return dict(self._config)

    def write_app_config(self, config):
        self._config = dict(config)
        self.calls.append(("write_app_config", json.dumps(config, sort_keys=True)))

    def timed_request(self, path):
        return RequestObservation(path=path, started_at=NOW, status=200,
                                  latency_ms=12.5, body={"status": "done"})


def healthy_snap(**metrics) -> ProbeSnapshot:
    return ProbeSnapshot(
        captured_at=NOW,
        healthz_status=200,
        healthz_body={"status": "ok", "components": {"db": {"ok": True}, "queue": {"ok": True}}},
        work_status=200,
        work_body={"status": "done"},
        metrics={"requests_total": 1, "errors_total": 0, "work_success_total": 1,
                 "queue_depth": 0, "jobs_processed": 0, **metrics},
    )


def db_down_snap() -> ProbeSnapshot:
    return ProbeSnapshot(
        captured_at=NOW,
        healthz_status=503,
        healthz_body={"status": "degraded",
                      "components": {"db": {"ok": False, "error": "too many clients"},
                                     "queue": {"ok": True}}},
        work_status=500,
        work_body={"error": "db error: too many clients"},
        metrics=None,
    )


def compose_log_lines(n: int, message: str, service: str = "app") -> str:
    ts = "2026-06-12T12:00:00.000000000Z"
    return "\n".join(f"autopilot-sbx-{service}  | {ts} {message}" for _ in range(n))


async def call(server, tool: str, args: dict | None = None):
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        return await client.call_tool(tool, args or {})


def payload(result) -> dict:
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


def error_text(result) -> str:
    assert result.isError
    return result.content[0].text


async def tool_schemas(server) -> dict[str, dict]:
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        listed = await client.list_tools()
    return {t.name: t.inputSchema for t in listed.tools}


# ----------------------------------------------------------------- telemetry


async def test_telemetry_tools_listed():
    schemas = await tool_schemas(build_telemetry_server(FakeController()))
    assert set(schemas) == {"query_logs", "query_metrics", "get_active_alerts", "get_trace"}


async def test_query_logs_summarizes_never_dumps():
    text = (compose_log_lines(2990, "work_failed reason=db_error")
            + "\n" + compose_log_lines(10, "healthz_component_failed component=db"))
    ctrl = FakeController(log_text=text)
    data = payload(await call(build_telemetry_server(ctrl), "query_logs",
                              {"limit": 50}))
    result = LogQueryResult.model_validate(data)
    assert result.total_lines == result.matched == 3000
    assert result.groups_returned <= 50  # 3000 lines collapse to grouped counts
    assert result.truncated
    assert result.groups[0].count == 2990
    assert result.per_service == {"app": 3000}


async def test_query_logs_filters_and_guards():
    text = (compose_log_lines(5, "work_failed reason=db_error")
            + "\n" + compose_log_lines(3, "job_done", service="worker"))
    server = build_telemetry_server(FakeController(log_text=text))
    filtered = LogQueryResult.model_validate(
        payload(await call(server, "query_logs",
                           {"service": "worker", "contains": "job"})))
    assert filtered.matched == 3
    assert all(g.service == "worker" for g in filtered.groups)
    assert "refusing" in error_text(await call(server, "query_logs", {"service": "nginx"}))


async def test_query_metrics_windows():
    snaps = [healthy_snap(queue_depth=1), healthy_snap(queue_depth=5),
             healthy_snap(queue_depth=9)]
    server = build_telemetry_server(FakeController(snapshots=snaps))
    data = payload(await call(server, "query_metrics",
                              {"names": ["queue_depth", "bogus_metric"],
                               "samples": 3, "interval_s": 0}))
    result = MetricsQueryResult.model_validate(data)
    (series,) = result.series
    assert (series.first, series.last, series.delta) == (1, 9, 8)
    assert result.unavailable == ["bogus_metric"]


async def test_get_active_alerts_fires_only_when_unhealthy():
    degraded = build_telemetry_server(
        FakeController(snapshots=[db_down_snap(), db_down_snap()]))
    data = ActiveAlertsResult.model_validate(
        payload(await call(degraded, "get_active_alerts", {"interval_s": 0})))
    assert data.failing_probes == data.probes
    assert [a.name for a in data.alerts] == ["sandbox.app.health_degraded"]

    healthy = build_telemetry_server(FakeController())
    data = ActiveAlertsResult.model_validate(
        payload(await call(healthy, "get_active_alerts", {"interval_s": 0})))
    assert data.alerts == [] and data.failing_probes == 0


async def test_get_trace_and_path_guard():
    ctrl = FakeController(log_text=compose_log_lines(2, "work_done items=3"))
    server = build_telemetry_server(ctrl)
    result = TraceResult.model_validate(payload(await call(server, "get_trace", {})))
    assert result.ok and result.status == 200 and result.path == "/work"
    assert len(result.events) == 2
    assert "refusing" in error_text(await call(server, "get_trace", {"path": "/admin"}))


# --------------------------------------------------------------------- infra


async def test_infra_tools_listed_with_dry_run_default_true():
    schemas = await tool_schemas(build_infra_server(FakeController()))
    assert set(schemas) == {"restart_service", "scale_service", "apply_config",
                            "rollback", "health_check"}
    for tool in ("restart_service", "scale_service", "apply_config", "rollback"):
        assert schemas[tool]["properties"]["dry_run"]["default"] is True


async def test_mutating_tools_honor_dry_run_default():
    ctrl = FakeController()
    server = build_infra_server(ctrl)
    for tool, args in [("restart_service", {"service": "app"}),
                       ("scale_service", {"service": "worker", "replicas": 1}),
                       ("apply_config", {"patch": {"feature_mode": "turbo_v2"}}),
                       ("rollback", {})]:
        result = OpResult.model_validate(payload(await call(server, tool, args)))
        assert result.dry_run and not result.executed and result.success
    assert ctrl.calls == []  # nothing ever touched the (fake) stack


async def test_restart_and_scale_execute_when_opted_in():
    ctrl = FakeController()
    server = build_infra_server(ctrl)
    restart = OpResult.model_validate(payload(await call(
        server, "restart_service", {"service": "downstream", "dry_run": False})))
    scale = OpResult.model_validate(payload(await call(
        server, "scale_service", {"service": "worker", "replicas": 1, "dry_run": False})))
    assert restart.executed and scale.executed
    assert ctrl.calls == [("restart", "downstream"), ("scale", "worker", 1)]


async def test_apply_config_and_rollback_are_idempotent():
    ctrl = FakeController()
    server = build_infra_server(ctrl)
    patch = {"patch": {"downstream_timeout_s": 3.0}, "dry_run": False}  # default is 1.5

    first = OpResult.model_validate(payload(await call(server, "apply_config", patch)))
    assert first.changed and first.executed
    assert ("restart", "app") in ctrl.calls

    again = OpResult.model_validate(payload(await call(server, "apply_config", patch)))
    assert not again.changed and not again.executed and again.success  # no-op
    assert len([c for c in ctrl.calls if c[0] == "restart"]) == 1

    rolled = OpResult.model_validate(
        payload(await call(server, "rollback", {"dry_run": False})))
    assert rolled.changed and ctrl.read_app_config() == ctrl.default_app_config()
    rolled_again = OpResult.model_validate(
        payload(await call(server, "rollback", {"dry_run": False})))
    assert not rolled_again.changed and rolled_again.success


async def test_apply_config_rejects_unknown_keys():
    server = build_infra_server(FakeController())
    result = await call(server, "apply_config",
                        {"patch": {"max_connections": 100}, "dry_run": False})
    assert result.isError


async def test_sandbox_only_guard_refuses_foreign_targets():
    ctrl = FakeController()
    server = build_infra_server(ctrl)
    for tool, args in [("restart_service", {"service": "host-nginx", "dry_run": False}),
                       ("scale_service", {"service": "db; rm -rf /", "replicas": 0,
                                          "dry_run": False})]:
        text = error_text(await call(server, tool, args))
        assert "refusing" in text and "autopilot-sandbox" in text
    assert ctrl.calls == []


async def test_health_check_reads_components():
    server = build_infra_server(FakeController(snapshots=[db_down_snap()]))
    result = HealthCheckResult.model_validate(payload(await call(server, "health_check")))
    assert not result.healthy
    assert result.components == {"db": False, "queue": True}


def test_service_allowlist_matches_compose_file():
    compose = (REPO_ROOT / "sandbox" / "docker-compose.yml").read_text()
    services_block = compose.split("\nservices:\n", 1)[1]
    declared = set(re.findall(r"^  ([a-z]+):\s*$", services_block, re.M))
    assert declared == set(SANDBOX_SERVICES)


# ----------------------------------------------------------------- knowledge


@pytest.fixture
def knowledge():
    store = KnowledgeStore(":memory:")
    return store, build_knowledge_server(store=store)


async def test_knowledge_tools_listed(knowledge):
    _, server = knowledge
    schemas = await tool_schemas(server)
    assert set(schemas) == {"search_runbooks", "search_past_incidents", "record_outcome"}


async def test_search_runbooks_ranks_relevant_first(knowledge):
    _, server = knowledge
    data = SearchRunbooksResult.model_validate(payload(await call(
        server, "search_runbooks",
        {"query": "postgres reports too many clients, connection slots exhausted"})))
    assert data.results[0].slug == "postgres-connection-exhaustion"
    assert data.results[0].score > data.results[1].score

    data = SearchRunbooksResult.model_validate(payload(await call(
        server, "search_runbooks",
        {"query": "queue_depth growing while jobs_processed counter stalled"})))
    assert data.results[0].slug == "queue-consumer-stall"


async def test_record_outcome_upserts_and_is_searchable(knowledge):
    store, server = knowledge
    args = {"incident_id": "inc-abc123", "summary": "worker stalled, queue backlog",
            "root_cause": "queue consumer paused", "remediation": "restart worker",
            "resolved": True}
    first = RecordOutcomeResult.model_validate(
        payload(await call(server, "record_outcome", args)))
    assert first.created
    second = RecordOutcomeResult.model_validate(
        payload(await call(server, "record_outcome", {**args, "notes": "verified"})))
    assert not second.created and second.doc_id == first.doc_id
    assert store.count("incident") == 1  # idempotent upsert, no duplicates

    found = SearchIncidentsResult.model_validate(payload(await call(
        server, "search_past_incidents", {"query": "queue backlog worker stalled"})))
    assert found.results[0].incident_id == "inc-abc123"
    assert "restart worker" in found.results[0].excerpt
