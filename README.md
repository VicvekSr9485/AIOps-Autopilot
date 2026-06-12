# AIOps Autopilot

An autopilot agent for cloud incident response, built for the Qwen Cloud
**Autopilot Agent** hackathon track.

**Pipeline:** ingest an ambiguous incident (alert + logs + metrics) → reason to a
root cause → propose a risk-scored remediation → human-in-the-loop approval gate →
execute **only** against a sandboxed docker-compose stack → verify resolution.
Every run is benchmarked against a single-prompt baseline via a fault-injection
harness with known ground truth.

## Quick start

```bash
cp .env.example .env        # add your DASHSCOPE_API_KEY
make install
make test                   # runs fully offline (deterministic mock LLM)
make run-api                # http://localhost:8080/healthz
make sandbox-up             # start the sandboxed target stack
```

## Layout

| Path | Purpose |
|---|---|
| `src/autopilot/llm/` | Typed Qwen client: role-based model tiering, cost metering, mock mode |
| `src/autopilot/ingestion/` | Normalizes raw sandbox captures into typed Incidents |
| `src/autopilot/pipeline/` | Agent stages: ingestion + triage/root-cause done; remediation → verify (TODO) |
| `src/autopilot/mcp_servers/` | MCP tool servers: telemetry, infra/ops, knowledge — see [docs/mcp.md](docs/mcp.md) |
| `src/autopilot/sandbox/` | Deterministic controller for the sandbox compose stack |
| `src/autopilot/harness/` | Fault-injection harness (5 faults) with ground truth |
| `src/autopilot/benchmark/` | Agent-vs-baseline benchmark runner (TODO) |
| `sandbox/` | docker-compose stack the agent is allowed to act on |
| `dashboard/` | Vite + React UI (stub) |
| `docs/` | Architecture and judging-facing docs |

## MCP tool surface

The agent observes and acts only through three MCP servers (official Python
SDK, stdio): **telemetry** (summarized logs/metrics/alerts/traces), **infra**
(sandbox-only mutations, `dry_run` defaults to true, idempotent), and
**knowledge** (runbook + past-incident vector search, outcome recording).
Run them with `make mcp-telemetry | mcp-infra | mcp-knowledge`; full tool
schemas in [docs/mcp.md](docs/mcp.md).

## LLM usage

Models are selected by **role**: `reasoning` → `qwen3.7-max` (root-cause step
only), `default` → `qwen3.7-plus` (everything else). Every call is metered
(tokens, est. USD, free-tier vs voucher estimate). Tests run with
`AUTOPILOT_MOCK_LLM=1` and never spend money.

## License

MIT — see [LICENSE](LICENSE).
