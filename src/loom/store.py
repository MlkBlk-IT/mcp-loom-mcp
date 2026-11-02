"""Local drawer: where moved content lives.

SQLite + FTS5. No server, no network, no vendor SDK. Portable everywhere.
Supports optional encryption via SQLCipher or column-level Fernet.

The drawer path is read from the module global ``DB_PATH`` at CALL time, so a
test (or a caller) can repoint the drawer at any moment without the module
holding a stale path.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from loom.crypto import connect_encrypted, get_encryption

DB_PATH = Path(os.environ.get("LOOM_DB", Path.home() / ".loom" / "loom.db"))

# Drawer paths whose schema is already created in this process (DDL once).
_SCHEMA_READY: set[str] = set()


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    """The drawer path, read from the module global at CALL time."""
    return Path(DB_PATH)


def _schema_key(db_path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(db_path)))


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _apply_ddl(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema. Additive only: never destructive."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            label      TEXT NOT NULL DEFAULT '',
            content    TEXT NOT NULL,
            tokens     INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            hits       INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS anchors (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            label      TEXT NOT NULL DEFAULT '',
            body       TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invariants (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            kind       TEXT NOT NULL,
            value      TEXT NOT NULL,
            priority   INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            label        TEXT NOT NULL DEFAULT '',
            created_at   REAL NOT NULL,
            updated_at   REAL NOT NULL,
            ttl_days     INTEGER NOT NULL DEFAULT 30
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sources (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT NOT NULL,
            label         TEXT NOT NULL DEFAULT '',
            sha256        TEXT NOT NULL,
            bytes         INTEGER NOT NULL DEFAULT 0,
            n_chunks      INTEGER NOT NULL DEFAULT 0,
            first_chunk_id INTEGER,
            last_chunk_id  INTEGER,
            tokens_before INTEGER NOT NULL DEFAULT 0,
            anchor_tokens INTEGER NOT NULL DEFAULT 0,
            redactions    INTEGER NOT NULL DEFAULT 0,
            created_at    REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inv_session ON invariants (session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunk_session ON chunks (session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_anchor_session ON anchors (session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions (updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_session ON sources (session_id)"
    )

    # Additive columns: an old drawer keeps NULL there.
    chunk_columns = _table_columns(conn, "chunks")
    if "source_sha" not in chunk_columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN source_sha TEXT")
    if "idx" not in chunk_columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN idx INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunk_source ON chunks (source_sha, idx)"
    )

    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            label, content, content='chunks', content_rowid='id'
        )
        """
    )
    conn.commit()


def _ensure_schema(conn: sqlite3.Connection, db_path: Path) -> None:
    """Apply the DDL once per drawer path per process (idempotent)."""
    key = _schema_key(db_path)
    if key in _SCHEMA_READY:
        # Cheap sanity check: the file may have been removed since last time.
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
        ).fetchone()
        if row is not None:
            return
    had_fts = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()
        is not None
    )
    _apply_ddl(conn)
    if not had_fts:
        # The FTS index was just created over an existing content table:
        # rebuild it so the index matches the stored rows.
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        conn.commit()
    _SCHEMA_READY.add(key)


def _connect() -> sqlite3.Connection:
    """Open the drawer (encrypted when a password is configured).

    The path is resolved from the module global at call time and handed to
    crypto, which never binds a path of its own.
    """
    db_path = _db_path()
    conn = connect_encrypted(db_path)
    _ensure_schema(conn, db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _encryption():
    """The encryption handler of the current drawer (path resolved now)."""
    return get_encryption(_db_path())


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _insert_chunks(
    conn: sqlite3.Connection, session_id: str, label: str, chunks: list[dict[str, Any]]
) -> list[int]:
    """Insert chunks (+ their FTS index rows) inside the caller's transaction."""
    from loom.tokenizer import count_tokens

    crypto = _encryption()
    ids: list[int] = []
    for chunk in chunks:
        text = chunk["text"]
        # The FTS index stores exactly what the content table stores, so the
        # external-content index stays consistent with the content table.
        stored = crypto.encrypt_content(text) if crypto.column_encrypted else text
        cur = conn.execute(
            "INSERT INTO chunks (session_id, label, content, tokens, created_at, "
            "source_sha, idx) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                label,
                stored,
                count_tokens(text),
                time.time(),
                chunk.get("source_sha"),
                chunk.get("idx"),
            ),
        )
        chunk_id = int(cur.lastrowid)
        ids.append(chunk_id)
        if not crypto.column_encrypted:
            conn.execute(
                "INSERT INTO chunks_fts (rowid, label, content) VALUES (?, ?, ?)",
                (chunk_id, label, stored),
            )
    return ids


