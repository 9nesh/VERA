"""
Actian VectorAI DB client for VERA — with local numpy fallback.

Primary:  Actian VectorAI DB (gRPC, Docker on port 50051)
Fallback: In-process numpy cosine-similarity store (persisted to disk)

The fallback activates automatically when Actian is unreachable, so the
full semantic-search pipeline works in any environment. On Linux/x86 the
Actian container runs natively; on Apple Silicon it falls back gracefully.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pickle
from pathlib import Path
from typing import Any

import numpy as np

from backend.config import ACTIAN_HOST, COLLECTION_NAME, EMBED_DIM

log = logging.getLogger("vera.vector_store")

# Disk paths for the local fallback store
_INDEX_DIR = Path(__file__).parent.parent.parent / "index"
_VECTORS_PATH = _INDEX_DIR / "vectors.npy"
_PAYLOADS_PATH = _INDEX_DIR / "payloads.pkl"

# In-memory state for the local store
_local_vectors: np.ndarray | None = None   # shape (N, EMBED_DIM), float32, L2-normalised
_local_payloads: list[dict[str, Any]] = []
_local_ids: list[int] = []
_local_dirty = False

# ---------------------------------------------------------------------------
# Actian VectorAI DB — sync helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _actian_health_sync() -> dict[str, str]:
    from cortex import CortexClient  # type: ignore[import]
    with CortexClient(ACTIAN_HOST) as client:
        version, uptime = client.health_check()
        return {"version": str(version), "uptime": str(uptime)}


def _actian_count_sync() -> int:
    from cortex import CortexClient  # type: ignore[import]
    with CortexClient(ACTIAN_HOST) as client:
        if not client.has_collection(COLLECTION_NAME):
            return 0
        return client.count(COLLECTION_NAME)


def _actian_ensure_collection_sync() -> None:
    from cortex import CortexClient, DistanceMetric  # type: ignore[import]
    with CortexClient(ACTIAN_HOST) as client:
        if not client.has_collection(COLLECTION_NAME):
            client.create_collection(
                name=COLLECTION_NAME,
                dimension=EMBED_DIM,
                distance_metric=DistanceMetric.COSINE,
            )
            log.info("Created Actian collection '%s'", COLLECTION_NAME)


def _actian_reset_collection_sync() -> None:
    from cortex import CortexClient, DistanceMetric  # type: ignore[import]
    with CortexClient(ACTIAN_HOST) as client:
        if client.has_collection(COLLECTION_NAME):
            client.delete_collection(COLLECTION_NAME)
        client.create_collection(
            name=COLLECTION_NAME,
            dimension=EMBED_DIM,
            distance_metric=DistanceMetric.COSINE,
        )
        log.info("Reset Actian collection '%s'", COLLECTION_NAME)


def _actian_upsert_sync(items: list[dict[str, Any]]) -> None:
    from cortex import CortexClient  # type: ignore[import]
    ids = [item["id"] for item in items]
    vectors = [item["vector"] for item in items]
    payloads = [item["payload"] for item in items]
    with CortexClient(ACTIAN_HOST) as client:
        client.batch_upsert(COLLECTION_NAME, ids=ids, vectors=vectors, payloads=payloads)


def _actian_search_sync(
    vector: list[float],
    top_k: int,
    filters: dict[str, str] | None,
) -> list[Any]:
    from cortex import CortexClient  # type: ignore[import]
    with CortexClient(ACTIAN_HOST) as client:
        if filters:
            from cortex.filters import Filter, Field  # type: ignore[import]
            f = Filter()
            for key, val in filters.items():
                f = f.must(Field(key).eq(val))
            return client.search_filtered(COLLECTION_NAME, vector, f, top_k=top_k)
        return client.search(COLLECTION_NAME, vector, top_k=top_k)


import socket as _socket
import time as _time

_actian_cache: dict[str, Any] = {"ok": None, "ts": 0.0}
_ACTIAN_CACHE_TTL = 120.0  # re-probe at most once per 2 minutes


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Fast TCP pre-check — returns False immediately if port is closed."""
    try:
        with _socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _actian_host_port() -> tuple[str, int]:
    parts = ACTIAN_HOST.rsplit(":", 1)
    return parts[0], int(parts[1]) if len(parts) == 2 else 50051


async def _actian_available() -> bool:
    """Return True if Actian VectorAI DB is reachable.
    Uses a fast TCP pre-check (0.5 s max) before attempting gRPC,
    and caches the result for 120 s to avoid thread-pool exhaustion.
    """
    now = _time.monotonic()
    if _actian_cache["ok"] is not None and (now - _actian_cache["ts"]) < _ACTIAN_CACHE_TTL:
        return bool(_actian_cache["ok"])

    host, port = _actian_host_port()

    # Fast TCP check first — avoids spawning a gRPC thread when port is closed
    port_up = await asyncio.to_thread(_port_open, host, port)
    if not port_up:
        _actian_cache.update(ok=False, ts=now)
        return False

    # Port is open — try the gRPC health check
    try:
        await asyncio.wait_for(asyncio.to_thread(_actian_health_sync), timeout=4.0)
        _actian_cache.update(ok=True, ts=now)
        return True
    except Exception:
        _actian_cache.update(ok=False, ts=now)
        return False


# ---------------------------------------------------------------------------
# Local numpy fallback store
# ---------------------------------------------------------------------------

