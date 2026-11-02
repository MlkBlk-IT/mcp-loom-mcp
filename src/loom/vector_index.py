"""Loom — local semantic search via embeddings + FAISS HNSW.

Lazy-loads the embedding model only when first needed, so the server starts
instantly even without the optional deps installed.  Two backends are
supported:

1. ``sentence-transformers`` (preferred, per spec) — pulls torch.
2. ``onnxruntime`` + ``tokenizers``  — lightweight CPU-only fallback.

If neither is available, ``available()`` returns ``False`` and callers fall
back to the existing FTS5 keyword search.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# ---- paths -----------------------------------------------------------------

VECTORS_DIR = Path(os.environ.get("LOOM_VECTORS", Path.home() / ".loom" / "vectors"))
INDEX_PATH = VECTORS_DIR / "hnsw.index"
META_PATH = VECTORS_DIR / "meta.json"
MODEL_NAME = os.environ.get("LOOM_EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_DIM = 384  # all-MiniLM-L6-v2 output size

# ---- singleton state -------------------------------------------------------

_model: Any = None
_model_lock = threading.Lock()
_backend: str | None = None  # "st" | "onnx" | None

_index: Any = None          # faiss.IndexHNSW
_index_lock = threading.Lock()
_chunk_ids: list[int] = []   # maps row position -> chunk id in DB


# =========================================================================
# Embedding model (lazy singleton)
# =========================================================================

def _try_sentence_transformers() -> Any | None:
    """Load via sentence-transformers if installed."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None
    log.info("vector_index: loading model %s via sentence-transformers", MODEL_NAME)
    m = SentenceTransformer(MODEL_NAME, device="cpu")
    return m


def _try_onnx() -> Any | None:
    """Lightweight fallback: ONNX runtime + HF tokenizers."""
    try:
        import json
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer
    except ImportError:
        return None

    # Download ONNX export of the model (small, ~90 MB).
    repo = f"sentence-transformers/{MODEL_NAME}"
    log.info("vector_index: loading ONNX model from %s", repo)
    model_dir = snapshot_download(
        repo_id=repo,
        allow_patterns=["onnx/*", "tokenizer.json", "config.json", "special_tokens_map.json"],
    )
    model_path = os.path.join(model_dir, "onnx", "model.onnx")
    tokenizer_path = os.path.join(model_dir, "tokenizer.json")

    sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(tokenizer_path)
    tok.enable_padding(length=None, pad_id=0, pad_token="[PAD]")
    tok.enable_truncation(max_length=256)

    class _OnnxModel:
        def encode(self, texts, **kw):
            if isinstance(texts, str):
                texts = [texts]
            enc = tok.encode_batch(texts)
            input_ids = np.array([e.ids for e in enc], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            token_type_ids = np.zeros_like(input_ids)
            outputs = sess.run(None, {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            })
            # Mean pooling over tokens with attention mask.
            token_embs = outputs[0]
            mask = attention_mask[:, :, None].astype(np.float32)
            summed = (token_embs * mask).sum(axis=1)
            counts = mask.sum(axis=1).clip(min=1e-9)
            return (summed / counts).astype(np.float32)

    return _OnnxModel()


def _get_model() -> Any | None:
    """Return the embedding model singleton, or ``None`` if unavailable."""
    global _model, _backend
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        m = _try_sentence_transformers()
        if m is not None:
            _backend = "st"
        else:
            m = _try_onnx()
            if m is not None:
                _backend = "onnx"
        _model = m
        if m is None:
            log.warning("vector_index: no embedding backend available — semantic search disabled")
        else:
            log.info("vector_index: backend=%s, dim=%d", _backend, EMBED_DIM)
    return _model


def available() -> bool:
    """True if an embedding backend is installed."""
    return _get_model() is not None


def embed(text: str) -> np.ndarray | None:
    """Embed a single text. Returns a 384-dim float32 vector, or ``None``."""
    m = _get_model()
    if m is None:
        return None
    vec = m.encode(text, normalize_embeddings=True, convert_to_numpy=True)
    return np.asarray(vec, dtype=np.float32)


def embed_batch(texts: list[str]) -> np.ndarray | None:
    """Embed multiple texts at once (batched for GPU/parallel efficiency)."""
    m = _get_model()
    if m is None:
        return None
    vecs = m.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                    batch_size=min(64, len(texts)))
    return np.asarray(vecs, dtype=np.float32)


