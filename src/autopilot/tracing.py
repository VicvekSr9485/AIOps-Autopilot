"""Structlog spans for pipeline stages: started/completed/failed events with a
duration, all carrying the `step` label our telemetry convention requires."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

import structlog

log = structlog.get_logger("autopilot.span")


@contextmanager
def span(step: str, **fields) -> Iterator[None]:
    t0 = time.perf_counter()
    log.info(f"{step}_started", step=step, **fields)
    try:
        yield
    except Exception as e:
        log.error(
            f"{step}_failed", step=step, error=str(e)[:300],
            duration_ms=round((time.perf_counter() - t0) * 1000, 1), **fields,
        )
        raise
    log.info(
        f"{step}_completed", step=step,
        duration_ms=round((time.perf_counter() - t0) * 1000, 1), **fields,
    )
