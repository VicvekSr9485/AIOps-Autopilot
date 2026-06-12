"""Knowledge MCP server: runbook/past-incident retrieval + outcome recording,
backed by the local SQLite vector store (see store.py). Seeded with the runbook
corpus on build; record_outcome upserts by incident id so re-recording the same
incident updates rather than duplicates.

NOTE: no `from __future__ import annotations` here — FastMCP 1.9.4 inspects real
(non-string) annotations when registering tools.
"""

from typing import Annotated

import structlog
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from autopilot.mcp_servers.guards import truncate
from autopilot.mcp_servers.runbooks import SEED_RUNBOOKS
from autopilot.mcp_servers.store import KnowledgeStore

log = structlog.get_logger("autopilot.mcp.knowledge")

KMax = Annotated[int, Field(ge=1, le=10)]


class RunbookHit(BaseModel):
    slug: str
    title: str
    score: float
    excerpt: str
    tags: list[str]


class SearchRunbooksResult(BaseModel):
    query: str
    results: list[RunbookHit]


class IncidentHit(BaseModel):
    incident_id: str
    title: str
    score: float
    excerpt: str


class SearchIncidentsResult(BaseModel):
    query: str
    results: list[IncidentHit]


class RecordOutcomeResult(BaseModel):
    incident_id: str
    doc_id: int
    created: bool  # False = an existing record for this incident was updated


def seed_store(store: KnowledgeStore) -> None:
    """Idempotent: upserts every seed runbook by slug."""
    for rb in SEED_RUNBOOKS:
        store.add("runbook", rb.slug, rb.title, rb.body, rb.tags)
    log.info("runbooks_seeded", step="mcp.knowledge", count=len(SEED_RUNBOOKS))


def build_knowledge_server(store: KnowledgeStore | None = None, seed: bool = True) -> FastMCP:
    store = store or KnowledgeStore()
    if seed:
        seed_store(store)
    mcp = FastMCP(
        "autopilot-knowledge",
        instructions="Operational knowledge for the autopilot agent: searchable "
        "runbooks and past-incident records, plus outcome recording so future "
        "triage can retrieve what worked.",
    )

    @mcp.tool()
    def search_runbooks(query: str, k: KMax = 3) -> SearchRunbooksResult:
        """Find the runbooks most relevant to a symptom/hypothesis description.
        Returns scored excerpts (cosine similarity, higher = more relevant)."""
        hits = [
            RunbookHit(slug=d.key, title=d.title, score=round(d.score, 4),
                       excerpt=truncate(d.body, 600), tags=d.tags)
            for d in store.search(query, kind="runbook", k=k)
        ]
        log.info("mcp_tool", step="mcp.knowledge", tool="search_runbooks",
                 query=truncate(query, 120), results=len(hits))
        return SearchRunbooksResult(query=query, results=hits)

    @mcp.tool()
    def search_past_incidents(query: str, k: KMax = 3) -> SearchIncidentsResult:
        """Find previously recorded incidents similar to a description — what the
        root cause was and which remediation resolved it."""
        hits = [
            IncidentHit(incident_id=d.key, title=d.title, score=round(d.score, 4),
                        excerpt=truncate(d.body, 600))
            for d in store.search(query, kind="incident", k=k)
        ]
        log.info("mcp_tool", step="mcp.knowledge", tool="search_past_incidents",
                 query=truncate(query, 120), results=len(hits))
        return SearchIncidentsResult(query=query, results=hits)

    @mcp.tool()
    def record_outcome(incident_id: str, summary: str, root_cause: str,
                       remediation: str, resolved: bool, notes: str = "") -> RecordOutcomeResult:
        """Record how an incident turned out so future searches can retrieve it.
        Idempotent: re-recording the same incident_id updates the existing record."""
        title = f"[{'resolved' if resolved else 'unresolved'}] {truncate(summary, 120)}"
        body = (
            f"Summary: {summary}\n"
            f"Root cause: {root_cause}\n"
            f"Remediation: {remediation}\n"
            f"Outcome: {'resolved' if resolved else 'NOT resolved'}"
            + (f"\nNotes: {notes}" if notes else "")
        )
        doc_id, created = store.add("incident", incident_id, title, body)
        log.info("mcp_tool", step="mcp.knowledge", tool="record_outcome",
                 incident_id=incident_id, created=created, resolved=resolved)
        return RecordOutcomeResult(incident_id=incident_id, doc_id=doc_id, created=created)

    return mcp