# =========================================================================
# FAISS HNSW index
# =========================================================================

def _build_index() -> Any:
    import faiss
    idx = faiss.IndexHNSWFlat(EMBED_DIM, 32, faiss.METRIC_INNER_PRODUCT)
    idx.hnsw.efConstruction = 80
    idx.hnsw.efSearch = 48
    return idx


def _load_index() -> Any:
    """Load FAISS index from disk, or create empty."""
    global _index, _chunk_ids
    if _index is not None:
        return _index
    with _index_lock:
        if _index is not None:
            return _index
        import faiss
        if INDEX_PATH.exists():
            _index = faiss.read_index(str(INDEX_PATH))
            import json
            meta = json.loads(META_PATH.read_text())
            _chunk_ids = meta.get("chunk_ids", [])
            log.info("vector_index: loaded %d vectors from %s", len(_chunk_ids), INDEX_PATH)
        else:
            _index = _build_index()
            _chunk_ids = []
    return _index


def add(chunk_id: int, text: str) -> bool:
    """Embed *text* and add to the HNSW index. Returns False if no backend."""
    vec = embed(text)
    if vec is None:
        return False
    idx = _load_index()
    with _index_lock:
        idx.add(np.array([vec], dtype=np.float32))
        _chunk_ids.append(chunk_id)
    return True


def add_batch(chunk_ids: list[int], texts: list[str]) -> int:
    """Embed and add multiple chunks. Returns number successfully added."""
    if not chunk_ids:
        return 0
    vecs = embed_batch(texts)
    if vecs is None:
        return 0
    idx = _load_index()
    with _index_lock:
        idx.add(vecs.astype(np.float32))
        _chunk_ids.extend(chunk_ids)
    return len(chunk_ids)


def search(query: str, k: int = 10) -> list[dict[str, Any]]:
    """Semantic search: returns list of ``{chunk_id, score}`` sorted by relevance."""
    vec = embed(query)
    if vec is None:
        return []
    idx = _load_index()
    with _index_lock:
        if idx.ntotal == 0:
            return []
        k_eff = min(k, idx.ntotal)
        scores, indices = idx.search(np.array([vec], dtype=np.float32), k_eff)
    results = []
    for score, pos in zip(scores[0], indices[0]):
        if pos < 0 or pos >= len(_chunk_ids):
            continue
        results.append({"chunk_id": _chunk_ids[pos], "score": float(score)})
    return results


def save() -> None:
    """Persist FAISS index + chunk-id mapping to disk."""
    if _index is None:
        return
    import json
    VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    with _index_lock:
        import faiss
        faiss.write_index(_index, str(INDEX_PATH))
        META_PATH.write_text(json.dumps({"chunk_ids": _chunk_ids, "dim": EMBED_DIM}))
    log.info("vector_index: saved %d vectors", len(_chunk_ids))


def rebuild_from_db(db_path: Path) -> int:
    """Rebuild the entire vector index from chunks in the DB.

    Called once after install or when the index is stale.  Returns the number
    of chunks embedded.
    """
    import sqlite3
    from loom import crypto
    conn = crypto.connect_encrypted(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, content FROM chunks ORDER BY id").fetchall()
    conn.close()
    if not rows:
        return 0
    global _index, _chunk_ids
    import faiss
    _index = _build_index()
    _chunk_ids = []
    batch_size = 128
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        texts = [r["content"] for r in batch]
        ids = [r["id"] for r in batch]
        n = add_batch(ids, texts)
        total += n
        log.info("vector_index: rebuilt %d/%d", total, len(rows))
    save()
    return total


def stats() -> dict[str, Any]:
    """Return index statistics for the dashboard."""
    idx = _load_index()
    with _index_lock:
        return {
            "available": available(),
            "backend": _backend or "none",
            "model": MODEL_NAME,
            "dim": EMBED_DIM,
            "n_vectors": int(idx.ntotal) if idx else 0,
            "index_path": str(INDEX_PATH) if INDEX_PATH.exists() else None,
        }
