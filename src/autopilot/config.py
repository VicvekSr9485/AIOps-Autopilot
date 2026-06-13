"""Central LLM configuration: endpoint, model tiering by role, pricing, free-tier budgets.

This is the single source of truth for which model serves which pipeline role and
what each model costs. Edit PRICING / MODEL_BY_ROLE here, nowhere else.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

DASHSCOPE_BASE_URL_DEFAULT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

Role = Literal["reasoning", "planning", "default"]

# Model tiering by role. "reasoning" (root-cause analysis) and "planning"
# (remediation planning) both run on the max tier — the real-model benchmark
# (REPORT-real.md) localized the pipeline's remediation losses to a too-cheap
# planner (wrong targets, hallucinated config values, sibling-runbook
# confusion), so the planner is promoted to qwen3.7-max. Everything else
# (triage enrichment is toolless/deterministic, summarization, verification)
# stays on "default". The role->model SET is still {max, plus}, so the
# benchmark's ModelConsistencyError check (constant pair within a run) holds.
MODEL_BY_ROLE: dict[Role, str] = {
    "reasoning": "qwen3.7-max",
    "planning": "qwen3.7-max",
    "default": "qwen3.7-plus",
}


class ModelPricing(BaseModel):
    """USD per million tokens. Editable seed values — sync with Qwen Cloud pricing page."""

    usd_per_m_input: float
    usd_per_m_output: float


PRICING: dict[str, ModelPricing] = {
    "qwen3.7-max": ModelPricing(usd_per_m_input=1.25, usd_per_m_output=3.75),
    "qwen3.7-plus": ModelPricing(usd_per_m_input=0.32, usd_per_m_output=1.28),
}

# Local ESTIMATE of the per-model free-token allowance. The authoritative figure
# is the Qwen Cloud console (Analytics / Usage page).
DEFAULT_FREE_TIER_TOKENS = 1_000_000


class LLMConfig(BaseModel):
    base_url: str = DASHSCOPE_BASE_URL_DEFAULT
    api_key: str = ""
    model_by_role: dict[str, str] = Field(default_factory=lambda: dict(MODEL_BY_ROLE))
    pricing: dict[str, ModelPricing] = Field(default_factory=lambda: dict(PRICING))
    free_tier_tokens: dict[str, int] = Field(
        default_factory=lambda: {m: DEFAULT_FREE_TIER_TOKENS for m in PRICING}
    )
    mock_mode: bool = False
    fixtures_dir: str | None = None
    # Hard per-run kill switch: once the session meter reaches this many total
    # tokens, the next complete() refuses BEFORE calling the model. None = off.
    run_token_cap: int | None = None
    # Resilience for the one external dependency (the Qwen Cloud endpoint).
    # Every live call gets a wall-clock timeout; transient failures (timeouts,
    # 429s, 5xx) are retried with exponential backoff up to max_retries. Both
    # are bounded so a flaky network can never hang or loop a deployed run.
    request_timeout_s: float = 30.0
    max_retries: int = 2


def load_llm_config() -> LLMConfig:
    """Build config from environment (.env is loaded by the app entrypoint, not here)."""
    free_default = int(os.environ.get("AUTOPILOT_FREE_TIER_TOKENS", DEFAULT_FREE_TIER_TOKENS))
    cap_raw = os.environ.get("AUTOPILOT_RUN_TOKEN_CAP", "")
    return LLMConfig(
        base_url=os.environ.get("DASHSCOPE_BASE_URL", DASHSCOPE_BASE_URL_DEFAULT),
        api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
        free_tier_tokens={m: free_default for m in PRICING},
        mock_mode=os.environ.get("AUTOPILOT_MOCK_LLM", "0") == "1",
        fixtures_dir=os.environ.get("AUTOPILOT_FIXTURES_DIR"),
        run_token_cap=int(cap_raw) if cap_raw else None,
        request_timeout_s=float(os.environ.get("AUTOPILOT_LLM_TIMEOUT_S", "30")),
        max_retries=int(os.environ.get("AUTOPILOT_LLM_MAX_RETRIES", "2")),
    )
