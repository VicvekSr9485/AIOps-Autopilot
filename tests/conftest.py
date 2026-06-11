"""Hard guardrail: tests NEVER call the real LLM. Mock mode is forced for the
whole test session and any leaked API key is scrubbed from the environment."""

import os

os.environ["AUTOPILOT_MOCK_LLM"] = "1"
os.environ.pop("DASHSCOPE_API_KEY", None)
