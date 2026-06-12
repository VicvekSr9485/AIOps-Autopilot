"""Shared guardrails for the MCP tool surface.

Sandbox-only enforcement: any tool that targets a service must validate it
against SANDBOX_SERVICES before doing anything. The allowlist is kept in sync
with sandbox/docker-compose.yml by a test (tests/test_mcp_servers.py).
"""

from __future__ import annotations

SANDBOX_COMPOSE_PROJECT = "autopilot-sandbox"

# Must match the services of sandbox/docker-compose.yml exactly (test-enforced).
SANDBOX_SERVICES = frozenset({"app", "worker", "downstream", "db", "queue"})


class SandboxViolation(ValueError):
    """Raised when a tool is asked to act outside the sandbox compose project."""


def ensure_sandbox_service(service: str) -> str:
    name = (service or "").strip()
    if name not in SANDBOX_SERVICES:
        raise SandboxViolation(
            f"refusing to act on {service!r}: not a service of compose project "
            f"'{SANDBOX_COMPOSE_PROJECT}' (allowed: {sorted(SANDBOX_SERVICES)})"
        )
    return name


def truncate(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
