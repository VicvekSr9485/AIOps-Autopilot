# Pipeline vs baseline: diagnosis, fix, and verdict

All numbers below are **mock mode** (deterministic fault-aware mock sandbox +
heuristic mock model, zero real tokens). Re-run with `make bench`; the final
real-model run remains `make bench-real`.

## Verdict

**Target met.** On the original 5 faults the pipeline now matches the baseline
on remediation-correctness (4/5) and auto-resolution (4/5) while beating it on
safety (0 false remediations vs 1). On the expanded 8-fault suite the pipeline
clearly wins on safe-outcome rate: **100% vs 50%**. The token cost is real and
not hidden: the pipeline spends **~4.9x** the baseline's tokens per incident
(~1,700 vs ~350 mock-estimated tokens).

## Part A — why the first benchmark was lost

The recorded first run (pipeline 40% remediation-correct vs baseline 80%,
wrong-action-then-rollback on `bad_config_rollout`/`downstream_timeout`)
traced to a **planner handoff defect, plus a mock patch that hid it**:

1. The planner received only ~250 chars of hypothesis prose with pointer-only
   evidence ("see grouped log summary") and **zero telemetry**, followed by
   ~1.4 KB of runbook excerpts retrieved at near-noise similarity (scores
   0.03–0.10; the top-ranked "runbook" for `bad_config_rollout` was the
   *downstream timeout* one). The baseline, by contrast, saw the actual
   symptom line (`work_failed reason=invalid_feature_mode`) directly. The
   planner was information-starved relative to the baseline AND polluted by
   distractor retrieval — the user-stated hypothesis is **confirmed**, with
   retrieval pollution as the second half of the mechanism.
2. At the committed HEAD the loss no longer reproduced (80%/80%), because the
   measurement mock had been patched to split runbook text out of its
   inference input (`mockenv.py`) — masking the failure in mock mode without
   fixing the production prompt. Reverting that patch reproduced the recorded
   signature exactly for `bad_config_rollout`: pipeline proposes
   `restart_service db` → false remediation → auto-rollback, baseline proposes
   `rollback app` and resolves.

## What changed (kept) and why

1. **Planner evidence handoff** (`TriageResult.telemetry_summary`): the
   planner prompt now leads with the summarized telemetry triage gathered,
   then the hypothesis (full 300-char evidence excerpts), with runbooks last
   and labeled as approximate reference. Regression-tested with a
   deliberately vague hypothesis. The planner stays **toolless** — option B.2
   (read-only tool access) was unnecessary once evidence flowed forward, and
   no-tools is a stronger guarantee than read-only. "Only the executor may
   call mutating tools" is unchanged and now documented as the invariant.
2. **Safe-outcome metric** (`metrics.classify_outcome`): RESOLVED /
   SAFE_ESCALATED / UNSAFE_FAIL / MISSED_ESCALATION, with
   `safe_outcome_rate = (RESOLVED + SAFE_ESCALATED) / N`. Escalation counts
   as safe only when ground truth says no in-vocabulary fix exists, and
   silent inaction is never safe — escalate-everything scores as misses.
3. **Three hard faults** (ground truth honest, never tuned to favor either
   side): `config_rollout_worker_wedge` (the fix needs BOTH rollback and a
   worker restart; one action is incomplete), `db_outage_ambiguous` (capture
   carries only generic connection errors; the decisive FATAL detail is
   reachable only via a live log query — a no-tool reader genuinely cannot
   disambiguate), `worker_scaled_to_zero` (the only fixing action is
   destructive-class `scale_service`, so the correct path must pass the HITL
   gate; the tempting restart reflex is a no-op at zero replicas). All three
   inject/revert cleanly against the real Docker stack (sandbox suite green).
4. **Supporting capabilities the new faults exposed as missing** —
   production improvements, not benchmark special-cases:
   - Triage now deterministically queries live log groups + a fresh metric
     window before its single reasoning call (zero LLM tokens).
   - Verification gained a `backlog_draining` check — green probes with a
     growing/stuck queue no longer verify as resolved (this is what catches
     the baseline's partial fix on the wedge fault).
   - Knowledge retrieval was the genuinely broken piece behind Part A's
     pollution: `embed()` now uses 1024 buckets, stopword removal, and
     sublinear TF, and the retrieval query deduplicates digit-normalized log
     lines and includes metric deltas. Every fault now retrieves its matching
     runbook in the top-3 (previously: near-noise, wrong top-1).

One honest design deviation: Part D(iii) asked for a fault whose *tempting*
action is destructive. In this closed action vocabulary every
destructive-and-tempting candidate either genuinely fixes the fault (can't
honestly be scored wrong) or is inexpressible, so the fault was inverted: the
**correct** fix is destructive and must survive the gate. Combined with
`expired_credential` (gate correctly rejects), the gate is now exercised in
both directions: blocking bad destructive actions and passing necessary ones.

## Before / after

Original 5 faults (mock):

| Metric | First run (recorded) | HEAD (masked) | After fix |
|---|---|---|---|
| Pipeline remediation-correct | 40% | 80% | **80%** |
| Baseline remediation-correct | 80% | 80% | 80% |
| Pipeline auto-resolution | — | 80% | **80%** |
| Baseline auto-resolution | — | 80% | 80% |
| Pipeline false remediations | > baseline | 0% | **0%** |
| Baseline false remediations | — | 20% | 20% |
| Pipeline safe-outcome rate | — | — | **100%** |
| Baseline safe-outcome rate | — | — | 80% |
| Pipeline tokens/incident (mean) | ~4x baseline | 1481 | 1692 (~4.8x) |

Expanded suite (8 faults, mock):

| Metric | Pipeline | Baseline |
|---|---|---|
| Root-cause top-1 | 100% (8/8) | 87.5% (7/8) |
| Remediation correct (sandbox-verified) | **87.5%** (7/8) | 50% (4/8) |
| Auto-resolution | 75% (6/8)¹ | 50% (4/8) |
| Safe-outcome rate | **100%** (7 RESOLVED + 1 SAFE_ESCALATED) | 50% (4 RESOLVED + 4 UNSAFE_FAIL) |
| False remediations | **0%** | 50% |
| Tokens/incident (mean) | ~1,735 | ~360 |

¹ `expired_credential` (escalation is the correct outcome) and
`worker_scaled_to_zero` (destructive fix must pass the gate) cannot
auto-resolve **by design** — both still end safely.

Per-fault outcomes (expanded suite): the pipeline's lead comes precisely from
the capabilities the architecture pays tokens for — multi-step planning off
runbook guidance (wedge), live-telemetry disambiguation (ambiguous), gated
destructive remediation (scaled-to-zero), and gate-rejection of an unfixable
fault (credential). On the four "easy" faults the single prompt is exactly as
good and ~5x cheaper.

## Honest caveats

- These are **mock-mode** numbers: the heuristic mock model maps observable
  prompt keywords/patterns to causes and plans. It models *information access*
  (what is in the prompt), not model intelligence. The real-model run
  (`make bench-real`) is the final word.
- The mock convention "infer the fault from primary-evidence sections, consult
  the runbook section for plan refinement" is documented in `mockenv.py`; the
  ground-truth leak boundary (scoring-side only) is unchanged and test-enforced.
- `db_outage_ambiguous`'s ambiguity is guaranteed by the synthetic capture; in
  a real run it also depends on how much driver detail lands in the capture
  window (noted in the fault's spec).
- Token/cost figures are local CostMeter estimates; the Qwen Cloud usage page
  is authoritative.

Iteration budget: 3 of 4 change→measure loops used (handoff fix; safe-outcome
metric; new faults + retrieval/verification fixes).
