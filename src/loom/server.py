"""Loom — portable MCP server: weave LLM context without cutting a thread.

Business logic lives in the sibling modules; this file only exposes it as MCP tools.
"""

from __future__ import annotations

import hashlib
import re
import logging
import os
import time

from typing import Annotated

from fastmcp import FastMCP
from pydantic import BeforeValidator
from mcp.types import ToolAnnotations

from loom import anchors as anchor_mod
from loom import compress, session, store, verify
from loom.audit import audit_tool_call
from loom.profile import get_profile
from loom.ratelimit import get_rate_limiter
from loom.sandbox import get_sandbox
from loom.tokenizer import count_tokens

log = logging.getLogger(__name__)

SERVER_NAME = "loom"
INSTRUCTIONS = """
Loom keeps context small WITHOUT losing information. It never summarizes by default.

When tool output or file content is bulky:
  1. Call `ctx_store` with the content — Loom moves it into a local drawer and returns a
     dense anchor plus stable IDs.
  2. Keep the anchor in your context; drop the raw bulk.
  3. If you truly need the raw bytes, call `ctx_get` with the returned ID — you get them
     back byte-identical.

Before accepting a compressed context, call `ctx_check` to confirm nothing critical was lost.
Losing a number, a path, a version or an explicit constraint is a bug, not a trade-off.
"""

mcp = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS)

# Max chunk ids echoed in a ctx_store* response (the rest: chunk_id_first/last).
MAX_IDS_IN_RESPONSE = 20

_rate_limiter = get_rate_limiter()


def _check_rate_limit(session_id: str) -> dict | None:
    """Check rate limit for session. Returns error dict if limited, None if allowed."""
    if not _rate_limiter.allow(session_id):
        return {"error": f"rate limit exceeded for session {session_id}"}
    return None


def _audit_call(tool: str, session_id: str, start: float, outcome: str, error: str | None = None):
    """Log tool call to audit trail."""
    audit_tool_call(
        tool=tool,
        session_id=session_id,
        latency_ms=(time.perf_counter() - start) * 1000,
        outcome=outcome,
        error=error,
    )


import time


def _path_allowed(path: str, dirs: list[str]) -> bool:
    """True if `path` is inside one of `dirs` (realpath + case-normalised + commonpath).

    Avoids the prefix bug of str.startswith ('/a/ok' must not allow '/a/ok_evil'),
    follows symlinks, and is case-insensitive on Windows.
    """
    import os as _o

    p = _o.path.normcase(_o.path.realpath(path))
    for d in dirs:
        base = _o.path.normcase(_o.path.realpath(d))
        try:
            if _o.path.commonpath([p, base]) == base:
                return True
        except ValueError:  # different drives on Windows
            continue
    return False


def _excerpt(content: str, query: str, width: int = 600) -> str:
    """Window of `width` chars centred on the densest cluster of query terms."""
    if len(content) <= width:
        return content
    terms = set(re.findall(r"\w+", query.lower()))
    hits = sorted(
        (m.start(), t)
        for t in terms
        for m in re.finditer(r"\b%s\b" % re.escape(t), content, re.IGNORECASE)
    )[:200]
    if not hits:
        return content[:width]
    half = width // 2
    best = max(
        hits,
        key=lambda h: (len({t for p, t in hits if abs(p - h[0]) <= half}), -h[0]),
    )
    start = max(0, best[0] - half)
    return content[start : start + width]


