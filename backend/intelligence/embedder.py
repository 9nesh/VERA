"""
OpenAI embedding functions for VERA semantic search.

Uses text-embedding-3-small: 1536d, fast, cost-efficient.
The openai package is already present in the backend venv.
"""

from __future__ import annotations

from typing import Sequence

from backend.config import EMBED_MODEL, OPENAI_API_KEY

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


async def embed_text(text: str) -> list[float]:
    """Embed a single string. Caps input at 8000 chars for safety."""
    client = _get_client()
    resp = await client.embeddings.create(
        model=EMBED_MODEL,
        input=text[:8000],
    )
    return resp.data[0].embedding


async def embed_batch(texts: Sequence[str]) -> list[list[float]]:
    """Embed up to 100 strings in a single API call."""
    client = _get_client()
    resp = await client.embeddings.create(
        model=EMBED_MODEL,
        input=[t[:8000] for t in texts],
    )
    # API returns embeddings in the same order as input
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
