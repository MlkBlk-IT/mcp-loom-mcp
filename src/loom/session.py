"""Session management — auto UUIDv7, isolation, TTL, admin tools.

The drawer path and the schema belong to ``store``: this module calls
``store._connect()`` and reads ``store.DB_PATH`` dynamically, so there is no
second copy of the path and no second schema owner.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from loom import crypto, store


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TTL_DAYS = int(os.environ.get("LOOM_TTL_DAYS", "30"))
SESSION_ID_ENV = os.environ.get("LOOM_SESSION_ID")


# ---------------------------------------------------------------------------
# UUIDv7 generation (time-ordered, RFC 9562)
# ---------------------------------------------------------------------------

def _uuid7() -> str:
    """Generate UUIDv7 (Unix timestamp ms + random)."""
    ms = int(time.time() * 1000)
    rand = uuid.uuid4().bytes
    time_bytes = ms.to_bytes(6, "big")
    combined = time_bytes + rand[6:]
    combined = bytearray(combined)
    combined[6] = (combined[6] & 0x0F) | 0x70  # version 7
    combined[8] = (combined[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(combined)).hex


def generate_session_id() -> str:
    """Generate a new session ID (UUIDv7)."""
    return _uuid7()


def resolve_session_id(provided: str | None = None) -> str:
    """Resolve the session ID: explicit > env > literal "default".

    "default" is a stable, shared session ID: several calls without an
    explicit session_id land in the same session, which is what makes
    ``ctx_store`` and ``ctx_check`` see each other's data. It is NOT a UUIDv7:
    ``generate_session_id()`` produces one when a caller asks for a unique
    session.
    """
    if provided and provided != "default":
        return provided
    if SESSION_ID_ENV:
        return SESSION_ID_ENV
    return "default"


# ---------------------------------------------------------------------------
# Session metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    label: str
    created_at: float
    updated_at: float
    chunk_count: int
    token_count: int
    anchor_count: int
    expired: bool


def _now() -> float:
    return time.time()


def _is_expired(updated_at: float, ttl_days: int) -> bool:
    """A session expires when it has not been touched for ttl_days."""
    return (_now() - updated_at) > (ttl_days * 86400)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_session(session_id: str, label: str = "", ttl_days: int = DEFAULT_TTL_DAYS) -> None:
    """Register a session (upsert: created_at is preserved on conflict)."""
    conn = store._connect()
    try:
        now = _now()
        with conn:
            conn.execute(
                "INSERT INTO sessions (session_id, label, created_at, updated_at, ttl_days) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "  updated_at = excluded.updated_at, "
                "  label = CASE WHEN excluded.label != '' THEN excluded.label "
                "               ELSE sessions.label END",
                (session_id, label, now, now, ttl_days),
            )
    finally:
        conn.close()


def touch_session(session_id: str) -> None:
    """Update the session's updated_at to now."""
    conn = store._connect()
    try:
        with conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (_now(), session_id),
            )
    finally:
        conn.close()


