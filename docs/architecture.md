# Architecture (skeleton)

```
alert+logs+metrics → ingestion → triage → root-cause (qwen3.7-max)
    → risk-scored remediation → HITL gate → sandbox executor → verification
                                     ↑ benchmark harness injects known faults
```

To be expanded: stage contracts, MCP tool inventory, risk-scoring rubric,
HITL gate semantics, benchmark methodology vs single-prompt baseline.
