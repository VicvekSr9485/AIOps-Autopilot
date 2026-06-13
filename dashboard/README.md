# Dashboard

Vite + React demo UI for AIOps Autopilot. Views:

- **Incident feed + scenario launcher** — inject any of the 8 fault scenarios
  and watch a run appear in the feed with a live status chip.
- **Live reasoning trace** — every pipeline stage (ingest → triage → plan →
  HITL gate → execute → verify → rollback → outcome) as a timeline card with the
  one-line takeaway, ranked hypotheses + cited evidence + consulted runbooks,
  the planned steps, diagnosis/fix **confidence**, and each stage's **token +
  cost**. Built to be readable in seconds.
- **HITL approval gate** — when a run pauses, the operator reviews the diagnosis,
  the escalation reasons, and the proposed steps, then **approves / edits /
  rejects** (the decision flows back into the live pipeline).
- **Benchmark** — pipeline vs. single-prompt baseline headline metrics, the
  damage-containment (residual-damage) stat, the summarization ablation, and the
  run cost summary, read from the committed real-run artifacts.

## Run it

```bash
make run-api                                  # FastAPI backend on :8080 (mock mode)
cd dashboard && npm install && npm run dev    # Vite dev server on :5173
```

The dev server proxies `/api` and `/healthz` to the backend. Everything runs
offline in mock mode — no Docker, no tokens, no network.

## Layout

- `src/api.js` — REST + SSE client.
- `src/App.jsx` — layout, sidebar (launcher + feed), live/benchmark tabs, SSE wiring.
- `src/components/TraceTimeline.jsx` — the reasoning trace.
- `src/components/ApprovalGate.jsx` — the HITL decision panel.
- `src/components/Benchmark.jsx` — the comparison view.
- `src/format.js` — shared formatting (status/stage labels, tokens, cost, %).
