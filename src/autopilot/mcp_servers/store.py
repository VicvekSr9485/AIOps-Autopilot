"""Local knowledge store: runbooks + past incidents in SQLite with vector search.

Embeddings are deterministic local hashing vectors (lowercased word tokens →
sha256-bucketed signed counts, L2-normalized): zero network, zero tokens, stable
across runs — swap `embed()` for a real embedding model later without touching
the schema. KNN uses the sqlite-vec extension when the interpreter can load
SQLite extensions; otherwise it falls back to a pure-Python cosine scan over the
same rows (identical scores — vectors are normalized, so 1 - d²/2 == cosine).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger("autopilot.mcp.store")

EMBED_DIM = 256
DATA_DIR = Path(__file__).resolve().parents[3] / "data"
DEFAULT_DB_PATH = DATA_DIR / "knowledge.db"

DocKind = Literal["runbook", "incident"]


class StoredDoc(BaseModel):
    id: int
    kind: DocKind
    key: str  # stable slug (runbooks) or incident id — unique per kind
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)


class ScoredDoc(StoredDoc):
    score: float  # cosine similarity in [-1, 1]; higher is more relevant


def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic hashing embedding: signed bag-of-words, L2-normalized."""
    vec = [0.0] * dim
    for token in re.findall(r"[a-z0-9_]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        idx = ((digest[0] << 8) | digest[1]) % dim
        vec[idx] += 1.0 if digest[2] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


class KnowledgeStore:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path) if db_path else DEFAULT_DB_PATH
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self.vec_enabled = self._try_load_sqlite_vec()
        self._init_schema()
        log.info("knowledge_store_opened", step="mcp.knowledge", path=str(path),
                 vec_enabled=self.vec_enabled)

    def _try_load_sqlite_vec(self) -> bool:
        if not hasattr(self._db, "enable_load_extension"):
            # e.g. pyenv/macOS CPython built without --enable-loadable-sqlite-extensions
            log.info("sqlite_vec_unavailable", step="mcp.knowledge",
                     reason="sqlite3 built without extension loading")
            return False
        try:
            import sqlite_vec

            self._db.enable_load_extension(True)
            sqlite_vec.load(self._db)
            self._db.enable_load_extension(False)
            return True
        except Exception as e:
            log.info("sqlite_vec_unavailable", step="mcp.knowledge", reason=str(e)[:200])
            return False

    def _init_schema(self) -> None:
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                embedding BLOB NOT NULL,
                UNIQUE (kind, key)
            )"""
        )
        if self.vec_enabled:
            self._db.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_index "
                f"USING vec0(embedding float[{EMBED_DIM}])"
            )
        self._db.commit()

    # ------------------------------------------------------------------ write

    def add(self, kind: DocKind, key: str, title: str, body: str,
            tags: list[str] | None = None) -> tuple[int, bool]:
        """Upsert by (kind, key). Returns (doc_id, created) — idempotent."""
        vec_blob = _pack(embed(f"{title}\n{body}"))
        tags_json = json.dumps(tags or [])
        row = self._db.execute(
            "SELECT id FROM documents WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        if row:
            doc_id, created = row[0], False
            self._db.execute(
                "UPDATE documents SET title = ?, body = ?, tags = ?, embedding = ? "
                "WHERE id = ?",
                (title, body, tags_json, vec_blob, doc_id),
            )
            if self.vec_enabled:
                self._db.execute("DELETE FROM vec_index WHERE rowid = ?", (doc_id,))
        else:
            cur = self._db.execute(
                "INSERT INTO documents (kind, key, title, body, tags, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, key, title, body, tags_json, vec_blob),
            )
            doc_id, created = cur.lastrowid, True
        if self.vec_enabled:
            self._db.execute(
                "INSERT INTO vec_index (rowid, embedding) VALUES (?, ?)", (doc_id, vec_blob)
            )
        self._db.commit()
        return doc_id, created

    # ------------------------------------------------------------------- read

    def search(self, query: str, kind: DocKind, k: int = 3) -> list[ScoredDoc]:
        query_vec = embed(query)
        scored = (
            self._search_vec(query_vec, kind, k)
            if self.vec_enabled
            else self._search_python(query_vec, kind)
        )
        scored.sort(key=lambda d: d.score, reverse=True)
        return scored[:k]

    def _search_vec(self, query_vec: list[float], kind: DocKind, k: int) -> list[ScoredDoc]:
        # Over-fetch (vec0 can't filter by kind), then join + filter.
        rows = self._db.execute(
            "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (_pack(query_vec), k * 4 + 8),
        ).fetchall()
        out: list[ScoredDoc] = []
        for rowid, distance in rows:
            doc = self._fetch(rowid)
            if doc is not None and doc.kind == kind:
                # normalized vectors: cosine = 1 - L2distance² / 2
                out.append(ScoredDoc(score=1.0 - distance * distance / 2.0,
                                     **doc.model_dump()))
        return out

    def _search_python(self, query_vec: list[float], kind: DocKind) -> list[ScoredDoc]:
        rows = self._db.execute(
            "SELECT id, kind, key, title, body, tags, embedding FROM documents "
            "WHERE kind = ?",
            (kind,),
        ).fetchall()
        out: list[ScoredDoc] = []
        for doc_id, doc_kind, key, title, body, tags, blob in rows:
            doc_vec = _unpack(blob)
            score = sum(a * b for a, b in zip(query_vec, doc_vec, strict=True))
            out.append(ScoredDoc(id=doc_id, kind=doc_kind, key=key, title=title,
                                 body=body, tags=json.loads(tags), score=score))
        return out

    def _fetch(self, doc_id: int) -> StoredDoc | None:
        row = self._db.execute(
            "SELECT id, kind, key, title, body, tags FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredDoc(id=row[0], kind=row[1], key=row[2], title=row[3],
                         body=row[4], tags=json.loads(row[5]))

    def count(self, kind: DocKind) -> int:
        return self._db.execute(
            "SELECT count(*) FROM documents WHERE kind = ?", (kind,)
        ).fetchone()[0]
