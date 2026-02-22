"""
Permitting Stuckness Radar — FAST-41 analytics API.

Loads the FAST-41 Projects CSV at startup, computes stuckness scores, and
exposes read-only endpoints consumed by the radar.html frontend page.
Also exposes a POST /narrate endpoint that calls Ollama for LLM insight.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CSV_PATH = Path(__file__).parent.parent.parent / "other_data" / "FAST-41_Projects_Data_20260221.csv"
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

# OpenAI key — read from env or fall back to the databricks.md key
_OPENAI_KEY = os.getenv(
    "OPENAI_API_KEY",
    "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",
)
_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

router = APIRouter(prefix="/radar", tags=["radar"])

# ---------------------------------------------------------------------------
# Data loading and stuckness model
# ---------------------------------------------------------------------------

# Status → base stuckness score
_STATUS_SCORE: dict[str, float] = {
    "Paused": 1.0,
    "Class of Action Changed": 0.65,
    "In Progress": 0.40,
    "Planned": 0.20,
    "Cancelled": 0.10,
    "Complete": 0.0,
}

_STUCK_STATUSES = {"Paused", "In Progress", "Planned", "Class of Action Changed"}
_ACTIVE_STATUSES = {"In Progress", "Paused", "Planned", "Class of Action Changed"}


def _load_projects() -> list[dict[str, Any]]:
    """Read CSV, deduplicate by Project ID, return cleaned project dicts."""
    projects: dict[str, dict[str, Any]] = {}
    with open(_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = row.get("Project ID", "").strip()
            if not pid or pid in projects:
                continue
            lat_raw = row.get("Project Location Latitude", "").strip()
            lon_raw = row.get("Project Location Longitude", "").strip()
            try:
                lat = float(lat_raw) if lat_raw else None
                lon = float(lon_raw) if lon_raw else None
            except ValueError:
                lat = lon = None
            projects[pid] = {
                "id": pid,
                "name": row.get("Project", "").strip(),
                "agency": row.get("Project Lead Agency", "").strip(),
                "bureau": row.get("Project Lead Agency Bureau", "").strip(),
                "category": row.get("Project Category", "").strip(),
                "status": row.get("Project Status", "").strip(),
                "sector": row.get("Project Sector", "").strip(),
                "sector_type": row.get("Project Sector Type", "").strip(),
                "state": row.get("Project Location State", "").strip(),
                "county": row.get("Project Location County", "").strip(),
                "city": row.get("Project Location City", "").strip(),
                "lat": lat,
                "lon": lon,
            }
    return list(projects.values())


def _compute_stuckness(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Enrich each project with a stuckness_score [0,1] and stuckness_label.

    Algorithm:
    1. Start from base score by status.
    2. For 'In Progress' projects, adjust by comparing to sector stuckness baseline:
       if the sector has a high pause rate, that raises peer expectations and lowers
       the individual score slightly; if below average it bumps the score.
    3. Clamp to [0, 1].
    """
    # Compute sector-level pause rate for context
    sector_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "stuck": 0})
    for p in projects:
        s = p["sector"] or "Unknown"
        sector_counts[s]["total"] += 1
        if p["status"] in _STUCK_STATUSES:
            sector_counts[s]["stuck"] += 1

    sector_pause_rate: dict[str, float] = {}
    global_stuck = sum(sc["stuck"] for sc in sector_counts.values())
    global_total = sum(sc["total"] for sc in sector_counts.values())
    global_rate = global_stuck / global_total if global_total else 0.0
    for sector, sc in sector_counts.items():
        sector_pause_rate[sector] = sc["stuck"] / sc["total"] if sc["total"] else global_rate

    enriched = []
    for p in projects:
        base = _STATUS_SCORE.get(p["status"], 0.0)
        # For In Progress: bump if sector has below-average stuckness (this project stands out)
        if p["status"] == "In Progress":
            sector_rate = sector_pause_rate.get(p["sector"] or "Unknown", global_rate)
            delta = global_rate - sector_rate  # positive → sector is healthier than avg
            base = base + (delta * 0.2)  # small nudge
        score = max(0.0, min(1.0, base))

        if score >= 0.85:
            label = "High Risk"
        elif score >= 0.55:
            label = "At Risk"
        elif score >= 0.25:
            label = "Monitor"
        else:
            label = "On Track"

        enriched.append({**p, "stuckness_score": round(score, 3), "stuckness_label": label})

    return enriched


# ---------------------------------------------------------------------------
# Module-level cache (load once, serve many)
# ---------------------------------------------------------------------------

_data_lock = threading.Lock()
_cached_projects: list[dict[str, Any]] | None = None
_cache_ts: float = 0.0
_CACHE_TTL = 3600  # 1 hour


