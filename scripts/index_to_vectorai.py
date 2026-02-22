#!/usr/bin/env python3
"""
Index all NEPA documents from SQLite into Actian VectorAI DB.

Usage:
    export OPENAI_API_KEY=sk-...
    python scripts/index_to_vectorai.py [--reset] [--host localhost:50051] [--db nepa.db]

Requirements:
    - Actian VectorAI DB running:  docker compose up  (in actian-vectorAI-db-beta/)
    - pip install ./actiancortex-0.1.0b1-py3-none-any.whl
    - OPENAI_API_KEY environment variable
    - nepa.db present in repo root (run ingest first)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("indexer")

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
COLLECTION_NAME = "nepa_chunks"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 80
EMBED_BATCH = 100


# ---------------------------------------------------------------------------
# Chunking + ID
# ---------------------------------------------------------------------------

def chunk_text(text: str) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def stable_id(doc_id: str, chunk_idx: int) -> int:
    return abs(hash(f"{doc_id}::{chunk_idx}")) % (2**31)


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------

async def embed_batch(texts: list[str], api_key: str) -> list[list[float]]:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)
    resp = await client.embeddings.create(
        model=EMBED_MODEL,
        input=[t[:8000] for t in texts],
    )
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def load_documents(db_path: str) -> list[dict]:
    if not Path(db_path).exists():
        log.error("Database not found: %s", db_path)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT d.id, d.project_id, d.doc_type, d.text_content,
                  p.title, p.process_type, p.agency, p.state
           FROM documents d
           JOIN projects p ON p.id = d.project_id
           WHERE d.text_content IS NOT NULL AND LENGTH(d.text_content) > 50"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(host: str, db_path: str, reset: bool, api_key: str) -> None:
    from cortex import CortexClient, DistanceMetric  # type: ignore[import]

    log.info("Connecting to Actian VectorAI DB at %s …", host)
    with CortexClient(host) as client:
        version, uptime = client.health_check()
        log.info("Connected  version=%s  uptime=%s", version, uptime)

        if reset and client.has_collection(COLLECTION_NAME):
            client.delete_collection(COLLECTION_NAME)
            log.info("Dropped existing collection '%s'", COLLECTION_NAME)

        if not client.has_collection(COLLECTION_NAME):
            client.create_collection(
                name=COLLECTION_NAME,
                dimension=EMBED_DIM,
                distance_metric=DistanceMetric.COSINE,
            )
            log.info("Created collection '%s'  dim=%d  metric=COSINE", COLLECTION_NAME, EMBED_DIM)
        else:
            existing = client.count(COLLECTION_NAME)
            log.info("Collection exists with %d vectors (use --reset to rebuild)", existing)

    log.info("Loading documents from %s …", db_path)
    docs = load_documents(db_path)
    log.info("Found %d documents with text content", len(docs))

    all_chunks: list[tuple[int, dict, str]] = []
    for doc in docs:
        text = doc["text_content"] or ""
        for idx, chunk in enumerate(chunk_text(text)):
            cid = stable_id(doc["id"], idx)
            payload = {
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

    log.info("Total chunks to embed and index: %d", len(all_chunks))

    indexed = 0
    for i in range(0, len(all_chunks), EMBED_BATCH):
        batch = all_chunks[i : i + EMBED_BATCH]
        texts = [item[2] for item in batch]

        log.info(
            "Embedding batch %d–%d / %d …",
            i + 1, min(i + EMBED_BATCH, len(all_chunks)), len(all_chunks),
        )
        vectors = await embed_batch(texts, api_key)

        ids = [item[0] for item in batch]
        payloads = [item[1] for item in batch]

        with CortexClient(host) as client:
            client.batch_upsert(COLLECTION_NAME, ids=ids, vectors=vectors, payloads=payloads)

        indexed += len(batch)
        pct = indexed / len(all_chunks) * 100
        log.info("Progress: %d / %d  (%.1f%%)", indexed, len(all_chunks), pct)

    log.info(
        "Done! %d chunks from %d documents indexed into Actian VectorAI DB collection '%s'",
        indexed, len(docs), COLLECTION_NAME,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index NEPA documents into Actian VectorAI DB")
    parser.add_argument("--host", default="localhost:50051", help="Actian VectorAI DB host:port")
    parser.add_argument("--db", default="nepa.db", help="Path to SQLite database")
    parser.add_argument("--reset", action="store_true", help="Delete and recreate collection first")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        log.error("OPENAI_API_KEY environment variable is not set")
        sys.exit(1)

    asyncio.run(main(args.host, args.db, args.reset, api_key))
