"""FastAPI entrypoint. Feature routes (incidents, HITL approvals) land here later."""

from fastapi import FastAPI

from autopilot import __version__

app = FastAPI(title="AIOps Autopilot", version=__version__)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
