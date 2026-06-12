"""Server-side injection context for the MCP tool surface.

Values that are deterministic and session-known — the sandbox namespace, the
active incident id, fixed action targets — are NEVER model-supplied parameters.
They are injected server-side: the sandbox namespace via the controller bound
at server build time, fixed targets hardcoded in the tool body, and per-run
values through this context object, which the pipeline mutates as a run
progresses. Model-facing signatures expose only genuine decisions.
"""

from __future__ import annotations

from pydantic import BaseModel


class RunContext(BaseModel):
    """Mutable per-session context shared with the servers at build time.

    The pipeline sets `incident_id` when a run starts; tools that need it read
    it from here instead of declaring a parameter the model could hallucinate.
    """

    incident_id: str | None = None

    def require_incident_id(self) -> str:
        if not self.incident_id:
            raise RuntimeError(
                "no active incident bound to this session; the pipeline must set "
                "RunContext.incident_id before recording outcomes"
            )
        return self.incident_id
