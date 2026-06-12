# Architecture

```
alert+logs+metrics → ingestion → triage/root-cause (qwen3.7-max, scoped tools)
    → remediation planner (qwen3.7-plus, NO tools) → HITL gate
    → executor (infra tools only; dry-run → apply) → verifier (telemetry only)
    → auto-rollback if unresolved → record_outcome (knowledge store)
                         ↑ benchmark harness injects known faults (TODO)
```

## Stage contracts (Pydantic at every boundary)

| Stage | In → Out | Model tier | Tools (exposure.py) |
|---|---|---|---|
| ingest | raw capture → `Incident` | none | none |
| triage | `Incident` → `TriageResult` (ranked hypotheses + cited evidence) | **reasoning** (the only one) | telemetry + knowledge |
| plan | `TriageResult` → `RemediationProposal` (steps, rollback, risk, blast radius) | default | **none** (runbooks flow forward from triage) |
| HITL gate | proposal → `GateOutcome` | none | none (Approver protocol) |
| execute | approved proposal → `ExecutionResult` | none | infra/ops only |
| verify | — → `VerificationResult` | none | telemetry only |

## Safety model

- **Sandbox-only, structurally:** closed-enum targets + server-side namespace
  injection in the MCP layer; no model-facing field can name a foreign target.
- **HITL routing:** auto-approve iff confidence ≥ 0.75 AND risk ≤ 0.4;
  destructive actions (`apply_config`, `scale_service` — classified server-side)
  always escalate, and a 0.6 risk floor overrides model-claimed risk.
- **Approval is a capability:** proposals are born `requires_approval=True`;
  only the gate clears it and the executor refuses anything still flagged.
- **Execution discipline:** per step dry-run → apply, halt on failure; failed
  verification triggers the rollback plan automatically.
- **Cost discipline:** telemetry summarized before any prompt; structured-output
  retries bounded by attempts AND tokens; every call metered per step with
  free-tier/voucher attribution.

Full MCP tool schemas and the parameter-injection/stage-scoping patterns:
[mcp.md](mcp.md). Benchmark methodology vs the single-prompt baseline: TODO
(next milestone).
