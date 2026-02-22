"""
Semantic search API routes — powered by Actian VectorAI DB.

Endpoints:
  GET  /api/semantic/status          — DB health + indexed chunk count
  POST /api/semantic/search          — Semantic similarity search
  POST /api/semantic/ask             — RAG Q&A (retrieval + Ollama synthesis)
  POST /api/semantic/index           — Trigger background re-indexing from SQLite
  GET  /api/semantic/indexing-status — Poll whether indexing is in progress
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from backend.config import COLLECTION_NAME, DB_PATH
from backend.intelligence import embedder, vector_store
from backend.llm import client as llm

log = logging.getLogger("vera.semantic")

router = APIRouter(prefix="/semantic", tags=["semantic"])

_CHUNK_SIZE = 600
_CHUNK_OVERLAP = 80
_EMBED_BATCH = 100

_SYSTEM = (
    "You are VERA, an expert on NEPA environmental review and federal permitting. "
    "Answer the user's question using ONLY the provided document excerpts. "
    "Be precise, cite project names when relevant, and be concise."
)

_OLLAMA_UNAVAILABLE = (
    "The AI model (Ollama) is not running. Start it with `ollama serve` and try again."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _chunk_text(text: str) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks


def _stable_id(doc_id: str, chunk_idx: int) -> int:
    """Deterministic integer ID: abs(hash(doc_id::idx)) mod 2^31."""
    return abs(hash(f"{doc_id}::{chunk_idx}")) % (2**31)


_MAX_DOCS = 100   # cap to keep indexing fast and API costs low

def _load_documents() -> list[dict[str, Any]]:
    """Load up to _MAX_DOCS documents, prioritising EIS > EA > CE."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT d.id, d.project_id, d.doc_type,
                      SUBSTR(d.text_content, 1, 120000) AS text_content,
                      p.title, p.process_type, p.agency, p.state
               FROM documents d
               JOIN projects p ON p.id = d.project_id
               WHERE d.text_content IS NOT NULL AND LENGTH(d.text_content) > 200
               ORDER BY
                 CASE p.process_type WHEN 'EIS' THEN 0 WHEN 'EA' THEN 1 ELSE 2 END,
                 LENGTH(d.text_content) DESC
               LIMIT ?""",
            (_MAX_DOCS,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _build_chunks(docs: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any], str]]:
    all_chunks: list[tuple[int, dict[str, Any], str]] = []
    for doc in docs:
        text = doc["text_content"] or ""
        for idx, chunk in enumerate(_chunk_text(text)):
            cid = _stable_id(doc["id"], idx)
            payload: dict[str, Any] = {
                "text": chunk,
                "project_id": doc["project_id"],
                "project_title": doc["title"] or doc["project_id"],
                "doc_id": doc["id"],
                "doc_type": doc["doc_type"] or "UNKNOWN",
                "process_type": doc["process_type"] or "",
                "agency": doc["agency"] or "",
                "state": doc["state"] or "",
                "chunk_idx": idx,
            }
            all_chunks.append((cid, payload, chunk))
    return all_chunks


async def _run_indexing(reset: bool) -> dict[str, Any]:
    # Load docs in a thread (sync SQLite I/O on 4GB db)
    docs = await asyncio.to_thread(_load_documents)
    if not docs:
        return {"status": "no_documents", "indexed": 0}

    if reset:
        await vector_store.reset_collection()
    else:
        await vector_store.ensure_collection()

    # Build chunks in a thread (CPU work)
    all_chunks = await asyncio.to_thread(_build_chunks, docs)
    log.info("Built %d chunks from %d documents — starting embedding", len(all_chunks), len(docs))

    total = 0
    for i in range(0, len(all_chunks), _EMBED_BATCH):
        batch = all_chunks[i : i + _EMBED_BATCH]
        texts = [item[2] for item in batch]
        vectors = await embedder.embed_batch(texts)
        items = [
            {"id": cid, "vector": vec, "payload": payload}
            for (cid, payload, _), vec in zip(batch, vectors)
        ]
        await vector_store.upsert_batch(items)
        total += len(items)
        log.info("Indexed %d / %d chunks", total, len(all_chunks))

    return {"status": "complete", "indexed": total, "documents": len(docs)}


# ---------------------------------------------------------------------------
# Indexing state
# ---------------------------------------------------------------------------

_indexing: dict[str, Any] = {"in_progress": False, "last_result": None}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status")
async def semantic_status() -> dict[str, Any]:
    """Actian VectorAI DB connection status and indexed chunk count."""
    try:
        h = await vector_store.health()
        n = await vector_store.count()
        actian_online = h.get("actian", False)
        return {
            "online": True,  # always true — local fallback keeps us alive
            "actian_online": actian_online,
            "backend": "actian" if actian_online else "local-numpy",
            "db_version": h.get("version", "—"),
            "db_uptime": h.get("uptime", "—"),
            "indexed_chunks": n,
            "collection": COLLECTION_NAME,
        }
    except Exception as exc:
        return {"online": False, "error": str(exc), "indexed_chunks": 0}


class SearchRequest(BaseModel):
    query: str
    top_k: int = 10
    process_type: str | None = None
    agency: str | None = None
    state: str | None = None


@router.post("/search")
async def semantic_search(body: SearchRequest) -> list[dict[str, Any]]:
    """Embed query and run K-NN similarity search over indexed NEPA document chunks."""
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="Query must not be empty")
    try:
        vector = await embedder.embed_text(body.query)
        filters: dict[str, str] = {}
        if body.process_type:
            filters["process_type"] = body.process_type
        if body.agency:
            filters["agency"] = body.agency
        if body.state:
            filters["state"] = body.state
        return await vector_store.search(
            vector,
            top_k=body.top_k,
            filters=filters or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class AskRequest(BaseModel):
    question: str
    top_k: int = 8
    process_type: str | None = None


@router.post("/ask")
async def semantic_ask(body: AskRequest) -> dict[str, Any]:
    """
    RAG Q&A: embed question → retrieve semantically relevant chunks from
    Actian VectorAI DB → synthesize answer with Ollama.
    """
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Question must not be empty")
    try:
        vector = await embedder.embed_text(body.question)
        filters: dict[str, str] = {}
        if body.process_type:
            filters["process_type"] = body.process_type
        results = await vector_store.search(
            vector,
            top_k=body.top_k,
            filters=filters or None,
        )

        if not results:
            return {
                "answer": (
                    "No relevant documents found. "
                    "Make sure documents have been indexed first via the Index tab."
                ),
                "sources": [],
            }

        context_parts: list[str] = []
        for i, r in enumerate(results):
            context_parts.append(
                f"[{i + 1}] {r.get('project_title', 'Unknown')} "
                f"({r.get('process_type', '')} | {r.get('agency', '')} | {r.get('state', '')})\n"
                f"Similarity: {r.get('score', 0):.3f}\n"
                f"{r.get('text', '')}"
            )
        context = "\n\n---\n\n".join(context_parts)

        prompt = (
            f"Document excerpts (ranked by semantic relevance to the question):\n\n"
            f"{context}\n\n"
            f"Question: {body.question}\n\nAnswer:"
        )

        try:
            answer = await llm.generate(prompt, system=_SYSTEM)
        except (httpx.ConnectError, httpx.TimeoutException):
            return {"answer": _OLLAMA_UNAVAILABLE, "sources": results}

        sources = [
            {
                "score": r.get("score"),
                "project_id": r.get("project_id"),
                "project_title": r.get("project_title"),
                "process_type": r.get("process_type"),
                "agency": r.get("agency"),
                "state": r.get("state"),
                "excerpt": (r.get("text") or "")[:300],
            }
            for r in results
        ]
        return {"answer": answer.strip() or "No answer generated.", "sources": sources}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class IndexRequest(BaseModel):
    reset: bool = False


@router.post("/index")
async def trigger_index(body: IndexRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Trigger background indexing of all NEPA documents into Actian VectorAI DB."""
    if _indexing["in_progress"]:
        raise HTTPException(status_code=409, detail="Indexing already in progress")

    async def _run() -> None:
        _indexing["in_progress"] = True
        try:
            result = await _run_indexing(reset=body.reset)
            _indexing["last_result"] = result
            log.info("Indexing complete: %s", result)
        except Exception as exc:
            _indexing["last_result"] = {"status": "error", "error": str(exc)}
            log.error("Indexing failed: %s", exc)
        finally:
            _indexing["in_progress"] = False

    background_tasks.add_task(_run)
    return {"status": "indexing_started", "reset": body.reset}


@router.get("/indexing-status")
async def indexing_status() -> dict[str, Any]:
    """Poll indexing progress."""
    return {
        "in_progress": _indexing["in_progress"],
        "last_result": _indexing["last_result"],
    }
