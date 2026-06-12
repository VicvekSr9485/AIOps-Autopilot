# MCP tool surface

The agent reaches the world exclusively through three MCP servers (official MCP
Python SDK, `FastMCP`). Each runs over stdio:

```bash
make mcp-telemetry    # read-only observation of the sandbox stack
make mcp-infra        # mutating ops — sandbox-only, dry-run by default
make mcp-knowledge    # runbook / past-incident retrieval + outcome recording
```

Guardrails common to the whole surface:

- **Sandbox-only.** Service-targeting tools validate against the compose-service
  allowlist (`app`, `worker`, `downstream`, `db`, `queue`) and refuse anything
  else. The allowlist is test-enforced to match `sandbox/docker-compose.yml`.
- **Dry-run by default.** Every mutating tool takes `dry_run` defaulting to
  `true`; callers must explicitly opt in to act.
- **Idempotent mutations.** Mutating tools converge to a declared target state;
  config tools no-op (`changed=false`) when the state already matches.
- **Summarized outputs.** Telemetry never returns raw dumps: logs come back as
  deduplicated message groups (hard cap 200), metrics as windowed summaries.

All results are JSON-serialized Pydantic models; field types below use Python
notation. Errors (guard refusals, invalid input) surface as MCP tool errors
(`isError=true`) with a `refusing to …` message.

## Telemetry server — `autopilot-telemetry`

### `query_logs`

Search recent sandbox logs, returned as deduplicated `(service, message)` groups
with counts, most frequent first.

| Input | Type | Default | Notes |
|---|---|---|---|
| `service` | `str \| None` | `None` | must be a sandbox service if given |
| `contains` | `str \| None` | `None` | case-insensitive substring filter |
| `since_minutes` | `int` | `15` | clamped to 1–240 |
| `limit` | `int` | `50` | clamped to 1–200 (hard cap) |

Output `LogQueryResult`: `window_minutes: int`, `total_lines: int`,
`matched: int`, `groups_returned: int`, `truncated: bool`,
`per_service: dict[str, int]`, `groups: list[{service, message, count, last_seen}]`.

### `query_metrics`

Sample app metrics over a short window; each series summarized, never raw points.

| Input | Type | Default | Notes |
|---|---|---|---|
| `names` | `list[str] \| None` | `None` | default: all known metrics |
| `samples` | `int` | `3` | clamped to 2–10 |
| `interval_s` | `float` | `1.0` | clamped to 0–5 |

Known metrics: `requests_total`, `errors_total`, `work_success_total`,
`queue_depth`, `jobs_processed`.

Output `MetricsQueryResult`: `samples: int`, `interval_s: float`,
`series: list[{name, first, last, delta, min, max}]`, `unavailable: list[str]`.

### `get_active_alerts`

Probe the stack and synthesize alerts from observed health/work signals
(same path as ingestion — no ground truth involved).

| Input | Type | Default |
|---|---|---|
| `samples` | `int` | `3` |
| `interval_s` | `float` | `1.0` |

Output `ActiveAlertsResult`: `probes: int`, `failing_probes: int`,
`alerts: list[AlertEvent]` (empty when every probe is healthy).

### `get_trace`

Trace one request against the sandbox app: timed status/latency plus stack log
lines emitted while it ran (capped at 20 events).

| Input | Type | Default | Notes |
|---|---|---|---|
| `path` | `str` | `"/work"` | only `/work`, `/healthz`, `/metrics` |

Output `TraceResult`: `path: str`, `status: int | None`, `latency_ms: float`,
`ok: bool`, `body_excerpt: str` (≤500 chars), `error: str | None`,
`events: list[{service, message, timestamp}]`.

## Infra/Ops server — `autopilot-infra`

All mutating tools share the output shape `OpResult`: `tool: str`,
`target: str`, `dry_run: bool`, `changed: bool`, `executed: bool`,
`success: bool`, `detail: str`. Under `dry_run`, `executed` is always `false`
and `detail` describes exactly what would happen.

### `restart_service`

| Input | Type | Default | Notes |
|---|---|---|---|
| `service` | `str` | — | sandbox services only |
| `dry_run` | `bool` | `true` | |

### `scale_service`

| Input | Type | Default | Notes |
|---|---|---|---|
| `service` | `str` | — | sandbox services only |
| `replicas` | `int` | — | 0–3; 0 stops the service |
| `dry_run` | `bool` | `true` | |

Sandbox services pin `container_name`, so compose rejects `replicas > 1`; that
surfaces as `success=false` with the compose error in `detail`.

### `apply_config`

Apply a partial app-config change, then restart `app` so config is re-read.
No-ops when the active config already matches the patch.

| Input | Type | Default | Notes |
|---|---|---|---|
| `patch` | `AppConfigPatch` | — | `feature_mode: str`, `downstream_url: str`, `downstream_timeout_s: float` (all optional; unknown keys rejected) |
| `dry_run` | `bool` | `true` | |

### `rollback`

Restore the canonical (last known-good) app config and restart `app`. No-ops
when already canonical.

| Input | Type | Default |
|---|---|---|
| `dry_run` | `bool` | `true` |

### `health_check` (read-only)

No inputs. Output `HealthCheckResult`: `healthy: bool`,
`healthz_status: int | None`, `work_status: int | None`,
`components: dict[str, bool]`, `captured_at: datetime`.

## Knowledge server — `autopilot-knowledge`

Backed by the local SQLite vector store (`data/knowledge.db`; deterministic
hashing embeddings, sqlite-vec KNN when the interpreter can load SQLite
extensions, identical-score pure-Python cosine fallback otherwise). Seeded on
build with the runbook corpus in `mcp_servers/runbooks.py`.

### `search_runbooks`

| Input | Type | Default | Notes |
|---|---|---|---|
| `query` | `str` | — | symptom / hypothesis description |
| `k` | `int` | `3` | 1–10 |

Output `SearchRunbooksResult`: `query: str`,
`results: list[{slug, title, score, excerpt, tags}]` — `score` is cosine
similarity, higher = more relevant; `excerpt` ≤600 chars.

### `search_past_incidents`

Same inputs as `search_runbooks`. Output `SearchIncidentsResult`: `query: str`,
`results: list[{incident_id, title, score, excerpt}]`.

### `record_outcome`

Record how an incident turned out. Idempotent: upserts by `incident_id`.

| Input | Type | Default |
|---|---|---|
| `incident_id` | `str` | — |
| `summary` | `str` | — |
| `root_cause` | `str` | — |
| `remediation` | `str` | — |
| `resolved` | `bool` | — |
| `notes` | `str` | `""` |

Output `RecordOutcomeResult`: `incident_id: str`, `doc_id: int`,
`created: bool` (`false` = existing record updated).

## Testing

`tests/test_mcp_servers.py` exercises every tool over an in-memory MCP session
(`mcp.shared.memory`) against a fake controller / in-memory store — offline, no
Docker, mock mode. Covered: schema validation of all results, `dry_run`
defaults honored (no action recorded), idempotent no-ops, sandbox-guard
refusals, allowlist↔compose sync. `tests/test_knowledge_store.py` covers the
store itself.
