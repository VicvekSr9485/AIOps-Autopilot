from autopilot.config import PRICING
from autopilot.llm.metering import CostMeter


def test_cost_math_on_synthetic_usage():
    meter = CostMeter(PRICING, {"qwen3.7-max": 1_000_000, "qwen3.7-plus": 1_000_000})

    rec = meter.record(
        model="qwen3.7-max", role="reasoning", step="root_cause",
        input_tokens=100_000, output_tokens=20_000,
    )
    # 0.1M * $1.25 + 0.02M * $3.75 = $0.125 + $0.075 = $0.200
    assert rec.est_cost_usd == 0.200
    assert rec.tier == "free"

    rec2 = meter.record(
        model="qwen3.7-plus", role="default", step="triage",
        input_tokens=250_000, output_tokens=50_000,
    )
    # 0.25M * $0.32 + 0.05M * $1.28 = $0.080 + $0.064 = $0.144
    assert rec2.est_cost_usd == 0.144


def test_free_tier_flips_to_voucher_when_budget_depletes():
    meter = CostMeter(PRICING, {"qwen3.7-plus": 1_000})

    first = meter.record(
        model="qwen3.7-plus", role="default", step="s1", input_tokens=600, output_tokens=200
    )
    assert first.tier == "free"
    assert (first.free_tokens_used, first.voucher_tokens_used) == (800, 0)
    assert meter.free_tokens_remaining("qwen3.7-plus") == 200

    # 500 tokens against 200 remaining: overflow -> expected to draw on the voucher.
    second = meter.record(
        model="qwen3.7-plus", role="default", step="s2", input_tokens=300, output_tokens=200
    )
    assert second.tier == "voucher"
    assert (second.free_tokens_used, second.voucher_tokens_used) == (200, 300)
    assert meter.free_tokens_remaining("qwen3.7-plus") == 0

    summary = meter.summary()["qwen3.7-plus"]
    assert summary.calls == 2
    assert summary.free_tokens_used == 1_000
    assert summary.voucher_tokens_used == 300


def test_session_summary_renders():
    meter = CostMeter(PRICING, {"qwen3.7-max": 1_000_000, "qwen3.7-plus": 1_000_000})
    meter.record(
        model="qwen3.7-max", role="reasoning", step="root_cause",
        input_tokens=1_000, output_tokens=500,
    )
    text = meter.print_session_summary()
    assert "qwen3.7-max" in text
    assert "Qwen Cloud Analytics/Usage" in text  # the authoritative-source caveat
