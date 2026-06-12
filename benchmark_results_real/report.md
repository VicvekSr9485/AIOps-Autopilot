# Benchmark report — agent pipeline vs single-prompt baseline

- Run: 2026-06-12T19:11:23.521906+00:00 → 2026-06-12T19:51:30.715163+00:00 (real mode)
- Models (constant for the whole run, asserted): `default` → `qwen3.7-plus`, `reasoning` → `qwen3.7-max`
- Model consistency check: PASSED

## Approach comparison

| Metric | pipeline | baseline |
|---|---|---|
| Scenarios | 8 | 8 |
| Root-cause top-1 accuracy | 88% | 50% |
| Root-cause top-3 accuracy | 100% | 50% |
| Remediation correct (sandbox-verified) | 25% | 62% |
| Auto-resolution rate | 25% | 62% |
| Safe-outcome rate (RESOLVED + SAFE_ESCALATED) | 25% | 62% |
| Outcomes (R/SE/UF/ME) | 2/0/4/2 | 5/0/3/0 |
| False-remediation rate | 50% | 38% |
| Escalation rate | 25% | 0% |
| Schema-failure rate | 0% | 0% |
| Invalid tool calls | 0 | 0 |
| Tokens / incident (mean) | 7224 | 1672 |
| Tokens / incident (p95) | 8673 | 1969 |
| Total tokens | 57788 | 13376 |
| Est. cost (USD) | $0.1069 | $0.0314 |
| LLM calls to diagnosis (mean) | 1.0 | 1.0 |
| Time to diagnosis (mean s) | 46.830 | 17.270 |

## Per-scenario results

| Fault | Approach | RC top-1 | RC top-3 | Remediation | Outcome | Escalated | Rolled back | Tokens | Est. USD |
|---|---|---|---|---|---|---|---|---|---|
| db_pool_exhaustion | pipeline | yes | yes | WRONG | UNSAFE_FAIL | no | yes | 6711 | $0.0115 |
| db_pool_exhaustion | baseline | yes | yes | correct | RESOLVED | no | no | 1818 | $0.0044 |
| bad_config_rollout | pipeline | yes | yes | not executed | MISSED_ESCALATION | yes | no | 6837 | $0.0134 |
| bad_config_rollout | baseline | yes | yes | correct | RESOLVED | no | no | 1244 | $0.0024 |
| downstream_timeout | pipeline | yes | yes | correct | RESOLVED | no | no | 7867 | $0.0160 |
| downstream_timeout | baseline | yes | yes | correct | RESOLVED | no | no | 1706 | $0.0042 |
| queue_consumer_stall | pipeline | yes | yes | not executed | MISSED_ESCALATION | yes | no | 6148 | $0.0106 |
| queue_consumer_stall | baseline | no | no | correct | RESOLVED | no | no | 1535 | $0.0034 |
| expired_credential | pipeline | yes | yes | WRONG | UNSAFE_FAIL | no | yes | 8673 | $0.0140 |
| expired_credential | baseline | yes | yes | WRONG | UNSAFE_FAIL | no | no | 1910 | $0.0047 |
| config_rollout_worker_wedge | pipeline | yes | yes | correct | RESOLVED | no | no | 7046 | $0.0137 |
| config_rollout_worker_wedge | baseline | no | no | WRONG | UNSAFE_FAIL | no | no | 1560 | $0.0036 |
| db_outage_ambiguous | pipeline | yes | yes | WRONG | UNSAFE_FAIL | no | yes | 6409 | $0.0103 |
| db_outage_ambiguous | baseline | no | no | correct | RESOLVED | no | no | 1969 | $0.0050 |
| worker_scaled_to_zero | pipeline | no | yes | WRONG | UNSAFE_FAIL | no | yes | 8097 | $0.0173 |
| worker_scaled_to_zero | baseline | no | no | WRONG | UNSAFE_FAIL | no | no | 1634 | $0.0038 |

## Summarization ablation (pipeline tokens per incident)

Context mode A = tool/telemetry outputs summarized before entering the prompt (production default); mode B = raw outputs in context.

| Fault | A: summarized | B: raw | Saving |
|---|---|---|---|
| db_pool_exhaustion | 6711 | 12978 | 48.3% |
| bad_config_rollout | 6837 | 9909 | 31.0% |
| downstream_timeout | 7867 | 10900 | 27.8% |
| queue_consumer_stall | 6148 | 11601 | 47.0% |
| expired_credential | 8673 | 14347 | 39.5% |
| config_rollout_worker_wedge | 7046 | 9768 | 27.9% |
| db_outage_ambiguous | 6409 | 12833 | 50.1% |
| worker_scaled_to_zero | 8097 | 11092 | 27.0% |
| **mean** | **7224** | **11678** | **38.1%** |

## Run-level cost (local estimate)

- Total tokens: 164592
- Estimated cost: $0.2809
- Free-tier vs voucher split: 164592 free / 0 voucher tokens
  - qwen3.7-max: 24 calls, in=72288 out=38863, est $0.2361, free=111151 voucher=0
  - qwen3.7-plus: 16 calls, in=24574 out=28867, est $0.0448, free=53441 voucher=0

> All token/cost figures are LOCAL estimates from CostMeter; the authoritative usage and billing numbers are the Qwen Cloud Analytics/Usage page.
