"""Live proof that the deployed backend reaches Alibaba Cloud / Qwen Cloud.

This is the single, self-contained file linked as DEPLOYMENT PROOF. It performs
one real, metered chat completion against the Qwen Cloud (Model Studio /
DashScope) OpenAI-compatible inference endpoint — a hosted Alibaba Cloud service
on `*.aliyuncs.com` — and returns a typed report of everything that proves the
integration: the resolved Alibaba Cloud host and region, the role→model tiering
actually exercised, observed tokens, estimated cost, the free/voucher tier flag,
and round-trip latency.

It is the same `QwenClient` the pipeline uses, so a green result here means the
pipeline's one external dependency is reachable from wherever this runs (an
Alibaba Cloud ECS instance, in the documented deployment).

Three ways in, one code path:
- `run_self_check()` — importable; used by the `/api/cloud/selfcheck` route and
  the deploy smoke test.
- `python -m autopilot.cloud.qwen_live` — CLI; prints the JSON report and exits
  non-zero if the live call failed (so it is usable as a deploy health gate).
- Mock mode (`AUTOPILOT_MOCK_LLM=1`) short-circuits to a deterministic offline
  result flagged `mocked=true`, so importing/running this never spends tokens in
  tests or CI.
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlsplit

import structlog
from pydantic import BaseModel, computed_field

from autopilot.config import MODEL_BY_ROLE, load_llm_config
from autopilot.llm.client import LLMResponse, QwenCallError, QwenClient
from autopilot.models import utcnow

log = structlog.get_logger("autopilot.cloud.qwen_live")

# A deliberately tiny prompt — the point is to exercise the round-trip to
# Alibaba Cloud, not to spend tokens. Runs on the "default" (cheap) tier.
_PROBE_PROMPT = (
    "Reply with the single word OK to confirm connectivity. Do not explain."
)


class CloudSelfCheck(BaseModel):
    """Typed report of one round-trip to the Qwen Cloud endpoint."""

    ok: bool
    mocked: bool
    checked_at: str
    # What we reached (Alibaba Cloud / Qwen Cloud).
    endpoint: str
    cloud_host: str
    region: str
    # What we exercised.
    role: str
    model: str
    model_by_role: dict[str, str]
    # What it cost / how it behaved.
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    est_cost_usd: float | None = None
    tier: str | None = None
    sample_text: str | None = None
    error: str | None = None

    @computed_field  # serialized into the JSON response + CLI output
    @property
    def headline(self) -> str:
        """One unmistakable line so a MOCK run can never be mistaken for a real
        one (on camera, in logs, or in the API response)."""
        if self.mocked:
            return (
                "MOCK MODE — NOT a real Qwen Cloud call (AUTOPILOT_MOCK_LLM is set); "
                "this proves NOTHING about cloud connectivity"
            )
        if self.ok:
            tokens = (self.input_tokens or 0) + (self.output_tokens or 0)
            return (
                f"REAL Qwen Cloud round-trip (mocked=false) — {self.cloud_host} · "
                f"{self.region} · {self.model} · {tokens} tok · "
                f"${self.est_cost_usd} · {self.latency_ms}ms"
            )
        return f"REAL mode — Qwen Cloud call FAILED: {self.error}"


def region_for_host(host: str) -> str:
    """Best-effort human label for the Alibaba Cloud region behind a DashScope host."""
    h = host.lower()
    if "dashscope-intl" in h or "-intl." in h:
        return "alibaba-cloud-intl (Singapore)"
    if "dashscope" in h and "aliyuncs" in h:
        return "alibaba-cloud-cn (Beijing)"
    if "aliyuncs" in h:
        return "alibaba-cloud (region undetermined)"
    return "non-alibaba/custom endpoint"


def run_self_check(role: str = "default", prompt: str = _PROBE_PROMPT) -> CloudSelfCheck:
    """Make one real metered Qwen Cloud call and report the outcome.

    Never raises for an expected failure (no key, network/endpoint error): those
    come back as `ok=false` with `error` set, so the API route and smoke test can
    surface a clean 200-with-status rather than a 500. Mock mode returns a
    deterministic offline result without touching the network.
    """
    config = load_llm_config()
    host = urlsplit(config.base_url).hostname or ""
    base = CloudSelfCheck(
        ok=False,
        mocked=config.mock_mode,
        checked_at=utcnow().isoformat(),
        endpoint=config.base_url,
        cloud_host=host,
        region=region_for_host(host),
        role=role,
        model=MODEL_BY_ROLE.get(role, MODEL_BY_ROLE["default"]),
        model_by_role=dict(MODEL_BY_ROLE),
    )

    if config.mock_mode:
        # Offline/deterministic — proves wiring without spending tokens.
        base.ok = True
        base.sample_text = "[mock] live check skipped (AUTOPILOT_MOCK_LLM=1)"
        base.tier = "free"
        base.latency_ms = 0.0
        base.input_tokens = 0
        base.output_tokens = 0
        base.est_cost_usd = 0.0
        return base

    if not config.api_key:
        base.error = "DASHSCOPE_API_KEY is not set; cannot reach Qwen Cloud"
        return base

    client = QwenClient(config=config)
    t0 = time.perf_counter()
    try:
        resp: LLMResponse = client.complete(
            role,  # type: ignore[arg-type]
            [{"role": "user", "content": prompt}],
            step="cloud_selfcheck",
            max_tokens=16,
        )
    except QwenCallError as e:
        base.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        base.error = str(e)
        log.warning("cloud_selfcheck_failed", error=str(e)[:300])
        return base

    base.ok = True
    base.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    base.model = resp.model
    base.input_tokens = resp.input_tokens
    base.output_tokens = resp.output_tokens
    base.est_cost_usd = resp.est_cost_usd
    base.tier = resp.tier
    base.sample_text = resp.text[:200]
    log.info(
        "cloud_selfcheck_ok",
        host=host,
        region=base.region,
        model=resp.model,
        tokens=resp.input_tokens + resp.output_tokens,
        latency_ms=base.latency_ms,
    )
    return base


def print_banner(result: CloudSelfCheck) -> None:
    """Print a loud, camera-proof banner: green REAL, red MOCK/FAILED."""
    real_ok = (not result.mocked) and result.ok
    mark = "✅" if real_ok else ("⚠️ " if result.mocked else "❌")
    bar = "═" * 72
    body = f"\n{bar}\n  {mark}  {result.headline}\n{bar}\n"
    if sys.stdout.isatty():
        color = "\033[1;92m" if real_ok else "\033[1;91m"  # bold green / bold red
        body = f"{color}{body}\033[0m"
    print(body)


def main() -> int:
    result = run_self_check()
    print_banner(result)
    print(result.model_dump_json(indent=2))
    print_banner(result)  # repeat AFTER the JSON so it's the last thing on screen
    # Non-zero exit on a real failure so this doubles as a deploy health gate.
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
