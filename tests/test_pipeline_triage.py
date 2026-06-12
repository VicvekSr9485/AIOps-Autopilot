"""Triage/root-cause stage tests (mock mode, offline): schema-valid ranked
hypotheses over all 5 scenarios, stage-scoped tools only (never Infra/Ops),
bounded retry on malformed model output, per-call cost metering, token cap."""

from __future__ import annotations

import json

import pytest

from autopilot.config import LLMConfig
from autopilot.llm.client import QwenClient
from autopilot.mcp_servers.infra import build_infra_server
from autopilot.mcp_servers.knowledge import build_knowledge_server
from autopilot.mcp_servers.store import KnowledgeStore
from autopilot.mcp_servers.telemetry import build_telemetry_server
from autopilot.models import TriageResult
from autopilot.pipeline.ingest import ingest
from autopilot.pipeline.summarize import summarize_telemetry
from autopilot.pipeline.triage import TriageError, run_triage
from scenario_data import FAULT_IDS, scenario_capture
from test_mcp_servers import FakeController

pytestmark = pytest.mark.anyio


class ScriptedClient(QwenClient):
    """Mock-mode client whose responses are scripted per call; metering stays
    fully real (every attempt records tokens/cost/tier)."""

    def __init__(self, responses: list[str], tokens: tuple[int, int] = (800, 200)):
        super().__init__(config=LLMConfig(mock_mode=True))
        self._queue = list(responses)
        self._tokens = tokens
        self.prompts: list[list[dict]] = []

    def _mock_complete(self, model, messages):
        self.prompts.append(messages)
        text = self._queue.pop(0) if self._queue else "definitely not json"
        return text, *self._tokens


def valid_triage_json() -> str:
    # Deliberately mis-ordered (low confidence first) and with junk the stage
    # must sanitize: an extra field and an evidence item of unknown kind.
    return json.dumps({
        "hypotheses": [
            {"cause": "transient network blip", "confidence": 0.2,
             "evidence": [{"kind": "trace", "pointer": "x"}],  # unknown kind: dropped
             "reasoning_summary": "weak alternative"},
            {"cause": "db connection slots exhausted by idle sessions",
             "confidence": 0.85,
             "evidence": [{"kind": "log", "pointer": "log:app",
                           "excerpt": "remaining connection slots are reserved"}],
             "reasoning_summary": "errors match pool exhaustion signature",
             "made_up_field": True},
        ]
    })


def build_servers(ctrl: FakeController) -> dict:
    return {
        "telemetry": build_telemetry_server(ctrl),
        "infra": build_infra_server(ctrl),
        "knowledge": build_knowledge_server(store=KnowledgeStore(":memory:")),
    }


@pytest.mark.parametrize("fault_id", FAULT_IDS)
async def test_triage_ranks_hypotheses_per_scenario(fault_id):
    log_text, snapshots = scenario_capture(fault_id)
    incident = ingest(log_text, snapshots)
    ctrl = FakeController(snapshots=list(snapshots))
    client = ScriptedClient([valid_triage_json()])

    result = await run_triage(incident, build_servers(ctrl), client)

    assert TriageResult.model_validate(result.model_dump())
    assert result.incident_id == incident.id  # injected, never model-supplied
    confidences = [h.confidence for h in result.hypotheses]
    assert confidences == sorted(confidences, reverse=True)  # re-ranked server-side
    assert result.top.cause.startswith("db connection slots")
    assert result.top.evidence and result.top.evidence[0].kind == "log"
    assert all(h.incident_id == incident.id for h in result.hypotheses)
    # the unknown-kind evidence item was dropped, not crashed on
    assert all(e.kind in ("alert", "log", "metric")
               for h in result.hypotheses for e in h.evidence)


