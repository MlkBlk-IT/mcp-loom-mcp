#!/usr/bin/env python3
"""loom-dash: read-only real-time dashboard for the Loom MCP. Stdlib only."""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import mimetypes
import os
import sqlite3
import threading
import time
import http.server
import socketserver
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
PORT = int(os.environ.get("LOOM_DASH_PORT", "8775"))

db_path = None
audit_path = None
demo = False

events = collections.deque(maxlen=5000)
audit_offset = 0
audit_lock = threading.Lock()
events_lock = threading.Lock()


def parse_ts(s):
    if not s:
        return time.time()
    s = str(s).strip()
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time()


def open_db_ro(path):
    if not path or not os.path.exists(path):
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.row_factory = sqlite3.Row
    return conn


def db_size_bytes(path):
    total = 0
    try:
        total += os.path.getsize(path)
    except OSError:
        pass
    for suf in ("-wal", "-shm"):
        try:
            total += os.path.getsize(path + suf)
        except OSError:
            pass
    return total


def collect_db(path):
    out = {
        "exists": bool(path and os.path.exists(path)),
        "encrypted": False,
        "error": None,
        "path": path,
        "size_bytes": db_size_bytes(path) if (path and os.path.exists(path)) else 0,
        "db_size": (os.path.getsize(path) if (path and os.path.exists(path)) else 0),
        "wal_size": 0,
        "sessions": 0,
        "chunks": 0,
        "sources": 0,
        "anchors": 0,
        "invariants": 0,
        "invariants_by_kind": [],
        "hits_per_session": [],
        "tokens_per_store": [],
        "chunk_map": [],
    }
    if path and os.path.exists(path):
        try:
            out["wal_size"] = os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0
        except OSError:
            pass
    if not (path and os.path.exists(path)):
        out["error"] = "missing"
        return out
    conn = None
    try:
        conn = open_db_ro(path)
        # quick encryption probe: try reading a text column from chunks
        try:
            conn.execute("SELECT content FROM chunks LIMIT 1").fetchone()
        except Exception as e:
            out["encrypted"] = True
            out["error"] = "encrypted or unreadable: %s" % e
            # still try counts via sqlite_master? counts need table access; may fail
            conn.close()
            conn = None
            return out
        # counts
        out["sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        out["chunks"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        out["sources"] = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        out["anchors"] = conn.execute("SELECT COUNT(*) FROM anchors").fetchone()[0]
        out["invariants"] = conn.execute("SELECT COUNT(*) FROM invariants").fetchone()[0]
        # invariants by kind
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM invariants GROUP BY kind ORDER BY n DESC"
        ).fetchall()
        out["invariants_by_kind"] = [{"kind": r["kind"], "n": r["n"]} for r in rows]
        # hits per session
        rows = conn.execute(
            "SELECT session_id, COALESCE(SUM(hits),0) AS h FROM chunks GROUP BY session_id ORDER BY h DESC"
        ).fetchall()
        out["hits_per_session"] = [{"session_id": r["session_id"], "hits": r["h"]} for r in rows]
        # tokens per store (sources)
        rows = conn.execute(
            "SELECT id, label, tokens_before, anchor_tokens FROM sources ORDER BY id"
        ).fetchall()
        out["tokens_per_store"] = [
            {"id": r["id"], "label": r["label"], "tokens_before": r["tokens_before"], "anchor_tokens": r["anchor_tokens"]}
            for r in rows
        ]
        # chunk map (aggregates only, no content/body)
        rows = conn.execute(
            "SELECT id, session_id, source_sha, idx, hits FROM chunks ORDER BY id"
        ).fetchall()
        # map source_sha -> index
        srcs = [r["source_sha"] for r in rows]
        unique_src = list(dict.fromkeys(srcs))
        src_index = {s: i for i, s in enumerate(unique_src)}
        # session index
        sessions = [r["session_id"] for r in rows]
        unique_sess = list(dict.fromkeys(sessions))
        sess_index = {s: i for i, s in enumerate(unique_sess)}
        chunk_map = []
        for r in rows:
            chunk_map.append({
                "id": r["id"],
                "session_id": r["session_id"],
                "source_idx": src_index.get(r["source_sha"], 0),
                "idx": (r["idx"] if r["idx"] is not None else r["id"]),
                "hits": r["hits"],
                "session_idx": sess_index.get(r["session_id"], 0),
            })
        # cap 5000
        if len(chunk_map) > 5000:
            # sample
            step = len(chunk_map) / 5000.0
            sampled = []
            i = 0.0
            while int(i) < len(chunk_map) and len(sampled) < 5000:
                sampled.append(chunk_map[int(i)])
                i += step
            chunk_map = sampled
        out["chunk_map"] = chunk_map

        # --- Loom upgrade metrics (P1/P2/P3) ---
        # P1: Vector index stats
        try:
            vectors_dir = Path.home() / ".loom" / "vectors"
            idx_path = vectors_dir / "hnsw.index"
            meta_path = vectors_dir / "meta.json"
            out["vector_index"] = {
                "available": idx_path.exists(),
                "index_size_bytes": idx_path.stat().st_size if idx_path.exists() else 0,
                "n_vectors": 0,
                "model": "all-MiniLM-L6-v2",
                "dim": 384,
            }
            if meta_path.exists():
                import json as _json
                meta = _json.loads(meta_path.read_text())
                out["vector_index"]["n_vectors"] = len(meta.get("chunk_ids", []))
                out["vector_index"]["dim"] = meta.get("dim", 384)
        except Exception:
            out["vector_index"] = {"available": False, "n_vectors": 0}

        # P2: Cross-session transfers
        try:
            xfer_rows = conn.execute(
                "SELECT COUNT(*) FROM session_chunks"
            ).fetchone()
            out["cross_session_transfers"] = xfer_rows[0]
            # sessions involved in transfers
            xfer_sessions = conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM session_chunks"
            ).fetchone()
            out["cross_session_linked"] = xfer_sessions[0]
        except sqlite3.Error:
            out["cross_session_transfers"] = 0
            out["cross_session_linked"] = 0

        # P3: Auto-store hits (from audit if available, else 0)
        out["auto_store_hits"] = 0

    except sqlite3.Error as e:
        out["error"] = "sqlite error: %s" % e
        out["encrypted"] = ("not a database" in str(e).lower() or "encrypted" in str(e).lower())
    except Exception as e:
        out["error"] = "error: %s" % e
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def tail_audit():
    global audit_offset
    if not audit_path or not os.path.exists(audit_path):
        return
    try:
        size = os.path.getsize(audit_path)
    except OSError:
        return
    with audit_lock:
        if size < audit_offset:
            audit_offset = 0
        if size == audit_offset:
            return
        try:
            with open(audit_path, "rb") as f:
                f.seek(audit_offset)
                data = f.read(size - audit_offset)
                audit_offset = size
        except OSError:
            return
    # parse lines
    text = data.decode("utf-8", errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        # normalize ts
        ts_raw = obj.get("ts") or obj.get("timestamp")
        ts_epoch = parse_ts(ts_raw)
        # normalize key to ts for frontend (keep original too)
        ev = {
            "request_id": obj.get("request_id"),
            "session_id": obj.get("session_id", "default"),
            "tool": obj.get("tool", "unknown"),
            "latency_ms": obj.get("latency_ms", 0),
            "outcome": obj.get("outcome", "success"),
            "error": obj.get("error"),
            "ts": ts_raw,
            "ts_epoch": ts_epoch,
        }
        with events_lock:
            events.append(ev)


def audit_metrics():
    now = time.time()
    cutoff_60 = now - 60
    cutoff_15m = now - 15 * 60
    with events_lock:
        evs = list(events)
    recent = [e for e in evs if e["ts_epoch"] >= cutoff_60]
    calls_per_min = len(recent)
    errors = sum(1 for e in recent if str(e.get("outcome", "")).lower() not in ("success", "ok", ""))
    # error rate over recent
    error_rate = (errors / calls_per_min) if calls_per_min else 0.0
    latencies = sorted(e.get("latency_ms", 0) or 0 for e in recent)
    def pct(p):
        if not latencies:
            return 0.0
        idx = int((len(latencies) - 1) * p)
        idx = max(0, min(len(latencies) - 1, idx))
        return latencies[idx]
    p50 = pct(0.5)
    p95 = pct(0.95)
    # calls per tool per minute for last 15 min (stacked area): bucket by minute
    # minute buckets: 15 buckets, index 0 = oldest
    bucket_count = 15
    bucket_size = 60
    # map tool -> list of 15
    tools_set = sorted({e["tool"] for e in evs if e["ts_epoch"] >= cutoff_15m})
    series = {t: [0] * bucket_count for t in tools_set}
    minute_labels = []
    for b in range(bucket_count):
        minute_labels.append("")  # filled below
    # compute bucket index for each event
    for e in evs:
        if e["ts_epoch"] < cutoff_15m:
            continue
        age = now - e["ts_epoch"]
        bi = bucket_count - 1 - int(age // bucket_size)
        if bi < 0 or bi >= bucket_count:
            continue
        t = e["tool"]
        if t not in series:
            series[t] = [0] * bucket_count
            tools_set.append(t)
        series[t][bi] += 1
    # minute labels as relative minutes (-14..0)
    labels = ["-%dm" % (bucket_count - 1 - i) for i in range(bucket_count)]
    labels[-1] = "now"
    # latency series per minute (p50/p95)
    lat_p50 = [None] * bucket_count
    lat_p95 = [None] * bucket_count
    for b in range(bucket_count):
        bucket_evs = [e for e in evs if (lambda age: (bucket_count - 1 - int(age // bucket_size)) == b)(now - e["ts_epoch"]) and e["ts_epoch"] >= cutoff_15m]
        ls = sorted(e.get("latency_ms", 0) or 0 for e in bucket_evs)
        if ls:
            i50 = max(0, min(len(ls) - 1, int((len(ls) - 1) * 0.5)))
            i95 = max(0, min(len(ls) - 1, int((len(ls) - 1) * 0.95)))
            lat_p50[b] = ls[i50]
            lat_p95[b] = ls[i95]
    # outcomes
    outcomes = collections.Counter()
    for e in recent:
        outcomes[e.get("outcome", "success")] += 1
    # recent events (last 50)
    recent_events = sorted(evs, key=lambda x: x["ts_epoch"], reverse=True)[:50]
    recent_events_out = [
        {"tool": e["tool"], "session_id": e["session_id"], "outcome": e["outcome"],
         "latency_ms": e["latency_ms"], "ts": e["ts"]}
        for e in recent_events
    ]
    return {
        "total_events": len(evs),
        "calls_per_min": calls_per_min,
        "error_rate": round(error_rate, 4),
        "p50": round(p50, 2),
        "p95": round(p95, 2),
        "series_tools": sorted(series.keys()),
        "series_labels": labels,
        "series_counts": {t: series[t] for t in series},
        "latency_series_labels": labels,
        "latency_p50_series": lat_p50,
        "latency_p95_series": lat_p95,
        "outcomes": dict(outcomes),
        "recent_events": recent_events_out,
    }


def saved_ratio(tokens_per_store):
    total_before = 0
    total_anchor = 0
    for s in tokens_per_store:
        total_before += s.get("tokens_before", 0) or 0
        total_anchor += s.get("anchor_tokens", 0) or 0
    if total_before == 0:
        return 0.0
    return round((total_before - total_anchor) / total_before, 4)


def build_snapshot():
    if demo:
        return demo_snapshot()
    db = collect_db(db_path)
    am = audit_metrics()
    snap = {
        "ts": time.time(),
        "demo": False,
        "db": db,
        "audit": am,
    }
    snap["db"]["saved_ratio"] = saved_ratio(db["tokens_per_store"])
    return snap


def demo_snapshot():
    import random
    random.seed(int(time.time()) // 5)
    sessions = 5
    chunks = 42
    sources = 3
    db = {
        "exists": True,
        "encrypted": False,
        "error": None,
        "path": "(demo)",
        "size_bytes": 1234567,
        "db_size": 1200000,
        "wal_size": 34567,
        "sessions": sessions,
        "chunks": chunks,
        "sources": sources,
        "anchors": 5,
        "invariants": 12,
        "invariants_by_kind": [
            {"kind": "fact", "n": 5},
            {"kind": "constraint", "n": 4},
            {"kind": "decision", "n": 3},
        ],
        "hits_per_session": [
            {"session_id": "default", "hits": 12},
            {"session_id": "sess-a", "hits": 7},
            {"session_id": "sess-b", "hits": 3},
        ],
        "tokens_per_store": [
            {"id": 1, "label": "doc1", "tokens_before": 5000, "anchor_tokens": 800},
            {"id": 2, "label": "doc2", "tokens_before": 3200, "anchor_tokens": 450},
            {"id": 3, "label": "doc3", "tokens_before": 1800, "anchor_tokens": 300},
        ],
        "chunk_map": [],
    }
    # chunk map demo
    cm = []
    sid = 0
    for i in range(chunks):
        cm.append({
            "id": i + 1,
            "session_id": "default" if i % 3 == 0 else ("sess-a" if i % 3 == 1 else "sess-b"),
            "source_idx": i % sources,
            "idx": i,
            "hits": random.randint(0, 8),
            "session_idx": i % 3,
        })
    db["chunk_map"] = cm
    db["saved_ratio"] = saved_ratio(db["tokens_per_store"])
    # --- upgrade metrics demo ---
    db["vector_index"] = {
        "available": True,
        "backend": "st",
        "index_size_bytes": 2_150_000,
        "n_vectors": 107,
        "model": "all-MiniLM-L6-v2",
        "dim": 384,
    }
    db["cross_session_transfers"] = 2
    db["cross_session_linked"] = 2
    db["auto_store_hits"] = 1
    # audit demo: generate events if empty-ish
    now = time.time()
    with events_lock:
        if len(events) < 20:
            for i in range(60):
                events.append({
                    "request_id": "demo-%d" % i,
                    "session_id": "default",
                    "tool": random.choice(["ctx_store", "ctx_get", "ctx_check", "ctx_list"]),
                    "latency_ms": round(random.uniform(5, 120), 2),
                    "outcome": "success" if random.random() > 0.1 else "error",
                    "error": None,
                    "ts": datetime.datetime.fromtimestamp(now - random.randint(0, 900), tz=datetime.timezone.utc).isoformat(),
                    "ts_epoch": now - random.randint(0, 900),
                })
    am = audit_metrics()
    am["demo"] = True
    return {"ts": now, "demo": True, "db": db, "audit": am}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path):
        # path like /static/foo
        base = Path(__file__).resolve().parent / "static"
        rel = path[len("/static/"):]
        # prevent traversal
        target = (base / rel).resolve()
        if not target.is_relative_to(base.resolve()):
            self.send_error(403)
            return
        if not target.exists() or not target.is_file():
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(str(target))
        if mime is None:
            mime = "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            self.send_error(403)
            return
        if p == "/" or p == "/index.html":
            self._serve_static("/static/index.html")
            return
        if p.startswith("/static/"):
            self._serve_static(p)
            return
        if p == "/api/snapshot":
            snap = build_snapshot()
            self._send_json(snap)
            return
        if p == "/api/stream":
            self._sse()
            return
        self.send_error(404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Cache-Control")
        self.end_headers()
        try:
            while True:
                snap = build_snapshot()
                payload = json.dumps(snap)
                self.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception:
            return


def audit_tailer_loop():
    while True:
        tail_audit()
        time.sleep(1)


def main():
    global db_path, audit_path, demo
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--audit", default=None)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    if args.db:
        db_path = args.db
    else:
        db_path = os.path.expanduser("~/.loom/loom.db")
    if args.audit:
        apath = args.audit
        if os.path.isdir(apath):
            audit_path = os.path.join(apath, "audit.jsonl")
        else:
            audit_path = apath
    else:
        audit_path = os.path.expanduser("~/.loom/audit/audit.jsonl")
    demo = args.demo
    # initial tail
    tail_audit()
    # start tailer thread
    t = threading.Thread(target=audit_tailer_loop, daemon=True)
    t.start()
    httpd = socketserver.ThreadingTCPServer((HOST, PORT), Handler)
    httpd.allow_reuse_address = True
    print("loom-dash listening on http://%s:%d (db=%s, audit=%s, demo=%s)" % (HOST, PORT, db_path, audit_path, demo))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
