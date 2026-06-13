# AIOps Autopilot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
&nbsp;Built for the **Qwen Cloud "Autopilot Agent"** hackathon track · runs on
Alibaba Cloud ECS, reasons with **qwen3.7-max** on Qwen Cloud (DashScope).

An autopilot agent for cloud incident response: it ingests an ambiguous incident
(alert + logs + metrics), reasons to a root cause, proposes a **risk-scored**
remediation, passes a **human-in-the-loop gate**, executes the approved action
**only against a sandboxed docker-compose stack**, then verifies the fix — and is
measured against a single-prompt baseline on a fault-injection harness with known
ground truth.

## Headline result

Real Qwen models (`qwen3.7-max`), real Docker fault injection, 8 seeded faults,
agent vs a single-prompt baseline. Full report: [REPORT-real-v2.md](REPORT-real-v2.md).

| Metric (real run, 8 faults) | **Pipeline** | Single-prompt baseline |
|---|---|---|
| Root-cause top-1 accuracy | **100%** | 50% |
| Remediation correct (sandbox-verified) | **75%** | 62.5% |
| Safe-outcome rate | **87.5%** | 62.5% |
| Residual damage — system left broken | **0%** | 37.5% |
| **Invalid / out-of-sandbox tool calls** | **0** | 0 |
| Est. cost / incident (free tier) | ~$0.016 | ~$0.0037 |

The pipeline **leads every quality metric**, most decisively on **diagnosis
(100% vs 50%)** and **damage containment (0% vs 37.5% residual)** — it rolls back
its own wrong action where the baseline leaves the system broken — at ~4x the
baseline's tokens (the honest cost of a reasoning-tier planner). All 159,775
tokens for the run landed on the **free tier** (est. **$0.35**, 0 voucher, 0
structured-output retries).

## Safety by construction

**The agent physically cannot act outside the sandbox — because it never picks a
free-text target.** Every mutating action is a typed MCP tool call whose:

- **compose namespace is injected server-side** (`autopilot-sandbox`, bound at
  server build time — there is no model-facing field for it);
- **service target is a closed enum** of the five sandbox services
  (`app`/`worker`/`downstream`/`db`/`queue`) — an out-of-enum value dies at
  schema validation, with a runtime guard as a second wall;
- **config target and incident id are fixed/injected server-side**, never model
  params.

So the model chooses *which action*, not *what to point it at*. A hallucinated or
adversarial target (`prod-db`, `/var/run/docker.sock`, `../host`,
`app; rm -rf /`) cannot even be expressed. The benchmark confirms it: **0 invalid
(out-of-sandbox) tool calls across all 8 scenarios**, and the deployed backend
mounts no Docker socket at all. Approval is likewise a capability — proposals are
born `requires_approval=True`, only the HITL gate clears it, and the executor
refuses anything still flagged.

## Quick start (local, offline)

```bash
cp .env.example .env        # add DASHSCOPE_API_KEY for real runs; not needed for mock
make install
make test                   # full suite, fully offline (deterministic mock LLM, no network)
make run-api                # API at http://localhost:8080/healthz
# In another shell, the demo dashboard (feed · live trace · HITL gate · benchmark):
cd dashboard && npm install && npm run dev    # http://localhost:5173
```

Everything above runs **without spending a token** — tests and the dashboard demo
drive an in-process MockWorld with a deterministic mock model.

## Run the benchmark

```bash
make bench        # offline mock mode: no Docker, no tokens (development/CI default)
make bench-real   # FINAL RUN ONLY: real Qwen models + real Docker sandbox (spends tokens)
```

The harness runs the **full agent pipeline** and a **single-prompt baseline**
(same reasoning-tier model, no stages/tools/gate) over the seeded faults, with the
HITL gate auto-answered from ground truth. It reports root-cause accuracy,
**sandbox-verified** remediation correctness, safe-outcome and residual-damage
rates, invalid-tool-call counts, tokens (mean/p95) and estimated cost, plus a
**summarization ablation** (telemetry compaction saves **43.8%** of real tokens).
Model tiering is asserted constant for the whole run and the exact model strings
are recorded. Artifacts land in `benchmark_results*/`: `results.json`,
`report.md`, and per-scenario traces.

## Deploy to Alibaba Cloud

