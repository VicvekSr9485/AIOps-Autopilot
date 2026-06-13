# FINAL real-model benchmark: validation run report

Run: 2026-06-12, `make bench-real`, full 8-fault suite, both approaches,
summarization ablation included. Models (asserted constant for the whole run,
ModelConsistencyError armed): `reasoning` → `qwen3.7-max`, `default` →
`qwen3.7-plus`. Zero schema failures and zero structured-output retries across
all 40 LLM calls. Canonical artifacts (results.json, report.md, 24 per-scenario
traces) are preserved in `benchmark_results_real/` for the dashboard and the
README/demo headline.

## Verdict: the real run does NOT track mock, and is reported as measured

**On this suite, with this model pairing, the single-prompt qwen3.7-max
baseline outperforms the staged pipeline on remediation.** Baseline: 62.5%
remediation-correct / 62.5% safe-outcome at ~1,672 tokens/incident. Pipeline:
25% / 25% at ~7,224 tokens/incident (4.3x). The pipeline wins on diagnosis
accuracy (root-cause top-1 87.5% vs 50%), wins the multi-step fault that the
baseline structurally cannot solve, and contained every one of its own wrong
actions (4/4 auto-rolled back; the baseline has no rollback and left its 3
wrong mutations applied), but that is the honest extent of the win. Mock
predicted a decisive pipeline victory; the real run falsified the part of that
prediction that depended on competent cheap-tier planning and well-calibrated
confidence. Per the validation protocol (one run, no tuning against results),
the run stops here and is reported as measured.

## Headline metrics, real vs mock side-by-side

| Metric | Pipeline real | Pipeline mock | Baseline real | Baseline mock |
|---|---|---|---|---|
| Root-cause top-1 | **87.5%** | 100% | 50% | 87.5% |
| Remediation correct (sandbox-verified) | 25% | 87.5% | **62.5%** | 50% |
| Auto-resolution | 25% | 75% | **62.5%** | 50% |
| Safe-outcome rate | 25% | 100% | **62.5%** | 50% |
| Outcomes (R/SE/UF/ME) | 2/0/4/2 | 7/1/0/0 | 5/0/3/0 | 4/0/4/0 |
| False-remediation rate | 50% | 0% | 37.5% | 50% |
| Tokens / incident (mean) | 7,224 | 1,735 | 1,672 | ~360 |
| Est. cost / incident | $0.0134 | n/a | $0.0039 | n/a |

## Hard vs easy faults

- **Easy 4** (db_pool, bad_config, downstream, queue_stall): baseline **4/4**
  resolved at ~1,576 tokens each; pipeline **1/4** at ~6,891 tokens. The mock
  report's claim was "single-shot ties the easy faults and is ~5x cheaper";
  the real run is harsher: **single-shot wins the easy faults outright**; the
  honest statement is that one well-prompted max-tier call is both better and
  ~4.4x cheaper on routine single-action incidents.
- **Hard 4** (expired_credential, wedge, ambiguous, scaled_zero): 1/4 each.
  The pipeline's one hard win is `config_rollout_worker_wedge`, the
  multi-step class it was designed for: triage retrieved the wedge runbook,
  the planner emitted the genuine two-step fix (rollback → restart worker),
  verification confirmed on the first settle pass, while the baseline's
  single action failed. The baseline's one hard win (`db_outage_ambiguous`)
  is disqualified as a disambiguation test (see honesty checks).

So the defensible framing of the architecture's value is narrow and specific:
**multi-step remediation, diagnosis accuracy, and blast-radius containment
(gate + rollback)**, not general remediation quality, which the cheap-tier
planner currently caps.

## Why the pipeline lost: failure anatomy (from the traces)

The diagnosis layer was strong (7/8 top-1; triage causes often literally echo
the right runbook title). The losses are concentrated in one component, the
**qwen3.7-plus planner**, plus one calibration assumption:

1. **Action-vocabulary mismatch** (`db_pool_exhaustion`, `db_outage_ambiguous`):
   correct diagnosis, correct runbook retrieved top-1, but the runbook's
   remediation ("pg_terminate_backend… then confirm the app reconnects") is
   not expressible in the executor vocabulary, and the planner mapped it to
   `restart app` instead of `restart db`. Applied, failed verification (full
   45 s window), auto-rolled back: UNSAFE_FAIL ×2.
2. **Hallucinated config value** (`bad_config_rollout`): planner proposed
   `apply_config {"feature_mode": "stable"}` ("standard" is correct).
   Destructive → gate escalated → oracle rejected. The gate prevented a wrong
   mutation, but a fixable fault went unfixed: MISSED_ESCALATION.
3. **Sibling-runbook confusion** (`queue_consumer_stall`,
   `worker_scaled_to_zero`): both queue runbooks retrieve for both queue
   faults, and the planner followed the wrong one each time (scale-for-stall,
   restart-for-scaled-to-zero). One was caught by the gate (destructive), the
   other by the new `backlog_draining` verification check.
