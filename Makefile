VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install test test-sandbox lint run-api sandbox-up sandbox-down sandbox-reset bench

install:
	test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -e ".[dev]"

test:
	AUTOPILOT_MOCK_LLM=1 $(PY) -m pytest -m "not sandbox"

test-sandbox:
	AUTOPILOT_MOCK_LLM=1 $(PY) -m pytest -m sandbox

lint:
	$(PY) -m ruff check src tests

run-api:
	$(PY) -m uvicorn autopilot.api.app:app --reload --port 8080

sandbox-up:
	$(PY) -m autopilot.sandbox up

sandbox-down:
	$(PY) -m autopilot.sandbox down

sandbox-reset:
	$(PY) -m autopilot.sandbox reset

bench:
	$(PY) -m autopilot.benchmark