def _get_projects() -> list[dict[str, Any]]:
    global _cached_projects, _cache_ts
    with _data_lock:
        if _cached_projects is None or (time.time() - _cache_ts) > _CACHE_TTL:
            raw = _load_projects()
            _cached_projects = _compute_stuckness(raw)
            _cache_ts = time.time()
        return _cached_projects


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(num: int, denom: int) -> float:
    return round(100 * num / denom, 1) if denom else 0.0


# ---------------------------------------------------------------------------
# GET /api/radar/overview
# ---------------------------------------------------------------------------

@router.get("/overview")
def radar_overview() -> dict[str, Any]:
    """Top-line stuckness statistics across all FAST-41 projects."""
    projects = _get_projects()
    total = len(projects)
    by_status: dict[str, int] = defaultdict(int)
    for p in projects:
        by_status[p["status"]] += 1

    stuck = by_status.get("Paused", 0)
    in_progress = by_status.get("In Progress", 0)
    active = sum(by_status[s] for s in _ACTIVE_STATUSES)

    high_risk = sum(1 for p in projects if p["stuckness_label"] == "High Risk")
    at_risk = sum(1 for p in projects if p["stuckness_label"] == "At Risk")
    monitor = sum(1 for p in projects if p["stuckness_label"] == "Monitor")

    avg_score = round(sum(p["stuckness_score"] for p in projects) / total, 3) if total else 0.0

    return {
        "total_projects": total,
        "by_status": dict(by_status),
        "active_projects": active,
        "stuck_projects": stuck,
        "stuck_pct": _pct(stuck, total),
        "in_progress": in_progress,
        "avg_stuckness_score": avg_score,
        "risk_breakdown": {
            "high_risk": high_risk,
            "at_risk": at_risk,
            "monitor": monitor,
            "on_track": total - high_risk - at_risk - monitor,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/radar/by-state
# ---------------------------------------------------------------------------

@router.get("/by-state")
def radar_by_state() -> list[dict[str, Any]]:
    """State-level stuckness aggregation for choropleth map."""
    projects = _get_projects()
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "paused": 0, "in_progress": 0, "complete": 0,
                 "cancelled": 0, "score_sum": 0.0, "high_risk": 0}
    )
    for p in projects:
        state = p["state"]
        if not state or len(state) > 3:
            continue
        agg[state]["total"] += 1
        agg[state]["score_sum"] += p["stuckness_score"]
        status = p["status"]
        if status == "Paused":
            agg[state]["paused"] += 1
        elif status == "In Progress":
            agg[state]["in_progress"] += 1
        elif status == "Complete":
            agg[state]["complete"] += 1
        elif status == "Cancelled":
            agg[state]["cancelled"] += 1
        if p["stuckness_label"] == "High Risk":
            agg[state]["high_risk"] += 1

    result = []
    for state, d in agg.items():
        t = d["total"]
        result.append({
            "state": state,
            "total": t,
            "paused": d["paused"],
            "in_progress": d["in_progress"],
            "complete": d["complete"],
            "cancelled": d["cancelled"],
            "high_risk": d["high_risk"],
            "stuck_pct": _pct(d["paused"], t),
            "avg_stuckness": round(d["score_sum"] / t, 3) if t else 0.0,
        })

    return sorted(result, key=lambda x: -x["avg_stuckness"])


# ---------------------------------------------------------------------------
# GET /api/radar/by-sector
# ---------------------------------------------------------------------------

@router.get("/by-sector")
def radar_by_sector() -> list[dict[str, Any]]:
    """Sector-level stuckness breakdown."""
    projects = _get_projects()
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "paused": 0, "in_progress": 0,
                 "complete": 0, "other": 0, "score_sum": 0.0}
    )
    for p in projects:
        sector = p["sector"] or "Unknown"
        agg[sector]["total"] += 1
        agg[sector]["score_sum"] += p["stuckness_score"]
        status = p["status"]
        if status == "Paused":
            agg[sector]["paused"] += 1
        elif status == "In Progress":
            agg[sector]["in_progress"] += 1
        elif status == "Complete":
            agg[sector]["complete"] += 1
        else:
            agg[sector]["other"] += 1

    result = []
    for sector, d in agg.items():
        t = d["total"]
        result.append({
            "sector": sector,
            "total": t,
            "paused": d["paused"],
            "in_progress": d["in_progress"],
            "complete": d["complete"],
            "other": d["other"],
            "stuck_pct": _pct(d["paused"], t),
            "avg_stuckness": round(d["score_sum"] / t, 3) if t else 0.0,
        })

    return sorted(result, key=lambda x: -x["avg_stuckness"])


# ---------------------------------------------------------------------------
# GET /api/radar/by-agency
# ---------------------------------------------------------------------------