4. **No escalate option + miscalibrated confidence** (`expired_credential`):
   triage diagnosed the credential failure correctly at 0.95 confidence, but
   the plan schema forces ≥1 action, so the planner emitted a benign
   `rollback` (risk 0.15), and 0.95 ≥ 0.75 auto-approved it. A no-op false
   remediation where mock produced SAFE_ESCALATED. Top-hypothesis confidences
   were 0.85-0.98 on all 8 faults, so the gate's confidence threshold never
   fired once; in real mode the auto-gate is effectively destructiveness-only.

What DID work as designed, with real models: triage's deterministic
evidence-gathering and retrieval (right runbook in top-3 for 7/8 incidents),
strict-JSON discipline (0 retries in 40 calls), the HITL gate (blocked both
wrong destructive plans), verification with the backlog check (caught a
green-probe no-op fix), and auto-rollback (restored the sandbox after all 4
wrong pipeline actions; the gateless baseline left its 3 wrong actions in
place; UNSAFE_FAIL undercounts this asymmetry).

## Honesty checks (required)

- **`db_outage_ambiguous` was NOT ambiguous in the real capture.** The
  alert-time capture summary contains the decisive `remaining connection
  slots` FATAL text (postgres writes it to compose logs; the app surfaces
  driver detail), so the no-tool baseline saw it and fixed the fault. **The
  tool-disambiguation win is not claimed**; in real mode this scenario
  degenerated into a db_pool duplicate, exactly the caveat recorded in the
  fault's spec. A real disambiguation test needs a capture pathway that
  genuinely truncates driver detail (future work; mock-only claim stands as
  mock-only).
- **Faults whose real behavior diverged from mock**: pipeline regressed on
  db_pool (wrong target), bad_config (hallucinated value, gate-rejected),
  queue_stall (wrong sibling runbook, gate-rejected), expired_credential
  (SAFE_ESCALATED → UNSAFE_FAIL via overconfidence + forced action),
  db_outage_ambiguous and worker_scaled_to_zero (wrong plan). Baseline
  improved on db_outage_ambiguous (capture not ambiguous) and held everywhere
  else. The wedge and downstream_timeout replicated for the pipeline.
- **Root-cause keyword scoring understates the baseline.** Baseline top-1 of
  50% includes phrasing misses, e.g. "db connection limit exceeded" (correct
  in substance) failing the `["connection","slot"/"pool"]` patterns. Treat
  the rc gap (87.5 vs 50) as real but smaller than it looks.
- **Oracle conservatism**: the ground-truth approver rejects any plan outside
  `REQUIRED_FIX_STEPS`. For `queue_consumer_stall` it rejected
  `scale worker→1`, which would genuinely have no-opped (already 1 replica),
  so the reject was correct here, but the mechanism can in principle
  under-credit non-canonical fixes. Noted, not changed mid-validation.
- **Aborted first attempt (disclosed)**: the first `bench-real` was aborted
  after 3 scenarios (~27k tokens, est **$0.046**, all free-tier) when both
  approaches "failed" db_pool identically: the benchmark verified with
  `verify_interval_s=0` and zero settle, sampling a still-restarting container,
  measuring restart latency, not remediation correctness. Fix: bounded
  convergence window in the verifier (45 s, identical for both approaches;
  final steady-state measurement recorded; mock keeps settle=0), validated
  against the real stack with zero LLM tokens before the re-run. This was the
  protocol's one permitted investigate-fix-rerun, spent on a harness defect,
  not on tuning either approach.

## Ablation (real tokens)

Summarized (mode A) vs raw (mode B) context, pipeline tokens per incident:
mean **7,224 vs 11,679, a 38.1% saving** (range 27.0-50.1% per fault),
stronger than the mock estimate of 20.8% because real captures are bulkier
than synthetic ones. The summarization design pays for itself at real scale.

## Cost (LOCAL estimate; the Qwen Cloud Analytics/Usage page is authoritative)

- Final run: **164,592 tokens, est $0.2809**, from qwen3.7-max 24 calls
  (72,288 in / 38,863 out, $0.2361), qwen3.7-plus 16 calls (24,574 in /
  28,867 out, $0.0448). Free-tier vs voucher split: **164,592 free / 0
  voucher**.
- Plus pre-flight smoke ($0.0001) and the disclosed aborted attempt
  ($0.046): total session ≈ **$0.33 estimated, all free-tier; the voucher was
  never touched.** The AUTOPILOT_RUN_TOKEN_CAP=400,000 kill switch (added for
  this run) was never approached.

## What this means (no changes made; recorded for future work)

The measured bottleneck is planning, not architecture plumbing: a
reasoning-tier planner (or planner self-check against the runbook), an
explicit `escalate` member in the plan vocabulary (so unfixable faults don't
force a fabricated action), and de-confusable queue runbooks are the obvious
next experiments; each would need a fresh validation run, not a re-tune of
this one. As measured today: **for routine single-action incidents, one
max-tier prompt suffices and is ~4x cheaper; the staged pipeline earns its
tokens on multi-step remediations, diagnosis quality, and damage containment.**
