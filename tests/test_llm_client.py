import json

import pytest

from autopilot.config import load_llm_config
from autopilot.llm.client import QwenClient, RunTokenCapExceeded, _fixture_key

MESSAGES = [{"role": "user", "content": "Service checkout-api is returning 503s. Why?"}]


def test_mock_mode_is_deterministic_and_offline():
    client = QwenClient()
    assert client.config.mock_mode is True
    assert client._client is None  # no network client was ever constructed

    r1 = client.complete("reasoning", MESSAGES, step="root_cause")
    r2 = QwenClient().complete("reasoning", MESSAGES, step="root_cause")

    assert r1.mocked is True
    assert r1.model == "qwen3.7-max"  # role tiering: reasoning -> max
    assert (r1.text, r1.input_tokens, r1.output_tokens) == (
        r2.text,
        r2.input_tokens,
        r2.output_tokens,
    )


def test_default_role_uses_plus_model():
    r = QwenClient().complete("default", MESSAGES, step="triage")
    assert r.model == "qwen3.7-plus"


def test_fixture_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_FIXTURES_DIR", str(tmp_path))
    config = load_llm_config()
    model = config.model_by_role["default"]
    key = _fixture_key(model, MESSAGES)
    fixture = {"text": "recorded: OOM in checkout-api", "input_tokens": 50, "output_tokens": 10}
    (tmp_path / f"{key}.json").write_text(json.dumps(fixture))

    r = QwenClient(config=config).complete("default", MESSAGES, step="triage")
    assert r.text == "recorded: OOM in checkout-api"
    assert (r.input_tokens, r.output_tokens) == (50, 10)


def test_run_token_cap_refuses_before_the_call(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_RUN_TOKEN_CAP", "20")
    client = QwenClient(config=load_llm_config())
    client.complete("default", MESSAGES, step="one")  # spends >20 mock tokens
    spent_after_first = len(client.meter.records)

    with pytest.raises(RunTokenCapExceeded, match="run token cap 20"):
        client.complete("default", MESSAGES, step="two")
    # the refused call was never metered: it died before reaching the model
    assert len(client.meter.records) == spent_after_first


def test_no_cap_when_env_unset(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_RUN_TOKEN_CAP", raising=False)
    assert load_llm_config().run_token_cap is None
