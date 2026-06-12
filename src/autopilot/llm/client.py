"""Typed wrapper around Qwen Cloud's OpenAI-compatible endpoint.

- Model is selected by ROLE (see autopilot.config.MODEL_BY_ROLE), never hardcoded
  at call sites. "reasoning" (qwen3.7-max) is reserved for the root-cause step.
- Every call is metered through CostMeter (tokens, est. USD, free/voucher flag).
- Mock mode (AUTOPILOT_MOCK_LLM=1) is fully deterministic and never touches the
  network: it replays recorded fixtures keyed by a hash of (model, messages),
  falling back to a synthesized response with length-derived token counts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import structlog
from pydantic import BaseModel

from autopilot.config import LLMConfig, Role, load_llm_config
from autopilot.llm.metering import CostMeter, Tier

log = structlog.get_logger("autopilot.llm.client")

_PACKAGE_FIXTURES_DIR = Path(__file__).parent / "fixtures"


class LLMResponse(BaseModel):
    text: str
    model: str
    role: str
    step: str
    input_tokens: int
    output_tokens: int
    est_cost_usd: float
    tier: Tier
    mocked: bool


def _fixture_key(model: str, messages: list[dict[str, str]]) -> str:
    payload = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class RunTokenCapExceeded(RuntimeError):
    """The session-wide token cap (AUTOPILOT_RUN_TOKEN_CAP) was reached; the
    call was refused BEFORE touching the model — a runaway loop cannot drain
    the free tier or the voucher past the cap by more than one call."""


class QwenClient:
    """One instance per session/run; shares a CostMeter across the whole pipeline."""

    def __init__(self, config: LLMConfig | None = None, meter: CostMeter | None = None):
        self.config = config or load_llm_config()
        self.meter = meter or CostMeter(self.config.pricing, self.config.free_tier_tokens)
        self._client = None
        if not self.config.mock_mode:
            if not self.config.api_key:
                raise RuntimeError(
                    "DASHSCOPE_API_KEY is not set and mock mode is off. "
                    "Set AUTOPILOT_MOCK_LLM=1 for offline/deterministic runs."
                )
            from openai import OpenAI

            self._client = OpenAI(base_url=self.config.base_url, api_key=self.config.api_key)

    def complete(
        self,
        role: Role,
        messages: list[dict[str, str]],
        *,
        step: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Run a chat completion for a pipeline step. `step` labels telemetry."""
        model = self.config.model_by_role[role]
        cap = self.config.run_token_cap
        if cap is not None:
            spent = sum(r.input_tokens + r.output_tokens for r in self.meter.records)
            if spent >= cap:
                raise RunTokenCapExceeded(
                    f"run token cap {cap} reached (spent~{spent}); refusing the "
                    f"call for step {step!r}"
                )
        if self.config.mock_mode:
            text, input_tokens, output_tokens = self._mock_complete(model, messages)
        else:
            resp = self._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = resp.choices[0].message.content or ""
            input_tokens = resp.usage.prompt_tokens
            output_tokens = resp.usage.completion_tokens

        rec = self.meter.record(
            model=model,
            role=role,
            step=step,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return LLMResponse(
            text=text,
            model=model,
            role=role,
            step=step,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=rec.est_cost_usd,
            tier=rec.tier,
            mocked=self.config.mock_mode,
        )

    def _mock_complete(
        self, model: str, messages: list[dict[str, str]]
    ) -> tuple[str, int, int]:
        """Deterministic replay: recorded fixture if present, synthesized fallback otherwise."""
        key = _fixture_key(model, messages)
        fixtures_dir = (
            Path(self.config.fixtures_dir) if self.config.fixtures_dir else _PACKAGE_FIXTURES_DIR
        )
        fixture_path = fixtures_dir / f"{key}.json"
        if fixture_path.exists():
            data = json.loads(fixture_path.read_text())
            log.info("mock_fixture_replayed", key=key, path=str(fixture_path))
            return data["text"], data["input_tokens"], data["output_tokens"]

        # Fallback: deterministic synthetic response (~4 chars/token heuristic).
        text = f"[mock:{model}:{key}] deterministic synthetic response"
        input_tokens = max(1, sum(len(m.get("content", "")) for m in messages) // 4)
        output_tokens = max(1, len(text) // 4)
        log.info("mock_fallback_synthesized", key=key)
        return text, input_tokens, output_tokens
