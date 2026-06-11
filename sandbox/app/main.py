"""Sandbox HTTP service: talks to postgres (per-request connections), a downstream
HTTP dependency, and a redis-backed job queue. Emits JSON logs on stdout and a
JSON /metrics endpoint. This is the fault-injection target, not autopilot code.
"""

import json
import os
from datetime import datetime, timezone

import httpx
import psycopg
import redis as redislib
from fastapi import FastAPI, Response

CONFIG_PATH = os.environ.get("APP_CONFIG", "/app/config.json")
DB_DSN = os.environ.get("DATABASE_URL", "postgresql://app:app_pw@db:5432/autopilot")

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)  # read once at startup; config changes require a restart

R = redislib.Redis(host="queue", port=6379, socket_timeout=2, socket_connect_timeout=2)
COUNTERS = {"requests_total": 0, "errors_total": 0, "work_success_total": 0}

app = FastAPI()


def log(event: str, **fields) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), "service": "app", "event": event}
    record.update(fields)
    print(json.dumps(record), flush=True)


def db_ping() -> None:
    with psycopg.connect(DB_DSN, connect_timeout=2) as conn:
        conn.execute("SELECT 1")


@app.get("/healthz")
def healthz(response: Response):
    components = {}
    try:
        db_ping()
        components["db"] = {"ok": True}
    except Exception as e:
        components["db"] = {"ok": False, "error": str(e)[:300]}
        log("healthz_component_failed", component="db", error=str(e)[:300])
    try:
        R.ping()
        components["queue"] = {"ok": True}
    except Exception as e:
        components["queue"] = {"ok": False, "error": str(e)[:300]}
        log("healthz_component_failed", component="queue", error=str(e)[:300])

    ok = all(c["ok"] for c in components.values())
    response.status_code = 200 if ok else 503
    return {"status": "ok" if ok else "degraded", "components": components}


@app.get("/work")
def work(response: Response):
    COUNTERS["requests_total"] += 1

    if CONFIG.get("feature_mode") != "standard":
        COUNTERS["errors_total"] += 1
        log("work_failed", reason="invalid_feature_mode", feature_mode=CONFIG.get("feature_mode"))
        response.status_code = 500
        return {"error": f"invalid feature_mode '{CONFIG.get('feature_mode')}'"}

    try:
        with psycopg.connect(DB_DSN, connect_timeout=2) as conn:
            conn.execute("INSERT INTO items (payload) VALUES ('job')")
            count = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    except Exception as e:
        COUNTERS["errors_total"] += 1
        log("work_failed", reason="db_error", error=str(e)[:300])
        response.status_code = 500
        return {"error": f"db error: {str(e)[:300]}"}

    try:
        httpx.get(CONFIG["downstream_url"], timeout=CONFIG["downstream_timeout_s"])
    except httpx.TimeoutException as e:
        COUNTERS["errors_total"] += 1
        log("work_failed", reason="downstream_timeout", error=str(e)[:300])
        response.status_code = 504
        return {"error": "downstream_timeout"}
    except httpx.HTTPError as e:
        COUNTERS["errors_total"] += 1
        log("work_failed", reason="downstream_error", error=str(e)[:300])
        response.status_code = 502
        return {"error": f"downstream_error: {str(e)[:300]}"}

    try:
        R.lpush("jobs", json.dumps({"item": count}))
    except Exception as e:
        COUNTERS["errors_total"] += 1
        log("work_failed", reason="queue_error", error=str(e)[:300])
        response.status_code = 500
        return {"error": f"queue error: {str(e)[:300]}"}

    COUNTERS["work_success_total"] += 1
    log("work_done", items=count)
    return {"status": "done", "items": count}


@app.get("/metrics")
def metrics():
    out = dict(COUNTERS)
    out["captured_at"] = datetime.now(timezone.utc).isoformat()
    try:
        out["queue_depth"] = R.llen("jobs")
        out["jobs_processed"] = int(R.get("jobs_processed") or 0)
    except Exception:
        out["queue_depth"] = None
        out["jobs_processed"] = None
    return out