def _insert_anchor(
    conn: sqlite3.Connection, session_id: str, label: str, body: str
) -> int:
    crypto = _encryption()
    stored = crypto.encrypt_content(body) if crypto.column_encrypted else body
    cur = conn.execute(
        "INSERT INTO anchors (session_id, label, body, created_at) VALUES (?, ?, ?, ?)",
        (session_id, label, stored, time.time()),
    )
    return int(cur.lastrowid)


def _insert_invariants(
    conn: sqlite3.Connection, session_id: str, invariants: list[tuple[str, str, int]]
) -> None:
    """Insert invariants, skipping the ones already stored for this session.

    Skipped entirely in column-encrypted mode: extracted values are plaintext,
    so persisting them would defeat the column encryption.
    ``verify.check_session`` recomputes them from the decrypted chunks instead.
    """
    if _encryption().column_encrypted:
        return
    conn.executemany(
        "INSERT INTO invariants (session_id, kind, value, priority) "
        "SELECT ?, ?, ?, ? WHERE NOT EXISTS ("
        "  SELECT 1 FROM invariants WHERE session_id = ? AND kind = ? AND value = ?"
        ")",
        [
            (session_id, kind, value, priority, session_id, kind, value)
            for kind, value, priority in invariants
        ],
    )


def save_chunks(session_id: str, label: str, chunks: list[dict[str, Any]]) -> list[int]:
    """Persist chunks and return their ids in order."""
    conn = _connect()
    try:
        with conn:
            ids = _insert_chunks(conn, session_id, label, chunks)
    finally:
        conn.close()
    return ids


def save_anchor(session_id: str, label: str, body: str) -> int:
    """Persist the in-context anchor so `verify` can compare against it later."""
    conn = _connect()
    try:
        with conn:
            anchor_id = _insert_anchor(conn, session_id, label, body)
    finally:
        conn.close()
    return anchor_id


def save_invariants(session_id: str, invariants: list[tuple[str, str, int]]) -> None:
    """Persist the invariant set of a session so verification never rescans the content."""
    conn = _connect()
    try:
        with conn:
            _insert_invariants(conn, session_id, invariants)
    finally:
        conn.close()