def get_session(session_id: str) -> SessionInfo | None:
    """Get session metadata + stats."""
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT session_id, label, created_at, updated_at, ttl_days FROM sessions "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None

        # Stats
        stats_row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM chunks WHERE session_id = ?) as chunks,
                (SELECT COALESCE(SUM(tokens), 0) FROM chunks WHERE session_id = ?) as tokens,
                (SELECT COUNT(*) FROM anchors WHERE session_id = ?) as anchors
            """,
            (session_id, session_id, session_id),
        ).fetchone()
    finally:
        conn.close()

    expired = _is_expired(row["updated_at"], row["ttl_days"])
    return SessionInfo(
        session_id=row["session_id"],
        label=row["label"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        chunk_count=stats_row["chunks"] or 0,
        token_count=stats_row["tokens"] or 0,
        anchor_count=stats_row["anchors"] or 0,
        expired=expired,
    )


def list_sessions(include_expired: bool = False) -> list[SessionInfo]:
    """List all sessions, optionally including expired."""
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT session_id, label, created_at, updated_at, ttl_days FROM sessions "
            "ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()

    sessions = []
    for row in rows:
        expired = _is_expired(row["updated_at"], row["ttl_days"])
        if expired and not include_expired:
            continue
        info = get_session(row["session_id"])
        if info:
            sessions.append(info)
    return sessions


def purge_session(session_id: str) -> dict[str, Any]:
    """Delete all data for a session (chunks, FTS rows, anchors, invariants, source)."""
    conn = store._connect()
    try:
        with conn:
            # FTS5 external-content index: the index rows must be dropped with
            # the 'delete' command BEFORE the content rows disappear, otherwise
            # the index keeps stale entries (and integrity-check fails).
            fts_rows = conn.execute(
                "SELECT id, label, content FROM chunks WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            for row in fts_rows:
                # Fernet rows are never handed to the FTS index (see
                # store._insert_chunks), so replaying their ciphertext as a
                # 'delete' would feed the index a value it never stored and
                # corrupt it. Skipping is the only faithful replay.
                if row["content"].startswith(crypto.FERNET_TOKEN_PREFIX):
                    continue
                conn.execute(
                    "INSERT INTO chunks_fts (chunks_fts, rowid, label, content) "
                    "VALUES ('delete', ?, ?, ?)",
                    (row["id"], row["label"], row["content"]),
                )
            chunks_deleted = conn.execute(
                "DELETE FROM chunks WHERE session_id = ?", (session_id,)
            ).rowcount
            anchors_deleted = conn.execute(
                "DELETE FROM anchors WHERE session_id = ?", (session_id,)
            ).rowcount
            invariants_deleted = conn.execute(
                "DELETE FROM invariants WHERE session_id = ?", (session_id,)
            ).rowcount
            sources_deleted = conn.execute(
                "DELETE FROM sources WHERE session_id = ?", (session_id,)
            ).rowcount
            session_deleted = conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            ).rowcount
    finally:
        conn.close()
    return {
        "session_id": session_id,
        "chunks_deleted": chunks_deleted,
        "anchors_deleted": anchors_deleted,
        "invariants_deleted": invariants_deleted,
        "sources_deleted": sources_deleted,
        "session_row_deleted": session_deleted > 0,
    }


def purge_expired_sessions() -> dict[str, Any]:
    """Purge all expired sessions. Returns count and details."""
    conn = store._connect()
    try:
        expired_rows = conn.execute(
            "SELECT session_id, updated_at, ttl_days FROM sessions"
        ).fetchall()
    finally:
        conn.close()

    purged = []
    for row in expired_rows:
        if _is_expired(row["updated_at"], row["ttl_days"]):
            result = purge_session(row["session_id"])
            purged.append(result)
    return {"purged_count": len(purged), "sessions": purged}


def vacuum_db() -> dict[str, Any]:
    """Run SQLite VACUUM to reclaim space."""
    db_path = store.DB_PATH
    conn = store._connect()
    try:
        start_size = db_path.stat().st_size
        conn.execute("VACUUM")
    finally:
        conn.close()
    end_size = db_path.stat().st_size
    return {
        "ok": True,
        "size_before_bytes": start_size,
        "size_after_bytes": end_size,
        "reclaimed_bytes": start_size - end_size,
    }


def health_check() -> dict[str, Any]:
    """Health check for the Loom drawer."""
    db_path = store.DB_PATH
    conn = store._connect()
    try:
        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        anchor_count = conn.execute("SELECT COUNT(*) FROM anchors").fetchone()[0]
        invariant_count = conn.execute("SELECT COUNT(*) FROM invariants").fetchone()[0]
        source_count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        wal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    return {
        "ok": True,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "sessions": session_count,
        "chunks": chunk_count,
        "anchors": anchor_count,
        "invariants": invariant_count,
        "sources": source_count,
        "journal_mode": wal_mode,
        "ttl_days_default": DEFAULT_TTL_DAYS,
    }
