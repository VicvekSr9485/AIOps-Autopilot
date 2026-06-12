"""Shared structured-output discipline for LLM stages: STRICT-JSON completion
parsed into a Pydantic payload, with bounded retry (validation errors fed back)
and a hard token cap — never an unbounded loop against a paid model."""

from __future__ import annotations

from typing import TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from autopilot.config import Role
from autopilot.llm.client import QwenClient

log = structlog.get_logger("autopilot.pipeline.structured")

PayloadT = TypeVar("PayloadT", bound=BaseModel)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TOKEN_CAP = 16_000


class StructuredOutputError(RuntimeError):
    """No schema-valid payload within the attempt/token caps."""


def extract_json(text: str) -> str:
    """Tolerate markdown fences / surrounding prose around the JSON object."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return text[start : end + 1]


def complete_structured(
    client: QwenClient,
    role: Role,
    messages: list[dict[str, str]],
    payload_model: type[PayloadT],
    *,
    step: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    token_cap: int = DEFAULT_TOKEN_CAP,
) -> tuple[PayloadT, int]:
    """Run a completion until `payload_model` validates. Returns (payload,
    tokens_spent). Every attempt is metered under `step` by the client."""
    msgs = list(messages)
    tokens_spent = 0
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        resp = client.complete(role, msgs, step=step)
        tokens_spent += resp.input_tokens + resp.output_tokens
        try:
            payload = payload_model.model_validate_json(extract_json(resp.text))
            log.info(
                "structured_output_ok", step=step, attempt=attempt,
                payload=payload_model.__name__, tokens_spent=tokens_spent,
            )
            return payload, tokens_spent
        except (ValueError, ValidationError) as e:
            last_error = str(e)[:300]
            log.warning(
                "structured_output_retry", step=step, attempt=attempt,
                error=last_error,
            )
            if tokens_spent >= token_cap:
                raise StructuredOutputError(
                    f"token cap exceeded ({tokens_spent} >= {token_cap}) after "
                    f"{attempt} attempt(s); last parse error: {last_error}"
                ) from None
            msgs = msgs + [
                {"role": "assistant", "content": resp.text[:1000]},
                {
                    "role": "user",
                    "content": (
                        f"Your previous response failed validation: {last_error}. "
                        "Respond again with ONLY the strict JSON object — no "
                        "prose, no markdown."
                    ),
                },
            ]

    raise StructuredOutputError(
        f"no valid {payload_model.__name__} after {max_attempts} attempts "
        f"({tokens_spent} tokens spent); last parse error: {last_error}"
    )
