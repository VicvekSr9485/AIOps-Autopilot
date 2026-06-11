# Recorded mock fixtures

Each fixture is `<key>.json` where `key` is the first 16 hex chars of
`sha256(json.dumps({"model": ..., "messages": ...}, sort_keys=True))`.

Schema:

```json
{"text": "recorded completion text", "input_tokens": 123, "output_tokens": 45}
```

With `AUTOPILOT_MOCK_LLM=1`, `QwenClient` replays a matching fixture; if none
matches it synthesizes a deterministic fallback response. Tests never hit the
network. Override the lookup directory with `AUTOPILOT_FIXTURES_DIR`.