Containerized backend ([`Dockerfile`](Dockerfile)) + ECS deployment config and
the **live cloud proof** in [`deployment/`](deployment/DEPLOYMENT.md). The proof
file [`src/autopilot/cloud/qwen_live.py`](src/autopilot/cloud/qwen_live.py) (and
route `GET /api/cloud/selfcheck`) makes one real, metered round-trip to the Qwen
Cloud (`*.aliyuncs.com`) endpoint and reports host/region/model/tokens/cost.

## Architecture

```
alert+logs+metrics → ingest → triage / root-cause (qwen3.7-max, scoped tools)
   → remediation planner (qwen3.7-max, NO tools) → HITL gate
   → executor (infra tools only; dry-run → apply) → verifier (telemetry only)
   → auto-rollback if unresolved → record_outcome (knowledge store)
                       ↑ fault harness injects known faults; agent vs baseline
```

Full diagram, stage contracts, and design decisions: [docs/architecture.md](docs/architecture.md).

| Path | Purpose |
|---|---|
| `src/autopilot/llm/` | Typed Qwen client: role-based model tiering, cost metering, mock mode |
| `src/autopilot/ingestion/` | Normalizes raw sandbox captures into typed Incidents |
| `src/autopilot/pipeline/` | Full agent loop: ingest → triage → plan → HITL gate → execute → verify → record |
| `src/autopilot/mcp_servers/` | MCP tool servers: telemetry, infra/ops, knowledge — see [docs/mcp.md](docs/mcp.md) |
| `src/autopilot/sandbox/` | Deterministic controller for the sandbox compose stack |
| `src/autopilot/harness/` | Fault-injection harness with ground truth (8 faults) |
| `src/autopilot/benchmark/` | Measurement layer: pipeline vs single-prompt baseline, ablation |
| `src/autopilot/api/` + `dashboard/` | FastAPI demo surface + Vite/React UI |
| `sandbox/` | docker-compose stack — the ONLY infrastructure the executor may touch |
| `deployment/` | Dockerfile orchestration + ECS deploy config + Qwen Cloud proof |

## MCP tool surface

The agent observes and acts only through three MCP servers (official Python SDK,
stdio): **telemetry** (summarized logs/metrics/alerts/traces), **infra**
(sandbox-only mutations, `dry_run` defaults true, idempotent), and **knowledge**
(runbook + past-incident vector search, outcome recording). Full schemas in
[docs/mcp.md](docs/mcp.md).

## Tooling efficiency — and why not a gateway

At this scale — **3 local stdio servers, ~12 tools, one agent, on a 1M-context
model** — the failure modes that justify heavyweight MCP infrastructure simply do
not apply: twelve tool schemas are a rounding error in a 1M-token window, so
**schema bloat** and **tool-selection collapse** are non-problems. We therefore
deliberately chose three lean, auditable patterns over the alternatives:

- **Stage-scoped tool exposure** — each pipeline stage sees only the minimal
  server subset (`planner` → **none**; only the `executor` sees mutating tools).
  A stage that cannot see a tool cannot call it.
- **Server-side parameter injection** — deterministic values (sandbox namespace,
  incident id, fixed config target) are never model params; this is what makes
  sandbox-only *structural* (see above), not merely validated.
- **Output summarization** — telemetry tools never return raw dumps; logs are
  deduplicated groups, metrics are windowed deltas, bounded before any prompt.

The heavier patterns are **scaling paths for future work**, not gaps here:

| Pattern | When it pays off | Why not now |
|---|---|---|
| MCP **gateway / router** | many servers, many consumers, cross-cutting auth/routing | one consumer, three local servers — nothing to route; an extra hop to test/audit |
| **Dynamic tool discovery / search** | hundreds of tools that can't fit a prompt | 12 schemas fit cheaply; a static stage map is *stricter* (it removes tools, not finds more) |
| **Code-execution mode** (model writes code calling tools) | very high tool fan-out, composition-heavy workflows | adds a second execution surface to sandbox separately, undermining the single hard guarantee — every action stays an enumerated, typed, HITL-gated, audit-logged tool call |

## LLM usage

Models are selected by **role**, never hardcoded at a call site: `reasoning`
(root-cause) and `planning` (remediation) → **qwen3.7-max**; `default`
(everything else) → **qwen3.7-plus**. Every call is metered (tokens, est. USD,
free-tier vs voucher). Tests force `AUTOPILOT_MOCK_LLM=1` and never spend money or
touch the network.

## License

MIT — see [LICENSE](LICENSE).
