"""Deterministic control surface for the sandboxed docker-compose stack.

HARD GUARDRAIL: every operation here is scoped to the compose project
`autopilot-sandbox` via its compose file. Nothing in this module (or anything
built on it) may touch the host or external systems.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel

log = structlog.get_logger("autopilot.sandbox")

SANDBOX_DIR = Path(__file__).resolve().parents[3] / "sandbox"
COMPOSE_FILE = SANDBOX_DIR / "docker-compose.yml"
RUNTIME_DIR = SANDBOX_DIR / "runtime"
APP_CONFIG_PATH = RUNTIME_DIR / "app-config.json"
DEFAULT_APP_CONFIG_PATH = SANDBOX_DIR / "app" / "config.default.json"

APP_BASE_URL = "http://localhost:8088"


class ProbeSnapshot(BaseModel):
    """One observation of the stack from the outside (no ground truth)."""

    captured_at: datetime
    healthz_status: int | None = None  # None = could not connect at all
    healthz_body: dict | None = None
    work_status: int | None = None
    work_body: dict | None = None
    metrics: dict | None = None

    @property
    def healthy(self) -> bool:
        return self.healthz_status == 200 and self.work_status == 200


class RequestObservation(BaseModel):
    """One timed HTTP request against the sandbox app (trace-style observation)."""

    path: str
    started_at: datetime
    status: int | None = None  # None = request failed before a response
    latency_ms: float
    body: dict | str | None = None
    error: str | None = None


class SandboxController:
    def __init__(self, compose_file: Path = COMPOSE_FILE, base_url: str = APP_BASE_URL):
        self.compose_file = compose_file
        self.base_url = base_url

    # ----------------------------------------------------------------- compose plumbing

    def _compose(self, *args: str, check: bool = True) -> str:
        cmd = ["docker", "compose", "-f", str(self.compose_file), *args]
        log.info("sandbox_compose", step="sandbox", args=list(args))
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"compose {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr[:500]}"
            )
        return proc.stdout

    # ----------------------------------------------------------------- lifecycle

    def up(self) -> None:
        self.ensure_app_config()
        self._compose("up", "-d", "--build", "--wait")

    def down(self) -> None:
        self._compose("down", "-v", "--remove-orphans")

    def reset(self) -> None:
        """Down (wiping volumes), restore canonical app config, fresh up + seed."""
        self.down()
        self.write_app_config(self.default_app_config())
        self.up()

    def restart(self, service: str) -> None:
        self._compose("restart", service)

    def scale(self, service: str, replicas: int) -> None:
        """Scale a compose service. NOTE: services pin container_name, so compose
        rejects replicas > 1; 0 stops the service, 1 converges to running."""
        self._compose(
            "up", "-d", "--no-build", "--no-recreate",
            "--scale", f"{service}={replicas}", service,
        )

    def pause(self, service: str) -> None:
        self._compose("pause", service)

    def unpause(self, service: str) -> None:
        self._compose("unpause", service)

    # ----------------------------------------------------------------- in-container ops

    def exec(self, service: str, *cmd: str, detach: bool = False) -> str:
        args = ["exec"]
        if detach:
            args.append("--detach")
        args += ["-T", service, *cmd]
        return self._compose(*args)

    def psql(self, sql: str, user: str = "autopilot") -> str:
        """Run SQL inside the db container over the local socket (trust auth)."""
        return self.exec("db", "psql", "-U", user, "-d", "autopilot", "-tAc", sql)

    # ----------------------------------------------------------------- app config

    @staticmethod
    def default_app_config() -> dict:
        return json.loads(DEFAULT_APP_CONFIG_PATH.read_text())

    def read_app_config(self) -> dict:
        self.ensure_app_config()
        return json.loads(APP_CONFIG_PATH.read_text())

    def ensure_app_config(self) -> None:
        if not APP_CONFIG_PATH.exists():
            self.write_app_config(self.default_app_config())

    def write_app_config(self, config: dict) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        APP_CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
        log.info("sandbox_app_config_written", step="sandbox", config=config)

    # ----------------------------------------------------------------- observation

    def logs(self, since: datetime | None = None) -> str:
        args = ["logs", "--no-color", "-t"]
        if since is not None:
            args += ["--since", since.astimezone(UTC).isoformat()]
        try:
            return self._compose(*args)
        except RuntimeError:
            return self._compose("logs", "--no-color", "-t")  # older compose: no --since

    def timed_request(self, path: str) -> RequestObservation:
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        try:
            with httpx.Client(base_url=self.base_url, timeout=10.0) as client:
                resp = client.get(path)
            latency_ms = (time.perf_counter() - t0) * 1000
            try:
                body: dict | str = resp.json()
            except ValueError:
                body = resp.text[:500]
            return RequestObservation(
                path=path, started_at=started, status=resp.status_code,
                latency_ms=latency_ms, body=body,
            )
        except Exception as e:
            return RequestObservation(
                path=path, started_at=started, status=None,
                latency_ms=(time.perf_counter() - t0) * 1000, error=str(e)[:300],
            )

    def probe(self) -> ProbeSnapshot:
        snap = ProbeSnapshot(captured_at=datetime.now(UTC))
        with httpx.Client(base_url=self.base_url, timeout=4.0) as client:
            for endpoint, status_field, body_field in [
                ("/healthz", "healthz_status", "healthz_body"),
                ("/work", "work_status", "work_body"),
                ("/metrics", None, "metrics"),
            ]:
                try:
                    resp = client.get(endpoint)
                    if status_field:
                        setattr(snap, status_field, resp.status_code)
                    setattr(snap, body_field, resp.json())
                except Exception:
                    pass  # fields stay None: "unreachable" is itself a signal
        return snap