def save_all(
    session_id: str,
    label: str,
    chunks: list[dict[str, Any]],
    anchor: str,
    invariants: list[tuple[str, str, int]],
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store chunks, anchor, invariants and the source row in ONE transaction.

    ``source_meta`` may carry ``tokens_before``, ``anchor_tokens`` and
    ``redactions``; the integrity fields (sha256, bytes, chunk range) are
    computed from the stored chunks themselves.
    """
    from loom.tokenizer import count_tokens

    meta = dict(source_meta or {})
    full_text = "".join(chunk["text"] for chunk in chunks)
    raw = full_text.encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()

    conn = _connect()
    try:
        with conn:
            ids = _insert_chunks(conn, session_id, label, chunks)
            anchor_id = _insert_anchor(conn, session_id, label, anchor)
            _insert_invariants(conn, session_id, invariants)
            cur = conn.execute(
                "INSERT INTO sources (session_id, label, sha256, bytes, n_chunks, "
                "first_chunk_id, last_chunk_id, tokens_before, anchor_tokens, "
                "redactions, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    label,
                    sha256,
                    len(raw),
                    len(ids),
                    ids[0] if ids else None,
                    ids[-1] if ids else None,
                    int(meta.get("tokens_before", count_tokens(full_text))),
                    int(meta.get("anchor_tokens", count_tokens(anchor))),
                    int(meta.get("redactions", 0)),
                    time.time(),
                ),
            )
            source_id = int(cur.lastrowid)
    finally:
        conn.close()

    # --- P1: embed chunks for semantic search (non-blocking) ---
    try:
        from loom import vector_index
        if vector_index.available():
            texts = [chunk["text"] for chunk in chunks]
            vector_index.add_batch(ids, texts)
            vector_index.save()
    except Exception:
        pass  # FTS still works; semantic is best-effort

    return {
        "chunk_ids": ids,
        "anchor_id": anchor_id,
        "source_id": source_id,
        "sha256": sha256,
        "bytes": len(raw),
        "n_chunks": len(ids),
        "embedded": len(ids),
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def load_invariants(session_id: str) -> list[tuple[str, str, int]]:
    """Return the persisted invariant set of a session."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT kind, value, priority FROM invariants WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(r["kind"], r["value"], r["priority"]) for r in rows]


def _chunk_dict(row: sqlite3.Row) -> dict[str, Any]:
    crypto = _encryption()
    item: dict[str, Any] = {
        "id": row["id"],
        "session_id": row["session_id"],
        "label": row["label"],
        "content": crypto.decrypt_content(row["content"]),
        "tokens": row["tokens"],
    }
    source_sha = row["source_sha"] if "source_sha" in row.keys() else None
    idx = row["idx"] if "idx" in row.keys() else None
    if source_sha:
        item["source_sha"] = source_sha
        if idx is not None:
            item["idx"] = idx
            item["ref"] = f"{source_sha[:12]}#{idx}"
    return item


def load_chunk(chunk_id: int) -> dict[str, Any] | None:
    """Return a chunk by id, incrementing its hit counter."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row is not None:
            with conn:
                conn.execute(
                    "UPDATE chunks SET hits = hits + 1 WHERE id = ?", (chunk_id,)
                )
    finally:
        conn.close()
    if row is None:
        return None
    return _chunk_dict(row)


def load_chunk_by_ref(ref: str) -> dict[str, Any] | None:
    """Return the chunk addressed by a ``<sha12>#<idx>`` reference, or None."""
    if not ref or "#" not in ref:
        return None
    sha12, _, index = ref.partition("#")
    try:
        idx = int(index)
    except ValueError:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM chunks WHERE source_sha LIKE ? AND idx = ? "
            "ORDER BY id LIMIT 1",
            (sha12 + "%", idx),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _chunk_dict(row)


def iter_session_chunks(
    session_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield the stored chunks (decrypted) in id order; None = every session."""
    conn = _connect()
    try:
        if session_id is None:
            rows = conn.execute(
                "SELECT * FROM chunks ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chunks WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
    finally:
        conn.close()
    for row in rows:
        yield _chunk_dict(row)


def load_anchors(session_id: str) -> list[dict[str, Any]]:
    """Return the anchors of a session (decrypted)."""
    crypto = _encryption()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, session_id, label, body, created_at FROM anchors "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r["id"],
            "session_id": r["session_id"],
            "label": r["label"],
            "body": crypto.decrypt_content(r["body"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def load_sources(session_id: str | None = None) -> list[dict[str, Any]]:
    """Return the source rows (integrity metadata) of a session or of all of them."""
    conn = _connect()
    try:
        if session_id is None:
            rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sources WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def stats(session_id: str = "default") -> dict[str, Any]:
    """Measured token statistics for a session."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(tokens), 0) AS stored_tokens, "
            "COALESCE(SUM(hits), 0) AS hits FROM chunks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        sources = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE session_id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    stored = int(row["stored_tokens"])
    return {
        "session_id": session_id,
        "chunks": int(row["n"]),
        "stored_tokens": stored,
        "hits": int(row["hits"]),
        "sources": int(sources["n"]),
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_disabled() -> bool:
    """True when the drawer is encrypted: the FTS index holds ciphertext only.

    In that mode the search tool must refuse to run instead of returning
    meaningless matches over ciphertext.
    """
    return _encryption().column_encrypted


def _escape_fts(query: str, mode: str = "AND") -> str:
    """Turn a user query into a safe FTS5 MATCH expression.

    Every whitespace-separated term is double-quoted (inner quotes doubled), so
    FTS5 operators, quotes and punctuation are matched literally and can never
    raise a syntax error. ``mode="OR"`` joins the terms with OR (used as a
    fallback when the strict AND query finds nothing).
    """
    terms = [term for term in str(query).split() if term]
    if not terms:
        return ""
    sep = " OR " if mode == "OR" else " "
    return sep.join('"' + term.replace('"', '""') + '"' for term in terms)


_STOP = {
    "the", "a", "an", "of", "at", "in", "on", "to", "for", "and", "or", "is", "are", "was",
    "what", "which", "le", "la", "les", "de", "du", "des", "un", "une", "et", "au", "aux", "en", "dans",
}
# query word -> anchors.PATTERNS key: "what is the X port" wants a chunk that holds a port VALUE.
_INTENT = {
    "port": "port", "ports": "port", "version": "version", "versions": "version",
    "url": "url", "urls": "url", "ip": "ip", "path": "path", "hash": "hex_hash",
    "sha": "hex_hash", "sha256": "hex_hash",
}
_RERANK_WINDOW = 120


def _rerank(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Reorder candidates by term proximity (+ value bonus), bm25 order breaks ties.

    bm25 favours short chunks that repeat the query words; the chunk that holds the
    actual value (`PORT = 8765`) is often longer and ranked far below them.
    """
    terms = [
        t for t in dict.fromkeys(re.findall(r"[A-Za-z0-9]+", str(query).lower()))
        if t not in _STOP and len(t) > 1
    ]
    if len(terms) < 2 or len(results) < 2:
        return results
    from loom import anchors as anchor_mod

    kinds = [anchor_mod.PATTERNS[k] for k in {_INTENT[t] for t in terms if t in _INTENT}
             if k in anchor_mod.PATTERNS]
    word = {
        t: re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(t), re.IGNORECASE)
        for t in terms
    }
    scored = []
    for pos, item in enumerate(results):
        text = item["content"]
        hits = sorted((m.start(), t) for t in terms for m in word[t].finditer(text))[:300]
        vals = [m.start() for p in kinds for m in p.finditer(text)][:300]
        best = 0.0
        for p, _t in hits:
            near = {t for q, t in hits if abs(q - p) <= _RERANK_WINDOW}
            score = float(len(near))
            if vals and any(abs(v - p) <= _RERANK_WINDOW for v in vals):
                score += 1.5
            best = max(best, score)
        scored.append((-best, pos, item))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [item for _s, _p, item in scored]


def search(
    query: str, session_id: str | None = None, limit: int = 10, topup: bool = False
) -> list[dict[str, Any]]:
    """Full-text search over stored chunks; ``session_id=None`` searches all sessions."""
    escaped = _escape_fts(query)
    if not escaped:
        return []

    sql = (
        "SELECT c.id, c.label, c.session_id, c.source_sha, c.idx, "
        "c.content, "
        "snippet(chunks_fts, 1, '', '', '…', 20) AS snip "
        "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ?"
    )
    params: list[Any] = [escaped]
    if session_id is not None:
        sql += " AND c.session_id = ?"
        params.append(session_id)
    multi = len(str(query).split()) > 1
    pool = max(limit * 10, 50) if multi else limit
    sql += " ORDER BY rank LIMIT ?"
    params.append(pool)

    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
        if (not rows or (topup and len(rows) < limit)) and multi:
            # Strict AND found nothing (or topup and fewer than limit): add OR matches, bm25-ranked.
            params[0] = _escape_fts(query, "OR")
            seen = {r["id"] for r in rows}
            extra = [r for r in conn.execute(sql, params).fetchall() if r["id"] not in seen]
            rows = list(rows) + extra[: max(0, pool - len(rows))]
    finally:
        conn.close()

    crypto = _encryption()
    results: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {
            "id": row["id"],
            "label": row["label"],
            "session_id": row["session_id"],
            "content": crypto.decrypt_content(row["content"]),
            "snippet": row["snip"],
        }
        source_sha = row["source_sha"] if "source_sha" in row.keys() else None
        idx = row["idx"] if "idx" in row.keys() else None
        if source_sha and idx is not None:
            item["ref"] = f"{source_sha[:12]}#{idx}"
        results.append(item)
    if multi:
        results = _rerank(results, query)
    return results[:limit]


def search_semantic(
    query: str, session_id: str | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Semantic search via FAISS HNSW. Falls back to [] if no backend.

    Returns the same shape as ``search()``: list of dicts with id, label,
    session_id, content, snippet, plus ``score`` (cosine similarity).
    """
    from loom import vector_index

    hits = vector_index.search(query, k=limit * 2)  # over-fetch, filter, trim
    if not hits:
        return []

    # Load chunk content from DB for the matched IDs.
    ids = [h["chunk_id"] for h in hits]
    placeholders = ",".join("?" * len(ids))
    sql = f"SELECT * FROM chunks WHERE id IN ({placeholders})"
    conn = _connect()
    try:
        rows = {row["id"]: row for row in conn.execute(sql, ids).fetchall()}
    finally:
        conn.close()

    crypto = _encryption()
    results: list[dict[str, Any]] = []
    for hit in hits:
        cid = hit["chunk_id"]
        row = rows.get(cid)
        if row is None:
            continue
        if session_id is not None and row["session_id"] != session_id:
            continue
        content = crypto.decrypt_content(row["content"])
        # Snippet: first 200 chars (semantic search doesn't produce FTS snippets)
        snip = content[:200].replace("\n", " ") + ("…" if len(content) > 200 else "")
        item: dict[str, Any] = {
            "id": row["id"],
            "label": row["label"],
            "session_id": row["session_id"],
            "content": content,
            "snippet": snip,
            "score": round(hit["score"], 4),
        }
        source_sha = row["source_sha"] if "source_sha" in row.keys() else None
        idx = row["idx"] if "idx" in row.keys() else None
        if source_sha and idx is not None:
            item["ref"] = f"{source_sha[:12]}#{idx}"
        results.append(item)
    return results[:limit]
