VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install test lint run-api sandbox-up sandbox-down bench

install:
	test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -e ".[dev]"

test:
	AUTOPILOT_MOCK_LLM=1 $(PY) -m pytest

lint:
	$(PY) -m ruff check src tests

run-api:
	$(PY) -m uvicorn autopilot.api.app:app --reload --port 8080

sandbox-up:
	docker compose -f sandbox/docker-compose.yml up -d

sandbox-down:
	docker compose -f sandbox/docker-compose.yml down -v

bench:
	$(PY) -m autopilot.benchmark
