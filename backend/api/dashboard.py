"""
Dashboard analytics endpoints for the NEPA Observatory.

All queries operate on the same SQLite DB used by routes.py.
No writes are performed here — read-only analytics only.

Results are cached in-process for CACHE_TTL seconds so the heavy
aggregate queries on the external-drive SQLite don't block every request.
"""

from __future__ import annotations

import json
import sqlite3
import time
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from backend.config import DB_PATH

# ---------------------------------------------------------------------------
# Two-layer cache: in-process dict (fast) + disk JSON (survives restarts)
# ---------------------------------------------------------------------------

CACHE_TTL = 86400        # in-process TTL in seconds (24 h; data rarely changes)
DISK_CACHE_MAX_AGE = 86400  # seconds before disk cache is considered stale

# Disk cache lives next to the database
_DISK_CACHE_PATH: Path = DB_PATH.parent / "dashboard_cache.json"

_cache: dict[str, Any] = {}
_cache_ts: dict[str, float] = {}
_cache_lock = threading.Lock()


def _load_disk_cache() -> None:
    """Called once at import time — populates in-memory cache from disk if fresh."""
    if not _DISK_CACHE_PATH.exists():
        return
    try:
        raw = json.loads(_DISK_CACHE_PATH.read_text())
        saved_at: float = raw.get("_saved_at", 0)
        if (time.time() - saved_at) > DISK_CACHE_MAX_AGE:
            return  # stale — let background thread rebuild
        with _cache_lock:
            for key in ("stats", "by_state", "by_agency"):
                if key in raw:
                    _cache[key] = raw[key]
                    _cache_ts[key] = saved_at
    except Exception:
        pass  # corrupt file — silently ignore, will rebuild


def _save_disk_cache() -> None:
    """Persist current in-memory cache to disk (called after every refresh)."""
    try:
        with _cache_lock:
            payload = {k: _cache[k] for k in ("stats", "by_state", "by_agency") if k in _cache}
        payload["_saved_at"] = time.time()
        _DISK_CACHE_PATH.write_text(json.dumps(payload))
    except Exception:
        pass


def _get_cached(key: str) -> Any:
    with _cache_lock:
        if key in _cache and (time.time() - _cache_ts.get(key, 0)) < CACHE_TTL:
            return _cache[key]
    return None


def _set_cached(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = value
        _cache_ts[key] = time.time()


# Populate from disk immediately on module load
_load_disk_cache()

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# ---------------------------------------------------------------------------
# State normalization — map any form to 2-letter USPS abbreviation
# ---------------------------------------------------------------------------

_NAME_TO_ABBREV: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
    "Puerto Rico": "PR",
}

_VALID_ABBREVS: frozenset[str] = frozenset(_NAME_TO_ABBREV.values())
_LOWER_MAP: dict[str, str] = {k.lower(): v for k, v in _NAME_TO_ABBREV.items()}


def _normalize_state(raw: str | None) -> str | None:
    """Return USPS 2-letter abbreviation or None if unrecognized."""
    if not raw:
        return None
    # Strip trailing punctuation common in BLM data ("Montana.", "New Mexico.")
    s = raw.strip().rstrip(".").strip()
    if not s or s == "UNK":
        return None
    upper = s.upper()
    if upper in _VALID_ABBREVS:
        return upper
    if s in _NAME_TO_ABBREV:
        return _NAME_TO_ABBREV[s]
    return _LOWER_MAP.get(s.lower())


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# GET /api/dashboard/stats
# ---------------------------------------------------------------------------

_CACHE_HEADERS = {"Cache-Control": "public, max-age=300, stale-while-revalidate=3600"}


def _json(data: Any) -> JSONResponse:
    return JSONResponse(content=data, headers=_CACHE_HEADERS)


@router.get("/stats")
def dashboard_stats() -> JSONResponse:
    """Top-line numbers: totals + process-type breakdown."""
    cached = _get_cached("stats")
    if cached is not None:
        return _json(cached)
    try:
        conn = _conn()
        try:
            total_projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            by_type_rows = conn.execute(
                "SELECT process_type, COUNT(*) AS count FROM projects"
                " GROUP BY process_type ORDER BY count DESC"
            ).fetchall()
            total_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            total_pages = conn.execute(
                "SELECT COALESCE(SUM(page_count), 0) FROM documents"
            ).fetchone()[0]
            total_agencies = conn.execute(
                "SELECT COUNT(DISTINCT agency) FROM projects"
                " WHERE agency IS NOT NULL AND agency != ''"
            ).fetchone()[0]
            result = {
                "total_projects": total_projects,
                "total_documents": total_docs,
                "total_pages": int(total_pages),
                "total_agencies": total_agencies,
                "by_process_type": [dict(r) for r in by_type_rows],
            }
        finally:
            conn.close()
        _set_cached("stats", result)
        _save_disk_cache()
        return _json(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/dashboard/by-state
# ---------------------------------------------------------------------------

@router.get("/by-state")
def dashboard_by_state() -> JSONResponse:
    """Project counts aggregated by normalized 2-letter state code."""
    cached = _get_cached("by_state")
    if cached is not None:
        return _json(cached)
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT state, process_type, COUNT(*) AS c"
                " FROM projects WHERE state IS NOT NULL"
                " GROUP BY state, process_type"
            ).fetchall()
        finally:
            conn.close()

        agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {"total": 0, "CE": 0, "EA": 0, "EIS": 0}
        )
        for r in rows:
            norm = _normalize_state(r["state"])
            if not norm:
                continue
            agg[norm]["total"] += r["c"]
            pt = r["process_type"]
            if pt in ("CE", "EA", "EIS"):
                agg[norm][pt] += r["c"]

        result = [
            {"state": k, **v}
            for k, v in sorted(agg.items(), key=lambda x: -x[1]["total"])
        ]
        _set_cached("by_state", result)
        _save_disk_cache()
        return _json(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/dashboard/by-agency
# ---------------------------------------------------------------------------

@router.get("/by-agency")
def dashboard_by_agency() -> JSONResponse:
    """Top 12 agencies by project count, each with CE/EA/EIS breakdown."""
    cached = _get_cached("by_agency")
    if cached is not None:
        return _json(cached)
    try:
        conn = _conn()
        try:
            # Get top 12 agency names first
            top_agencies = [
                r["agency"]
                for r in conn.execute(
                    "SELECT agency, COUNT(*) AS c FROM projects"
                    " WHERE agency IS NOT NULL AND agency != ''"
                    " GROUP BY agency ORDER BY c DESC LIMIT 12"
                ).fetchall()
            ]
            # Fetch breakdown per agency + process_type
            rows = conn.execute(
                "SELECT agency, process_type, COUNT(*) AS c FROM projects"
                " WHERE agency IN ({})".format(",".join("?" * len(top_agencies)))
                + " GROUP BY agency, process_type",
                top_agencies,
            ).fetchall()
        finally:
            conn.close()

        agg: dict[str, dict[str, Any]] = {
            a: {"agency": a, "total": 0, "CE": 0, "EA": 0, "EIS": 0}
            for a in top_agencies
        }
        for r in rows:
            a = r["agency"]
            if a in agg:
                agg[a]["total"] += r["c"]
                pt = r["process_type"]
                if pt in ("CE", "EA", "EIS"):
                    agg[a][pt] += r["c"]

        result = sorted(agg.values(), key=lambda x: -x["total"])
        _set_cached("by_agency", result)
        _save_disk_cache()
        return _json(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