def _store_impl(content: str, session_id: str = "default", label: str = "") -> dict:
    """Redact, compress and persist content.

    No rate limit and no audit here: every caller owns exactly one
    rate-limit check and one audit entry describing its real outcome.
    """
    resolved_session = session.resolve_session_id(session_id)
    # Ensure session exists in metadata
    session.create_session(resolved_session, label=label)

    # Redact secrets before storage (Task 4)
    from loom import redact
    content, redaction_count = redact.redact_text(content)

    profile = get_profile()
    tokens_before = count_tokens(content)
    anchor, chunks = compress.build_anchor(content, profile=profile)
    anchor_tokens = count_tokens(anchor)
    # Break-even guard: below ~3k tokens the anchor (invariants + facts + footer)
    # outweighs the source. Hand the content back verbatim instead; the chunks are
    # still stored, so ctx_get / ctx_search keep working.
    passthrough = anchor_tokens >= tokens_before
    if passthrough:
        anchor = content
        anchor_tokens = tokens_before
    invariants = anchor_mod.extract(content)
    saved = store.save_all(
        resolved_session,
        label,
        chunks,
        anchor,
        [(i.kind, i.value, i.priority) for i in invariants],
        source_meta={
            "tokens_before": tokens_before,
            "anchor_tokens": anchor_tokens,
            "redactions": redaction_count,
        },
    )
    ids = saved["chunk_ids"]
    # Touch session updated_at
    session.touch_session(resolved_session)

    # A big source yields thousands of ids; echoing them all costs more context than
    # the anchor itself. Ids are contiguous (ctx_verify relies on it), and the anchor
    # footer already carries refs usable with ctx_get("<sha12>#<idx>").
    ids_out = ids
    ids_meta: dict = {}
    if len(ids) > MAX_IDS_IN_RESPONSE:
        ids_out = ids[:MAX_IDS_IN_RESPONSE]
        ids_meta = {
            "chunk_ids_truncated": True,
            "chunk_count": len(ids),
            "chunk_id_first": ids[0],
            "chunk_id_last": ids[-1],
        }

    return {
        "session_id": resolved_session,
        "anchor": anchor,
        "chunk_ids": ids_out,
        **ids_meta,
        "passthrough": passthrough,
        "tokens_before": tokens_before,
        "tokens_after": anchor_tokens,
        "saved_ratio": (
            round(1 - (anchor_tokens / tokens_before), 4) if tokens_before else 0.0
        ),
        "lossless": redaction_count == 0,
        "redactions": redaction_count,
    }


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_store(content: str, session_id: str = "default", label: str = "") -> dict:
    """Move bulky content out of the prompt into a local drawer, without losing any of it.

    Returns a dense anchor (the parts that must stay in context) plus stable IDs so the
    original bytes can be retrieved later with `ctx_get`.

    Args:
        content: the bulky text to store (log dump, file body, tool output, etc.)
        session_id: logical session the content belongs to ("default" or omitted = the shared "default" session)
        label: short human-readable name, e.g. "build.log"

    Returns:
        dict with anchor, chunk ids, token counts (before/after) and savings ratio.
    """
    start = time.perf_counter()
    rl = _check_rate_limit(session_id)
    if rl is not None:
        _audit_call("ctx_store", session_id, start, "error", rl["error"])
        return rl
    result = _store_impl(content, session_id, label)
    _audit_call("ctx_store", result["session_id"], start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_get(
    chunk_id: Annotated[str, BeforeValidator(str)], session_id: str = "default"
) -> dict:
    """Retrieve a stored chunk, byte-identical to what was originally stored.

    Args:
        chunk_id: id returned by `ctx_store` in `chunk_ids` (int or its string form), or a
            `<sha12>#<idx>` ref from the anchor footer
        session_id: session to retrieve from (default: "default"); a chunk of another
            session is only readable after an explicit ctx_transfer

    Returns:
        dict with the original content and its metadata.
    """
    start = time.perf_counter()
    resolved_session = session.resolve_session_id(session_id)
    rl = _check_rate_limit(resolved_session)
    if rl is not None:
        _audit_call("ctx_get", resolved_session, start, "error", rl["error"])
        return rl
    from loom import cross_session

    if isinstance(chunk_id, str) and "#" in chunk_id:
        chunk = store.load_chunk_by_ref(chunk_id)
        # Fail closed: a ref hit is only returned if its session can read it.
        if chunk is not None:
            cid = chunk.get("id")
            chunk = (
                cross_session.get_cross_session(int(cid), resolved_session)
                if cid is not None
                else None
            )
    else:
        try:
            cid = int(chunk_id)
        except (TypeError, ValueError):
            res = {
                "found": False,
                "error": f"invalid chunk_id {chunk_id!r}: expected an integer or '<sha12>#<idx>'.",
            }
            _audit_call("ctx_get", resolved_session, start, "error", res["error"])
            return res
        chunk = cross_session.get_cross_session(cid, resolved_session)
    if chunk is None:
        res = {
            "found": False,
            "error": f"chunk {chunk_id!r} not found or not accessible from session "
            f"{resolved_session!r}. It may have been pruned; "
            f"call ctx_store again with the content.",
        }
        _audit_call("ctx_get", resolved_session, start, "error", res["error"])
        return res
    _audit_call("ctx_get", resolved_session, start, "success")
    return {"found": True, "content": chunk["content"], "label": chunk["label"]}


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_check(session_id: str = "default") -> dict:
    """Verify that nothing critical was lost in the stored context.

    Re-derives the invariants (numbers, paths, URLs, versions, explicit constraints) from
    every chunk of the session and reports coverage.

    Returns:
        dict with coverage ratio, missing invariants and per-chunk detail.
    """
    start = time.perf_counter()
    rl = _check_rate_limit(session_id)
    if rl is not None:
        _audit_call("ctx_check", session_id, start, "error", rl["error"])
        return rl
    resolved_session = session.resolve_session_id(session_id)
    result = verify.check_session(resolved_session)
    result["source_identity"] = "voir ctx_verify"
    _audit_call("ctx_check", resolved_session, start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_stats(session_id: str = "default") -> dict:
    """Report measured token usage and savings for a session.

    Returns:
        dict with chunk count, total stored tokens, anchor tokens, ratio, hit counts.
    """
    start = time.perf_counter()
    rl = _check_rate_limit(session_id)
    if rl is not None:
        _audit_call("ctx_stats", session_id, start, "error", rl["error"])
        return rl
    resolved_session = session.resolve_session_id(session_id)
    result = store.stats(resolved_session)
    _audit_call("ctx_stats", resolved_session, start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_profile(mode: str = "strict") -> dict:
    """Set the compression profile.

    Args:
        mode: "strict" (default, zero loss, move-only), "balanced", or "aggressive"
            (aggressive enables lossy summarization — opt-in, always reported).

    Returns:
        dict with the active profile and its rules.
    """
    start = time.perf_counter()
    rl = _check_rate_limit("default")
    if rl is not None:
        _audit_call("ctx_profile", "default", start, "error", rl["error"])
        return rl
    from loom.profile import set_profile

    result = set_profile(mode)
    _audit_call("ctx_profile", "default", start, "success")
    return result


# --- Session Management Tools ---

@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_list_sessions(include_expired: bool = False) -> dict:
    """List all sessions with metadata and stats.

    Args:
        include_expired: if true, include sessions past their TTL (default: false)

    Returns:
        dict with list of sessions and their info.
    """
    start = time.perf_counter()
    rl = _check_rate_limit("default")
    if rl is not None:
        _audit_call("ctx_list_sessions", "default", start, "error", rl["error"])
        return rl
    sessions = session.list_sessions(include_expired=include_expired)
    result = {
        "sessions": [
            {
                "session_id": s.session_id,
                "label": s.label,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "chunk_count": s.chunk_count,
                "token_count": s.token_count,
                "anchor_count": s.anchor_count,
                "expired": s.expired,
            }
            for s in sessions
        ],
        "count": len(sessions),
    }
    _audit_call("ctx_list_sessions", "default", start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_purge_session(session_id: str) -> dict:
    """Permanently delete all data for a session (chunks, anchors, invariants, session row).

    Args:
        session_id: the session to purge (must be explicit, not "default")

    Returns:
        dict with deletion counts.
    """
    start = time.perf_counter()
    rl = _check_rate_limit(session_id)
    if rl is not None:
        _audit_call("ctx_purge_session", session_id, start, "error", rl["error"])
        return rl
    if session_id == "default":
        res = {"ok": False, "error": "session_id required (cannot purge 'default')"}
        _audit_call("ctx_purge_session", session_id, start, "error", res["error"])
        return res
    result = session.purge_session(session_id)
    _audit_call("ctx_purge_session", session_id, start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_purge_expired() -> dict:
    """Purge all sessions past their TTL.

    Returns:
        dict with purged count and details.
    """
    start = time.perf_counter()
    rl = _check_rate_limit("default")
    if rl is not None:
        _audit_call("ctx_purge_expired", "default", start, "error", rl["error"])
        return rl
    result = session.purge_expired_sessions()
    _audit_call("ctx_purge_expired", "default", start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_vacuum() -> dict:
    """Run SQLite VACUUM to reclaim unused space.

    Returns:
        dict with size before/after and bytes reclaimed.
    """
    start = time.perf_counter()
    rl = _check_rate_limit("default")
    if rl is not None:
        _audit_call("ctx_vacuum", "default", start, "error", rl["error"])
        return rl
    result = session.vacuum_db()
    _audit_call("ctx_vacuum", "default", start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_health() -> dict:
    """Health check for the Loom drawer (DB connectivity, sizes, counts, WAL mode).

    Returns:
        dict with health status and metrics.
    """
    start = time.perf_counter()
    rl = _check_rate_limit("default")
    if rl is not None:
        _audit_call("ctx_health", "default", start, "error", rl["error"])
        return rl
    result = session.health_check()
    _audit_call("ctx_health", "default", start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_store_file(path: str, session_id: str = "default", label: str = "") -> dict:
    """Read a file from disk and store its contents in the Loom drawer.

    Args:
        path: absolute or relative file path
        session_id: logical session the content belongs to
        label: short human-readable name (defaults to basename)

    Returns:
        dict with anchor, chunk ids, token counts, savings ratio, redactions.
    """
    start = time.perf_counter()
    rl = _check_rate_limit(session_id)
    if rl is not None:
        _audit_call("ctx_store_file", session_id, start, "error", rl["error"])
        return rl
    import os as _os
    if not label:
        label = _os.path.basename(path)

    max_mb = float(_os.environ.get("LOOM_MAX_FILE_MB", "50"))
    allowed = _os.environ.get("LOOM_ALLOWED_DIRS")
    resolved = _os.path.realpath(path)
    if allowed:
        dirs = [d for d in allowed.split(";;") if d]
        if not _path_allowed(resolved, dirs):
            res = {"error": f"path outside allowed dirs: {resolved}"}
            _audit_call("ctx_store_file", session_id, start, "error", res["error"])
            return res

    try:
        size = _os.path.getsize(resolved)
    except OSError as exc:
        res = {"error": str(exc)}
        _audit_call("ctx_store_file", session_id, start, "error", res["error"])
        return res
    if size > max_mb * 1024 * 1024:
        res = {"error": f"file too large: {size} bytes (limit {max_mb} MB)"}
        _audit_call("ctx_store_file", session_id, start, "error", res["error"])
        return res

    try:
        with open(resolved, "rb") as f:
            raw = f.read()
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        hint = (
            " File looks like UTF-16 (default of Windows PowerShell '>' / Out-File):"
            " re-save it as UTF-8."
            if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
            else " File is not valid UTF-8 (cp1252/latin-1?): re-save it as UTF-8."
        )
        res = {"error": f"invalid utf-8: {exc}.{hint}"}
        _audit_call("ctx_store_file", session_id, start, "error", res["error"])
        return res
    except OSError as exc:
        res = {"error": str(exc)}
        _audit_call("ctx_store_file", session_id, start, "error", res["error"])
        return res

    result = _store_impl(content, session_id, label)
    _audit_call("ctx_store_file", result["session_id"], start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    )
)
def ctx_store_cmd(cmd: str, session_id: str = "default", label: str = "") -> dict:
    """Run a shell command in a sandbox, capture its output, and store it in the Loom drawer.

    Requires LOOM_STORE_CMD=1 to be set (opt-in). Sandbox enforces CPU, memory,
    and process limits (no network isolation on Windows). Fails closed if sandbox unavailable.

    Args:
        cmd: shell command to execute
        session_id: logical session the output belongs to
        label: short human-readable name (defaults to cmd)

    Returns:
        dict with anchor, chunk ids, token counts, savings ratio, redactions.
    """
    start = time.perf_counter()
    rl = _check_rate_limit(session_id)
    if rl is not None:
        _audit_call("ctx_store_cmd", session_id, start, "error", rl["error"])
        return rl
    if os.environ.get("LOOM_STORE_CMD") != "1":
        res = {"error": "ctx_store_cmd disabled — set LOOM_STORE_CMD=1 to enable"}
        _audit_call("ctx_store_cmd", session_id, start, "error", res["error"])
        return res

    if not label:
        label = cmd[:60]

    sandbox = get_sandbox()
    if not sandbox.is_available():
        res = {"error": "sandbox unavailable (pywin32 on Windows, bwrap/firejail on Linux) — command rejected"}
        _audit_call("ctx_store_cmd", session_id, start, "error", res["error"])
        return res

    result = sandbox.run(cmd)
    if result.error:
        res = {"error": f"sandbox error: {result.error}", "sandboxed": result.sandboxed}
        _audit_call("ctx_store_cmd", session_id, start, "error", res["error"])
        return res

    content = result.stdout
    if result.stderr:
        content += "\n\n[stderr]\n" + result.stderr

    out = _store_impl(content, session_id, label)
    _audit_call("ctx_store_cmd", out["session_id"], start, "success")
    return out


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_search(query: str, limit: int = 5, session_id: str = "default", semantic: bool = True) -> dict:
    """Search stored chunks — semantic (embeddings) first, FTS5 fallback.

    When ``semantic=True`` (default) and the embedding backend is available,
    searches by *meaning* using FAISS HNSW. Falls back to FTS5 keyword search
    if no backend, no index, or no semantic results.

    Args:
        query: search query (natural language or keywords)
        limit: max results (default 5)
        session_id: session to search ("default" = the shared default session only)
        semantic: use semantic search first (default True)

    Returns:
        dict with matching chunks, metadata, and ``mode`` ("semantic" or "fts").
    """
    start = time.perf_counter()
    rl = _check_rate_limit(session_id)
    if rl is not None:
        _audit_call("ctx_search", session_id, start, "error", rl["error"])
        return rl

    mode = "fts"
    results: list[dict] = []

    if semantic:
        try:
            from loom import vector_index
            if vector_index.available():
                sid = session_id
                sem = store.search_semantic(query, session_id=sid, limit=limit)
                if sem:
                    mode = "semantic"
                    results = sem
        except Exception:
            pass  # fall through to FTS

    if not results:
        results = store.search(query, limit=limit, session_id=session_id, topup=True)

    result = {
        "query": query,
        "mode": mode,
        "results": [
            {"chunk_id": r["id"], "label": r["label"], "content": _excerpt(r["content"], query),
             "session_id": r.get("session_id", ""),
             **({"score": r["score"]} if "score" in r else {})}
            for r in results
        ],
        "count": len(results),
    }
    _audit_call("ctx_search", session_id, start, "success")
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_verify(session_id: str = "") -> dict:
    """Verify source integrity by rebuilding each source from its chunks.

    For every stored source, reconstructs the original bytes from its chunks
    (in chunk-id order, through the normal read path) and compares SHA-256
    to the value stored at ingest time.
    """
    start = time.perf_counter()
    key = session_id or "default"
    rl = _check_rate_limit(key)
    if rl is not None:
        _audit_call("ctx_verify", key, start, "error", rl["error"])
        return rl
    resolved = session.resolve_session_id(key) if session_id else None
    sources = store.load_sources(resolved)
    out = []
    all_ok = True
    total_redactions = 0
    for src in sources:
        sid = src["session_id"]
        first = src["first_chunk_id"]
        last = src["last_chunk_id"]
        # load chunks in id order via normal read path
        chunks = []
        if first is not None and last is not None:
            for cid in range(first, last + 1):
                ch = store.load_chunk(cid)
                if ch is not None and ch.get("session_id") == sid:
                    chunks.append(ch)
        rebuilt = "".join(c["content"] for c in chunks)
        sha_rebuilt = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
        ok = sha_rebuilt == src["sha256"]
        all_ok = all_ok and ok
        total_redactions += int(src.get("redactions", 0) or 0)
        out.append({
            "id": src["id"],
            "label": src["label"],
            "ok": ok,
            "sha_stored": src["sha256"],
            "sha_rebuilt": sha_rebuilt,
            "bytes": int(src["bytes"]),
        })
    result = {"sources": out, "lossless": bool(all_ok and total_redactions == 0)}
    _audit_call("ctx_verify", key, start, "success")
    return result


# =========================================================================
# P2 — Cross-session transfer
# =========================================================================

@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_transfer(from_session: str, to_session: str, chunk_ids: list[int]) -> dict:
    """Transfer chunks from one session to another (cross-session / multi-agent).

    Creates a junction-table link so the chunks become visible in the target
    session without duplicating data.  The original chunks remain in the source
    session.

    Args:
        from_session: source session ID
        to_session: target session ID
        chunk_ids: list of chunk IDs to transfer

    Returns:
        dict with transferred count and any errors.
    """
    start = time.perf_counter()
    rl = _check_rate_limit(to_session)
    if rl is not None:
        _audit_call("ctx_transfer", to_session, start, "error", rl["error"])
        return rl
    from loom import cross_session
    result = cross_session.transfer(from_session, to_session, chunk_ids)
    _audit_call("ctx_transfer", to_session, start, "success")
    return result


# =========================================================================
# P1 — Vector index management
# =========================================================================

@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_rebuild_index() -> dict:
    """Rebuild the FAISS HNSW vector index from all stored chunks.

    Call this once after installing sentence-transformers, or when the index
    is stale / corrupted.  Embeds every chunk in the DB and persists the
    index to ``~/.loom/vectors/``.

    Returns:
        dict with chunk count, elapsed seconds, and index path.
    """
    start = time.perf_counter()
    from loom import vector_index
    from loom.store import DB_PATH
    count = vector_index.rebuild_from_db(DB_PATH)
    elapsed = round(time.perf_counter() - start, 2)
    return {
        "embedded": count,
        "elapsed_s": elapsed,
        "index_path": str(vector_index.INDEX_PATH),
        "backend": vector_index._backend,
    }


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def ctx_vector_stats() -> dict:
    """Return vector index statistics (backend, model, dimension, vector count)."""
    from loom import vector_index
    return vector_index.stats()


def main() -> None:
    """Entry point for `loom-mcp` console script (stdio transport)."""
    logging.basicConfig(level=os.environ.get("LOOM_LOG_LEVEL", "INFO"))
    transport = os.environ.get("LOOM_TRANSPORT", "stdio").strip().lower()
    if transport not in ("stdio", "http", "streamable-http", "sse"):
        raise SystemExit(
            f"invalid LOOM_TRANSPORT={transport!r}: use stdio (default), http, streamable-http or sse"
        )
    log.info("starting loom-mcp (transport=%s)", transport)
    if transport != "stdio":
        host = os.environ.get("LOOM_HOST", "127.0.0.1")
        port = int(os.environ.get("LOOM_PORT", "8770"))
        if host not in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "loom-mcp has no authentication: binding to %s exposes the context store", host
            )
        mcp.run(transport=transport, host=host, port=port)
        return
    mcp.run()


if __name__ == "__main__":
    main()
