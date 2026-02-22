"""
Ollama HTTP client for VERA.

Configurable via backend.config (OLLAMA_URL, OLLAMA_MODEL).
Retries once on connection refused to handle Ollama cold-start.
All inference is on-device; nothing is sent to any external service.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from backend.config import OLLAMA_MODEL, OLLAMA_URL

_client: httpx.AsyncClient | None = None
_TIMEOUT = 120.0  # seconds; generous for larger models


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=OLLAMA_URL, timeout=_TIMEOUT)
    return _client


async def is_available() -> bool:
    """Return True if Ollama is running and responsive."""
    try:
        r = await _get_client().get("/api/tags")
        return r.status_code == 200
    except Exception:
        return False


async def list_models() -> list[str]:
    """Return names of locally available models."""
    try:
        r = await _get_client().get("/api/tags")
        data = r.json()
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


async def generate(
    prompt: str,
    model: str | None = None,
    system: str = "",
    temperature: float = 0.1,
) -> str:
    """
    Non-streaming generate. Returns the full response text.
    Retries once on connection refused (Ollama cold-start).
    Low temperature keeps the model grounded on retrieved text.
    """
    model = model or OLLAMA_MODEL
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "num_predict": 1024,
        },
    }
    client = _get_client()
    try:
        r = await client.post("/api/generate", json=payload)
        r.raise_for_status()
        return r.json().get("response", "")
    except httpx.ConnectError:
        # Ollama may be warming up — retry once after a short wait
        import asyncio
        await asyncio.sleep(2)
        r = await _get_client().post("/api/generate", json=payload)
        r.raise_for_status()
        return r.json().get("response", "")


async def generate_stream(
    prompt: str,
    model: str | None = None,
    system: str = "",
    temperature: float = 0.1,
) -> AsyncIterator[str]:
    """
    Streaming generate — yields text tokens as they arrive.
    """
    model = model or OLLAMA_MODEL
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": True,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "num_predict": 1024,
        },
    }
    async with _get_client().stream("POST", "/api/generate", json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
                token = chunk.get("response", "")
                if token:
                    yield token
                if chunk.get("done"):
                    break
            except json.JSONDecodeError:
                continue
