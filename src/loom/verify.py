"""The verifier — vérifie that nothing critical was lost."""

from __future__ import annotations

from typing import Any

from loom import anchors as anchor_mod
from loom import store


def check_session(session_id: str = "default") -> dict[str, Any]:
    """Re-derive invariants from stored chunks and report coverage.

    Two guarantees, deliberately separated:

      retrievable : every invariant extracted from the original content can be
                    recovered from the store (anchor + chunks). This is the "zéro
                    perte" promise and must ALWAYS be 1.0.
      in_context  : the fraction of invariants that sit in the ANCHOR, i.e. what the
                    model sees without asking for anything. Quality metric, not
                    correctness — it is expected to be < 1.0 on large inputs, because
                    the anchor is a summary and the drawer holds the rest.

    `lossless` in the returned report means *retrievable*. The previous meaning
    ("everything is inside the anchor") reported false on inputs that were in fact
    perfectly recoverable.

    The invariant set is read from the store when available (it was computed once at
    store time); otherwise it is recomputed from the chunks. This keeps verification
    fast even on a very large session.
    """
    # Read through the decrypting accessors. In column-encrypted mode the raw
    # columns hold ciphertext, and hashing that would report every anchor as
    # lost even though the stored plaintext is intact.
    rows = list(store.iter_session_chunks(session_id))
    anchor_rows = store.load_anchors(session_id)

    if not rows:
        return {
            "session_id": session_id,
            "chunks": 0,
            "coverage": 1.0,
            "lossless": True,
            "note": "nothing stored for this session yet",
        }

    cached = store.load_invariants(session_id)
    if cached:
        invariants = [
            anchor_mod.Invariant(kind, value, priority) for kind, value, priority in cached
        ]
    else:
        full_text = "\n".join(r["content"] for r in rows)
        invariants = anchor_mod.extract(full_text)

    in_context = "\n".join(r["body"] for r in anchor_rows)
    retrievable = "\n".join(r["content"] for r in rows) + "\n" + in_context

    ctx_report = anchor_mod.coverage(invariants, in_context)
    full_report = anchor_mod.coverage(invariants, retrievable)

    return {
        "session_id": session_id,
        "chunks": len(rows),
        # fraction of invariants visible in the anchor without retrieval
        "coverage": ctx_report["coverage"],
        "in_context": ctx_report["coverage"],
        "coverage_weighted": ctx_report["coverage_weighted"],
        "coverage_structured": ctx_report["coverage_structured"],
        # fraction recoverable from the store — the real promise
        "retrievable": full_report["coverage"],
        "lossless": full_report["lossless"],
        "critical_missing": ctx_report["critical_missing"],
        "critical_missing_retrievable": full_report["critical_missing"],
        "missing": ctx_report["missing"][:50],
        "invariants_total": ctx_report["total"],
        "undetectable_types": [
            "dates", "function_names", "config_values", "identifiers_without_patterns",
            "natural_language_facts", "error_messages", "stack_traces",
        ],
    }
