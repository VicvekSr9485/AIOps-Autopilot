"""Knowledge store unit tests: deterministic embeddings, upsert idempotency,
kind isolation, ranking — all offline against an in-memory SQLite db."""

from __future__ import annotations

import math

from autopilot.mcp_servers.knowledge import seed_store
from autopilot.mcp_servers.runbooks import SEED_RUNBOOKS
from autopilot.mcp_servers.store import EMBED_DIM, KnowledgeStore, embed


def test_embed_is_deterministic_and_normalized():
    a = embed("postgres connection slots exhausted")
    b = embed("postgres connection slots exhausted")
    assert a == b
    assert len(a) == EMBED_DIM
    assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0, rel_tol=1e-9)
    assert embed("completely different text about queues") != a


def test_add_is_upsert_by_kind_and_key():
    store = KnowledgeStore(":memory:")
    doc_id, created = store.add("runbook", "rb-1", "title", "body one")
    assert created
    same_id, created_again = store.add("runbook", "rb-1", "title", "body two")
    assert same_id == doc_id and not created_again
    assert store.count("runbook") == 1
    assert store.search("body", kind="runbook", k=1)[0].body == "body two"


def test_seed_is_idempotent():
    store = KnowledgeStore(":memory:")
    seed_store(store)
    seed_store(store)
    assert store.count("runbook") == len(SEED_RUNBOOKS)


def test_search_isolates_kinds_and_ranks_by_similarity():
    store = KnowledgeStore(":memory:")
    seed_store(store)
    store.add("incident", "inc-1", "db outage",
              "postgres connection slots exhausted by idle sessions")

    runbook_hits = store.search("postgres connection slots", kind="runbook", k=10)
    assert all(d.kind == "runbook" for d in runbook_hits)
    assert runbook_hits[0].key == "postgres-connection-exhaustion"
    assert runbook_hits[0].score > runbook_hits[-1].score

    incident_hits = store.search("postgres connection slots", kind="incident", k=10)
    assert [d.key for d in incident_hits] == ["inc-1"]
