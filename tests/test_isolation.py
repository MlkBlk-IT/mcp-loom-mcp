"""Isolation tests — prove tests cannot touch the real ~/.loom drawer.

The conftest canary already fails the whole run if ~/.loom changes; these
tests make the isolation contract explicit and testable on their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loom import session, store


REAL_LOOM = Path.home() / ".loom"


def _stat(p: Path):
    try:
        s = p.stat()
        return (s.st_size, s.st_mtime_ns)
    except FileNotFoundError:
        return None


def test_store_db_path_is_tmp(monkeypatch, tmp_path):
    """store.DB_PATH must point at the tmp drawer, never ~/.loom."""
    db = tmp_path / "loom.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    assert store.DB_PATH == db
    assert "pytest" in str(store.DB_PATH) or str(tmp_path) in str(store.DB_PATH)
    assert store.DB_PATH != REAL_LOOM / "loom.db"


def test_store_connect_writes_tmp(tmp_path, monkeypatch):
    """A store write must create the tmp DB, not ~/.loom/loom.db."""
    db = tmp_path / "iso.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setenv("LOOM_DB", str(db))

    ids = store.save_chunks("iso-test", "log", [{"text": "hello", "tokens": 1}])
    assert len(ids) == 1
    assert db.exists()
    assert db.stat().st_size > 0


def test_session_health_check_uses_tmp(tmp_path, monkeypatch):
    """session.health_check() must report the tmp DB path."""
    db = tmp_path / "iso.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setenv("LOOM_DB", str(db))

    result = session.health_check()
    assert result["ok"] is True
    assert result["db_path"] == str(db)


def test_real_loom_untouched_during_store_write(tmp_path, monkeypatch):
    """Writing chunks must not change the real ~/.loom/loom.db."""
    before = _stat(REAL_LOOM / "loom.db")

    db = tmp_path / "iso.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setenv("LOOM_DB", str(db))
    store.save_chunks("iso-test2", "log", [{"text": "isolation", "tokens": 1}])

    after = _stat(REAL_LOOM / "loom.db")
    assert before == after, f"~/.loom changed: {before} -> {after}"


def test_session_isolation_ctx_get(tmp_path, monkeypatch):
    """ctx_get must not cross session boundaries without explicit transfer."""
    db = tmp_path / "iso.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setenv("LOOM_DB", str(db))

    from loom import server

    # Store in session A
    res_a = server.ctx_store("secret-A", session_id="session-A", label="a")
    assert res_a["chunk_ids"]
    cid_a = res_a["chunk_ids"][0]

    # Store in session B
    res_b = server.ctx_store("secret-B", session_id="session-B", label="b")
    assert res_b["chunk_ids"]
    cid_b = res_b["chunk_ids"][0]

    # Get from session A - should only see its own
    get_a = server.ctx_get(cid_a, session_id="session-A")
    assert get_a["found"] is True
    assert get_a["content"] == "secret-A"

    # Get from session B - should only see its own
    get_b = server.ctx_get(cid_b, session_id="session-B")
    assert get_b["found"] is True
    assert get_b["content"] == "secret-B"

    # Cross-session get without transfer should fail
    cross_a_from_b = server.ctx_get(cid_a, session_id="session-B")
    assert cross_a_from_b["found"] is False
    assert "not found or not accessible" in cross_a_from_b["error"]

    cross_b_from_a = server.ctx_get(cid_b, session_id="session-A")
    assert cross_b_from_a["found"] is False
    assert "not found or not accessible" in cross_b_from_a["error"]


def test_session_isolation_ctx_search(tmp_path, monkeypatch):
    """ctx_search must not cross session boundaries without explicit transfer."""
    db = tmp_path / "iso.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setenv("LOOM_DB", str(db))

    from loom import server

    # Store distinct content in each session
    server.ctx_store("alpha unique-content", session_id="sess1")
    server.ctx_store("beta unique-content", session_id="sess2")

    # Search from sess1 should only find alpha
    search1 = server.ctx_search("alpha", session_id="sess1", limit=10)
    assert search1["count"] == 1
    assert "alpha" in search1["results"][0]["content"]

    # Search from sess2 should only find beta
    search2 = server.ctx_search("beta", session_id="sess2", limit=10)
    assert search2["count"] == 1
    assert "beta" in search2["results"][0]["content"]

    # Cross search should not leak
    search1_cross = server.ctx_search("beta", session_id="sess1", limit=10)
    assert search1_cross["count"] == 0

    search2_cross = server.ctx_search("alpha", session_id="sess2", limit=10)
    assert search2_cross["count"] == 0