def _load_local_store() -> None:
    global _local_vectors, _local_payloads, _local_ids
    _INDEX_DIR.mkdir(parents=True, exist_ok=True)
    if _VECTORS_PATH.exists() and _PAYLOADS_PATH.exists():
        try:
            _local_vectors = np.load(str(_VECTORS_PATH))
            with open(_PAYLOADS_PATH, "rb") as f:
                data = pickle.load(f)
                _local_payloads = data.get("payloads", [])
                _local_ids = data.get("ids", [])
            log.info(
                "Local vector store loaded: %d vectors from %s",
                len(_local_payloads), _VECTORS_PATH,
            )
        except Exception as exc:
            log.warning("Could not load local vector store: %s", exc)
            _local_vectors = None
            _local_payloads = []
            _local_ids = []


def _save_local_store() -> None:
    global _local_dirty
    if not _local_dirty:
        return
    _INDEX_DIR.mkdir(parents=True, exist_ok=True)
    if _local_vectors is not None and len(_local_vectors):
        np.save(str(_VECTORS_PATH), _local_vectors)
    with open(_PAYLOADS_PATH, "wb") as f:
        pickle.dump({"payloads": _local_payloads, "ids": _local_ids}, f)
    _local_dirty = False
    log.info("Local vector store saved: %d vectors", len(_local_payloads))


def _local_upsert(items: list[dict[str, Any]]) -> None:
    """Batch-upsert: build new vectors as a matrix, then vstack once."""
    global _local_vectors, _local_payloads, _local_ids, _local_dirty

    id_to_idx = {cid: i for i, cid in enumerate(_local_ids)}
    new_vecs: list[np.ndarray] = []
    new_ids: list[int] = []
    new_payloads: list[dict[str, Any]] = []

    for item in items:
        cid = item["id"]
        vec = np.array(item["vector"], dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm

        if cid in id_to_idx:
            # Update in-place
            _local_vectors[id_to_idx[cid]] = vec
            _local_payloads[id_to_idx[cid]] = item["payload"]
        else:
            new_vecs.append(vec)
            new_ids.append(cid)
            new_payloads.append(item["payload"])

    if new_vecs:
        new_matrix = np.stack(new_vecs)  # single allocation for the whole batch
        if _local_vectors is None or len(_local_vectors) == 0:
            _local_vectors = new_matrix
        else:
            _local_vectors = np.vstack([_local_vectors, new_matrix])
        _local_ids.extend(new_ids)
        _local_payloads.extend(new_payloads)

    _local_dirty = True
    _save_local_store()


def _local_search(
    vector: list[float],
    top_k: int,
    filters: dict[str, str] | None,
) -> list[dict[str, Any]]:
    if _local_vectors is None or len(_local_vectors) == 0:
        return []

    q = np.array(vector, dtype=np.float32)
    norm = np.linalg.norm(q)
    if norm > 0:
        q = q / norm

    # Cosine similarity = dot product of L2-normalised vectors
    scores = (_local_vectors @ q).tolist()

    # Apply metadata filters
    filtered: list[tuple[float, dict[str, Any]]] = []
    for score, payload in zip(scores, _local_payloads):
        if filters:
            if not all(payload.get(k) == v for k, v in filters.items()):
                continue
        filtered.append((score, payload))

    # Sort by similarity descending, take top_k
    filtered.sort(key=lambda x: x[0], reverse=True)
    return [
        {"score": float(score), **payload}
        for score, payload in filtered[:top_k]
    ]


# ---------------------------------------------------------------------------
# Load local store on import
# ---------------------------------------------------------------------------
_load_local_store()


# ---------------------------------------------------------------------------
# Public async API  (Actian-primary, local-fallback)
# ---------------------------------------------------------------------------

async def health() -> dict[str, Any]:
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_actian_health_sync), timeout=3.0
        )
        return {"actian": True, **result}
    except Exception:
        n = len(_local_payloads)
        return {
            "actian": False,
            "fallback": "local-numpy",
            "local_vectors": n,
        }


async def count() -> int:
    if await _actian_available():
        try:
            return await asyncio.to_thread(_actian_count_sync)
        except Exception:
            pass
    return len(_local_payloads)


async def ensure_collection() -> None:
    if await _actian_available():
        await asyncio.to_thread(_actian_ensure_collection_sync)


async def reset_collection() -> None:
    global _local_vectors, _local_payloads, _local_ids, _local_dirty
    if await _actian_available():
        await asyncio.to_thread(_actian_reset_collection_sync)
    # Always reset local store too
    _local_vectors = None
    _local_payloads = []
    _local_ids = []
    _local_dirty = True
    _save_local_store()
    log.info("Local vector store reset")


async def upsert_batch(items: list[dict[str, Any]]) -> None:
    """Upsert to Actian VectorAI DB if available, otherwise local numpy store."""
    if await _actian_available():
        try:
            await asyncio.to_thread(_actian_upsert_sync, items)
            return
        except Exception as exc:
            log.warning("Actian upsert failed, falling back to local: %s", exc)
    await asyncio.to_thread(_local_upsert, items)


async def search(
    vector: list[float],
    top_k: int = 10,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """K-NN search: Actian VectorAI DB primary, local numpy fallback."""
    if await _actian_available():
        try:
            raw = await asyncio.to_thread(_actian_search_sync, vector, top_k, filters)
            return [{"score": r.score, **r.payload} for r in raw]
        except Exception as exc:
            log.warning("Actian search failed, falling back to local: %s", exc)
    return await asyncio.to_thread(_local_search, vector, top_k, filters)