@router.get("/by-agency")
def radar_by_agency() -> list[dict[str, Any]]:
    """Bureau-level stuckness ranking."""
    projects = _get_projects()
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "paused": 0, "in_progress": 0, "score_sum": 0.0, "agency": ""}
    )
    for p in projects:
        bureau = p["bureau"] or p["agency"] or "Unknown"
        agg[bureau]["total"] += 1
        agg[bureau]["agency"] = p["agency"]
        agg[bureau]["score_sum"] += p["stuckness_score"]
        if p["status"] == "Paused":
            agg[bureau]["paused"] += 1
        elif p["status"] == "In Progress":
            agg[bureau]["in_progress"] += 1

    result = []
    for bureau, d in agg.items():
        t = d["total"]
        if t < 3:  # skip tiny bureaus
            continue
        result.append({
            "bureau": bureau,
            "agency": d["agency"],
            "total": t,
            "paused": d["paused"],
            "in_progress": d["in_progress"],
            "stuck_pct": _pct(d["paused"], t),
            "avg_stuckness": round(d["score_sum"] / t, 3) if t else 0.0,
        })

    return sorted(result, key=lambda x: -x["avg_stuckness"])[:20]


# ---------------------------------------------------------------------------
# GET /api/radar/projects
# ---------------------------------------------------------------------------

@router.get("/projects")
def radar_projects(
    status: str | None = Query(None),
    state: str | None = Query(None),
    sector: str | None = Query(None),
    min_score: float = Query(0.0),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
) -> dict[str, Any]:
    """Filterable list of projects with stuckness scores."""
    projects = _get_projects()
    filtered = projects
    if status:
        filtered = [p for p in filtered if p["status"].lower() == status.lower()]
    if state:
        filtered = [p for p in filtered if p["state"].upper() == state.upper()]
    if sector:
        filtered = [p for p in filtered if sector.lower() in p["sector"].lower()]
    filtered = [p for p in filtered if p["stuckness_score"] >= min_score]
    filtered = sorted(filtered, key=lambda x: -x["stuckness_score"])
    return {
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "projects": filtered[offset : offset + limit],
    }


# ---------------------------------------------------------------------------
# GET /api/radar/project/{id}
# ---------------------------------------------------------------------------

@router.get("/project/{project_id}")
def radar_project_detail(project_id: str) -> dict[str, Any]:
    """Single project stuckness detail with peer comparison."""
    projects = _get_projects()
    project = next((p for p in projects if p["id"] == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Peer comparison: same sector
    peers = [p for p in projects if p["sector"] == project["sector"] and p["id"] != project_id]
    peer_scores = [p["stuckness_score"] for p in peers]
    peer_avg = round(sum(peer_scores) / len(peer_scores), 3) if peer_scores else 0.0
    peer_stuck_pct = _pct(
        sum(1 for p in peers if p["status"] in _STUCK_STATUSES), len(peers)
    )

    return {
        **project,
        "peer_comparison": {
            "sector": project["sector"],
            "peer_count": len(peers),
            "peer_avg_stuckness": peer_avg,
            "peer_stuck_pct": peer_stuck_pct,
            "vs_peers": round(project["stuckness_score"] - peer_avg, 3),
        },
    }


# ---------------------------------------------------------------------------
# POST /api/radar/narrate  — LLM copilot
# ---------------------------------------------------------------------------

class NarrateRequest(BaseModel):
    context: str  # JSON summary of the current radar view
    question: str | None = None  # optional specific question


@router.post("/narrate")
def radar_narrate(req: NarrateRequest) -> dict[str, str]:
    """
    Generate an OPEF-style narrative about stuckness data.
    Tries OpenAI first; falls back to Ollama if no key available.
    Returns {narrative: str}.
    """
    system_prompt = (
        "You are an OPEF (Office of Project Excellence and Finance) permitting analyst copilot. "
        "Your job is to interpret FAST-41 project stuckness data and narrate it clearly for a "
        "policy audience. Be specific, cite numbers, identify patterns, and suggest what might "
        "be causing bottlenecks. Be concise but insightful — no more than 4 paragraphs. "
        "Do not use markdown headers; write flowing prose."
    )

    user_message = f"Here is the current permitting stuckness data:\n\n{req.context}"
    if req.question:
        user_message += f"\n\nSpecific question: {req.question}"

    # Try OpenAI first
    if _OPENAI_KEY:
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {_OPENAI_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": _OPENAI_MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message},
                        ],
                        "temperature": 0.4,
                        "max_tokens": 700,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                narrative = data["choices"][0]["message"]["content"].strip()
                return {"narrative": narrative}
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="LLM timed out — try again")
        except httpx.HTTPStatusError as e:
            # Fall through to Ollama on auth failure
            if e.response.status_code not in (401, 403):
                raise HTTPException(status_code=502, detail=f"OpenAI error: {e.response.text[:300]}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Fallback: Ollama
    payload = {
        "model": _OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 600},
    }
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(f"{_OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            narrative = data["message"]["content"].strip()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="LLM timed out — try again")
    except (httpx.HTTPStatusError, httpx.ConnectError) as e:
        raise HTTPException(status_code=503, detail="No LLM service available. Please start Ollama or set OPENAI_API_KEY.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"narrative": narrative}
