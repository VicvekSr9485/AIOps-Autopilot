"""Queue consumer: pops jobs from redis and bumps the processed counter."""

import json
import signal
import sys
import time
from datetime import datetime, timezone

import redis as redislib

R = redislib.Redis(host="queue", port=6379, socket_timeout=5, socket_connect_timeout=2)


def log(event: str, **fields) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), "service": "worker", "event": event}
    record.update(fields)
    print(json.dumps(record), flush=True)


def _on_sigterm(signum, frame) -> None:
    # observable shutdown trace: a scaled-away/stopped consumer is
    # distinguishable in logs from one that is merely frozen
    log("worker_shutdown", signal="SIGTERM")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _on_sigterm)
    log("worker_started")
    while True:
        try:
            job = R.brpop("jobs", timeout=2)
            if job is not None:
                R.incr("jobs_processed")
                log("job_processed", payload=job[1].decode()[:100])
        except Exception as e:
            log("worker_error", error=str(e)[:300])
            time.sleep(1)


if __name__ == "__main__":
    main()
