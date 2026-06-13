# Benchmark report — agent pipeline vs single-prompt baseline

- Run: 2026-06-13T02:20:25.271932+00:00 → 2026-06-13T02:53:30.546689+00:00 (real mode)
- Models (constant for the whole run, asserted): `default` → `qwen3.7-plus`, `planning` → `qwen3.7-max`, `reasoning` → `qwen3.7-max`
- Model consistency check: PASSED

## Approach comparison

| Metric | pipeline | baseline |
|---|---|---|
| Scenarios | 8 | 8 |
| Root-cause top-1 accuracy | 100% | 50% |
| Root-cause top-3 accuracy | 100% | 50% |
| Remediation correct (sandbox-verified) | 75% | 62% |
| Auto-resolution rate | 75% | 62% |
| Safe-outcome rate (RESOLVED + SAFE_ESCALATED) | 88% | 62% |
| Outcomes (R/SE/UF/ME) | 6/1/1/0 | 5/0/3/0 |
| False-remediation rate | 12% | 38% |
| Residual-damage rate (left broken, not rolled back) | 0% | 38% |
| Escalation rate | 12% | 0% |
| Schema-failure rate | 0% | 0% |
| Invalid tool calls | 0 | 0 |
| Tokens / incident (mean) | 6606 | 1603 |
| Tokens / incident (p95) | 7367 | 1823 |
| Total tokens | 52850 | 12826 |
| Est. cost (USD) | $0.1299 | $0.0294 |
| LLM calls to diagnosis (mean) | 1.0 | 1.0 |
| Time to diagnosis (mean s) | 38.541 | 14.549 |

## Per-scenario results

| Fault | Approach | RC top-1 | RC top-3 | Remediation | Outcome | Escalated | Rolled back | Tokens | Est. USD |
|---|---|---|---|---|---|---|---|---|---|
| db_pool_exhaustion | pipeline | yes | yes | correct | RESOLVED | no | no | 6212 | $0.0143 |
| db_pool_exhaustion | baseline | no | no | correct | RESOLVED | no | no | 1680 | $0.0039 |
| bad_config_rollout | pipeline | yes | yes | correct | RESOLVED | no | no | 6836 | $0.0176 |
| bad_config_rollout | baseline | yes | yes | correct | RESOLVED | no | no | 1429 | $0.0031 |
| downstream_timeout | pipeline | yes | yes | correct | RESOLVED | no | no | 7367 | $0.0194 |
| downstream_timeout | baseline | yes | yes | correct | RESOLVED | no | no | 1708 | $0.0042 |
| queue_consumer_stall | pipeline | yes | yes | correct | RESOLVED | no | no | 6034 | $0.0140 |
| queue_consumer_stall | baseline | yes | yes | correct | RESOLVED | no | no | 1623 | $0.0038 |
| expired_credential | pipeline | yes | yes | not executed | SAFE_ESCALATED | yes | no | 6721 | $0.0163 |
| expired_credential | baseline | yes | yes | WRONG | UNSAFE_FAIL | no | no | 1823 | $0.0044 |
| config_rollout_worker_wedge | pipeline | yes | yes | correct | RESOLVED | no | no | 6349 | $0.0155 |
| config_rollout_worker_wedge | baseline | no | no | WRONG | UNSAFE_FAIL | no | no | 1572 | $0.0036 |
| db_outage_ambiguous | pipeline | yes | yes | correct | RESOLVED | no | no | 6777 | $0.0167 |
| db_outage_ambiguous | baseline | no | no | correct | RESOLVED | no | no | 1501 | $0.0032 |
| worker_scaled_to_zero | pipeline | yes | yes | WRONG | UNSAFE_FAIL | no | yes | 6554 | $0.0160 |
| worker_scaled_to_zero | baseline | no | no | WRONG | UNSAFE_FAIL | no | no | 1490 | $0.0033 |

## Summarization ablation (pipeline tokens per incident)

Context mode A = tool/telemetry outputs summarized before entering the prompt (production default); mode B = raw outputs in context.

| Fault | A: summarized | B: raw | Saving |
|---|---|---|---|
| db_pool_exhaustion | 6212 | 12909 | 51.9% |
| bad_config_rollout | 6836 | 11117 | 38.5% |
| downstream_timeout | 7367 | 10185 | 27.7% |
| queue_consumer_stall | 6034 | 10808 | 44.2% |
| expired_credential | 6721 | 13329 | 49.6% |
| config_rollout_worker_wedge | 6349 | 11414 | 44.4% |
| db_outage_ambiguous | 6777 | 13037 | 48.0% |
| worker_scaled_to_zero | 6554 | 11300 | 42.0% |
| **mean** | **6606** | **11762** | **43.8%** |

## Run-level cost (local estimate)

- Total tokens: 159775
- Estimated cost: $0.3455
- Free-tier vs voucher split: 159775 free / 0 voucher tokens
  - qwen3.7-max: 40 calls, in=101450 out=58325, est $0.3455, free=159775 voucher=0

> All token/cost figures are LOCAL estimates from CostMeter; the authoritative usage and billing numbers are the Qwen Cloud Analytics/Usage page.