async def test_triage_uses_only_scoped_tools_and_reasoning_tier():
    log_text, snapshots = scenario_capture("db_pool_exhaustion")
    incident = ingest(log_text, snapshots)
    ctrl = FakeController(snapshots=list(snapshots))
    client = ScriptedClient([valid_triage_json()])

    await run_triage(incident, build_servers(ctrl), client)

    # Infra/Ops never reached: no mutation recorded on the controller.
    assert ctrl.calls == []
    prompt_blob = json.dumps(client.prompts[0])
    # Knowledge tools were used: seeded runbook content reached the prompt.
    assert "connection slots" in prompt_blob.lower()
    # Telemetry tools were used: live re-check section present.
    assert "LIVE RE-CHECK" in prompt_blob
    # No infra tool surface leaked into the prompt.
    for name in ("restart_service", "scale_service", "apply_config", "rollback"):
        assert name not in prompt_blob
    # ONLY reasoning-tier calls, with the step label, metered.
    assert [r.role for r in client.meter.records] == ["reasoning"]
    assert client.meter.records[0].model == "qwen3.7-max"
    assert client.meter.records[0].step == "triage.root_cause"


async def test_retry_on_malformed_output_then_success():
    log_text, snapshots = scenario_capture("bad_config_rollout")
    incident = ingest(log_text, snapshots)
    ctrl = FakeController(snapshots=list(snapshots))
    client = ScriptedClient(["alas, I am prose, not JSON", valid_triage_json()])

    result = await run_triage(incident, build_servers(ctrl), client)

    assert result.top.confidence == 0.85
    assert len(client.meter.records) == 2  # both attempts metered
    retry_prompt = json.dumps(client.prompts[1])
    assert "failed validation" in retry_prompt  # error fed back to the model


async def test_retry_exhaustion_raises_after_hard_attempt_cap():
    log_text, snapshots = scenario_capture("downstream_timeout")
    incident = ingest(log_text, snapshots)
    ctrl = FakeController(snapshots=list(snapshots))
    client = ScriptedClient(["nope", '{"hypotheses": []}', "{broken json"])

    with pytest.raises(TriageError, match="after 3 attempts"):
        await run_triage(incident, build_servers(ctrl), client, max_attempts=3)
    assert len(client.meter.records) == 3  # hard cap on reasoning-tier calls


async def test_token_cap_aborts_retries():
    log_text, snapshots = scenario_capture("expired_credential")
    incident = ingest(log_text, snapshots)
    ctrl = FakeController(snapshots=list(snapshots))
    client = ScriptedClient(["not json at all"], tokens=(15_000, 2_000))

    with pytest.raises(TriageError, match="token cap exceeded"):
        await run_triage(incident, build_servers(ctrl), client, token_cap=16_000)
    assert len(client.meter.records) == 1  # aborted before a second attempt


async def test_cost_meter_records_every_attempt_with_tier():
    log_text, snapshots = scenario_capture("queue_consumer_stall")
    incident = ingest(log_text, snapshots)
    ctrl = FakeController(snapshots=list(snapshots))
    client = ScriptedClient(["garbage one", "garbage two", valid_triage_json()])

    await run_triage(incident, build_servers(ctrl), client)

    records = client.meter.records
    assert len(records) == 3
    for rec in records:
        assert rec.step == "triage.root_cause"
        assert rec.input_tokens > 0 and rec.output_tokens > 0
        assert rec.est_cost_usd > 0
        assert rec.tier in ("free", "voucher")
    assert client.meter.free_tokens_remaining("qwen3.7-max") < 1_000_000


# ------------------------------------------------------------- summarization


def test_summarize_telemetry_bounds_bulky_input():
    log_text, snapshots = scenario_capture("db_pool_exhaustion")
    # Inflate to thousands of lines: summary must stay bounded (cost rule).
    incident = ingest("\n".join([log_text] * 300), snapshots)
    assert len(incident.telemetry.logs) > 3000

    summary = summarize_telemetry(incident.telemetry)
    assert len(summary) < 4000
    assert "x3600" in summary or "x1200" in summary  # dedup counts, not raw lines
    assert "remaining connection slots" in summary  # signal survives compaction


def test_summarize_telemetry_handles_missing_sections():
    incident = ingest("", [])
    summary = summarize_telemetry(incident.telemetry)
    assert "METRICS: none captured" in summary
    assert "LOGS: none captured" in summary
