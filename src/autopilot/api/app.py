"""FastAPI entrypoint for the demo surface.

Endpoints drive and visualize the agent loop offline (mock world): launch a
scenario, watch the reasoning trace stream live (SSE), resolve an escalated
incident at the HITL gate, and read the benchmark comparison. Every route is
typed against autopilot.api.schemas so the dashboard — and the tests — get a
validated contract.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from autopilot import __version__
from autopilot.api.runmanager import TERMINAL, get_manager
from autopilot.api.schemas import (
    ApprovalView,
    CreateRunRequest,
    DecisionRequest,
    RunDetail,
    RunSummary,
    ScenarioInfo,
)
from autopilot.benchmark.metrics import BenchmarkReport
from autopilot.cloud.qwen_live import CloudSelfCheck, run_self_check
from autopilot.config import load_llm_config
from autopilot.models import RemediationStep
from autopilot.pipeline.hitl import HumanDecision


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Print a loud LLM-mode banner at startup so a MOCK deployment is obvious in
    the container logs and can never be mistaken for a real one on camera."""
    cfg = load_llm_config()
    if cfg.mock_mode:
        mode = ("⚠️  MOCK (AUTOPILOT_MOCK_LLM=1) — cloud calls SIMULATED; "
                "/api/cloud/selfcheck reports mocked=true")
    else:
        mode = f"✅ REAL — cloud calls hit Qwen Cloud at {cfg.base_url}"
    bar = "═" * 72
    print(f"\n{bar}\n  AIOps Autopilot backend · LLM MODE: {mode}\n{bar}\n", flush=True)
    yield


app = FastAPI(title="AIOps Autopilot", version=__version__, lifespan=_lifespan)

# The dashboard dev server (Vite, :5173) calls this API on :8080.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Prefer the latest real validation artifacts; fall back gracefully.
_BENCHMARK_CANDIDATES = [
    _REPO_ROOT / "benchmark_results_real_v2" / "results.json",
    _REPO_ROOT / "benchmark_results_real" / "results.json",
    _REPO_ROOT / "benchmark_results" / "results.json",
]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/cloud/selfcheck", response_model=CloudSelfCheck)
def cloud_selfcheck() -> CloudSelfCheck:
    """Deployment proof: make one live, metered round-trip to the Qwen Cloud
    (Alibaba Cloud) inference endpoint and report what was reached. In mock mode
    this returns a deterministic offline result (mocked=true) and spends nothing.
    Always 200 — a connectivity failure surfaces as ok=false with `error`."""
    return run_self_check()


@app.get("/api/scenarios", response_model=list[ScenarioInfo])
def list_scenarios() -> list[ScenarioInfo]:
    return get_manager().scenarios()


@app.get("/api/runs", response_model=list[RunSummary])
def list_runs() -> list[RunSummary]:
    return get_manager().list_runs()


@app.post("/api/runs", response_model=RunSummary, status_code=201)
def create_run(req: CreateRunRequest) -> RunSummary:
    try:
        run = get_manager().start(req.fault_id)
    except KeyError:
        raise HTTPException(404, f"unknown fault_id {req.fault_id!r}") from None
    return run.summary()


def _require_run(run_id: str):
    run = get_manager().get(run_id)
    if run is None:
        raise HTTPException(404, f"unknown run {run_id!r}")
    return run


@app.get("/api/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str) -> RunDetail:
    _, detail = _require_run(run_id).snapshot()
    return detail


@app.get("/api/runs/{run_id}/approval", response_model=ApprovalView)
def get_approval(run_id: str) -> ApprovalView:
    run = _require_run(run_id)
    if run.approval is None:
        raise HTTPException(409, "run is not awaiting a decision")
    return run.approval


@app.post("/api/runs/{run_id}/decision", response_model=RunSummary)
def submit_decision(run_id: str, body: DecisionRequest) -> RunSummary:
    run = _require_run(run_id)
    if run.status != "awaiting_approval" or run._pending_proposal is None:
        raise HTTPException(409, "run is not awaiting a decision")

    edited = None
    if body.action == "edit":
        steps = run._pending_proposal.steps
        if body.steps is not None:
            steps = [
                RemediationStep(
                    order=i + 1, action=s.action, target=s.target,
                    command=json.dumps(s.params, sort_keys=True) if s.params else None,
                    expected_effect=s.expected_effect)
                for i, s in enumerate(body.steps)
            ]
        edited = run._pending_proposal.model_copy(update={"steps": steps})

    run.submit_decision(HumanDecision(action=body.action,
                                      edited_proposal=edited, note=body.note))
    return run.summary()


@app.get("/api/runs/{run_id}/stream")
def stream_run(run_id: str) -> StreamingResponse:
    run = _require_run(run_id)

    async def gen():
        last_rev = -1
        # Emit immediately, then on every change, with periodic heartbeats so
        # proxies don't drop an idle connection. Close once terminal.
        while True:
            rev, detail = run.snapshot()
            if rev != last_rev:
                last_rev = rev
                yield f"data: {detail.model_dump_json()}\n\n"
                if detail.status in TERMINAL:
                    yield "event: done\ndata: {}\n\n"
                    return
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/benchmark", response_model=BenchmarkReport)
def benchmark() -> BenchmarkReport:
    for path in _BENCHMARK_CANDIDATES:
        if path.exists():
            return BenchmarkReport.model_validate_json(path.read_text())
    raise HTTPException(503, "no benchmark results available; run `make bench`")
