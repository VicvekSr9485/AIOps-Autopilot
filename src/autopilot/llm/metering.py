"""Per-call token/cost metering and a LOCAL free-tier vs paid-voucher estimate.

CAVEAT: every number here is a client-side estimate. The authoritative usage and
billing figures are on the Qwen Cloud console (Analytics / Usage page). This meter
exists so we notice runaway spend immediately, not to replace the console.
"""

from __future__ import annotations

from typing import Literal

import structlog
from pydantic import BaseModel

from autopilot.config import ModelPricing

log = structlog.get_logger("autopilot.llm.metering")

Tier = Literal["free", "voucher"]


class CallRecord(BaseModel):
    model: str
    role: str
    step: str
    input_tokens: int
    output_tokens: int
    est_cost_usd: float
    tier: Tier
    free_tokens_used: int
    voucher_tokens_used: int


class ModelSummary(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0.0
    free_tokens_used: int = 0
    voucher_tokens_used: int = 0
    free_tokens_remaining: int = 0


class CostMeter:
    """Tracks every LLM call: tokens, estimated USD cost, and which budget it draws on.

    A call is flagged "free" only if it fits entirely within the remaining local
    free-tier estimate for its model; otherwise it is flagged "voucher" (any
    overflow means paid credit is expected to be touched).
    """

    def __init__(self, pricing: dict[str, ModelPricing], free_tier_tokens: dict[str, int]):
        self._pricing = pricing
        self._free_remaining: dict[str, int] = dict(free_tier_tokens)
        self.records: list[CallRecord] = []

    def record(
        self, *, model: str, role: str, step: str, input_tokens: int, output_tokens: int
    ) -> CallRecord:
        pricing = self._pricing[model]
        est_cost = (
            input_tokens / 1_000_000 * pricing.usd_per_m_input
            + output_tokens / 1_000_000 * pricing.usd_per_m_output
        )
        total = input_tokens + output_tokens
        remaining = self._free_remaining.get(model, 0)
        free_used = min(remaining, total)
        voucher_used = total - free_used
        tier: Tier = "free" if voucher_used == 0 else "voucher"
        self._free_remaining[model] = remaining - free_used

        rec = CallRecord(
            model=model,
            role=role,
            step=step,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=round(est_cost, 6),
            tier=tier,
            free_tokens_used=free_used,
            voucher_tokens_used=voucher_used,
        )
        self.records.append(rec)
        log.info(
            "llm_call_metered",
            **rec.model_dump(),
            free_tokens_remaining=self._free_remaining[model],
            caveat="local estimate only; authoritative usage = Qwen Cloud Analytics/Usage page",
        )
        return rec

    def free_tokens_remaining(self, model: str) -> int:
        return self._free_remaining.get(model, 0)

    def summary(self) -> dict[str, ModelSummary]:
        out: dict[str, ModelSummary] = {}
        for rec in self.records:
            s = out.setdefault(
                rec.model,
                ModelSummary(free_tokens_remaining=self._free_remaining.get(rec.model, 0)),
            )
            s.calls += 1
            s.input_tokens += rec.input_tokens
            s.output_tokens += rec.output_tokens
            s.est_cost_usd = round(s.est_cost_usd + rec.est_cost_usd, 6)
            s.free_tokens_used += rec.free_tokens_used
            s.voucher_tokens_used += rec.voucher_tokens_used
        return out

    def print_session_summary(self) -> str:
        """Render (and return) a human-readable running session summary."""
        lines = [
            "=== LLM session summary (LOCAL ESTIMATE — verify on Qwen Cloud Analytics/Usage) ==="
        ]
        total_cost = 0.0
        for model, s in self.summary().items():
            total_cost += s.est_cost_usd
            lines.append(
                f"{model}: {s.calls} calls | in={s.input_tokens} out={s.output_tokens} tok"
                f" | est ${s.est_cost_usd:.4f} | free={s.free_tokens_used}"
                f" voucher={s.voucher_tokens_used}"
                f" | free remaining≈{s.free_tokens_remaining}"
            )
        lines.append(f"TOTAL est cost: ${total_cost:.4f}")
        text = "\n".join(lines)
        print(text)
        return text
