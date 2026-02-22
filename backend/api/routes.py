"""
FastAPI routes for VERA — lender-facing NEPA risk tool.

All DB access uses config.DB_PATH. Every endpoint wraps in try/except
and returns a clean 500 on failure. No hardcoded paths.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from backend.config import DB_PATH
from backend.intelligence.signals import scan_project


router = APIRouter(prefix="/projects", tags=["projects"])


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# GET /api/projects/search
# ---------------------------------------------------------------------------

@router.get("/search")
def projects_search(
    q: str = Query(..., min_length=1),
    process_type: str | None = Query(None),
) -> list[dict[str, Any]]:
    """Full-text search on project title, agency, state. Returns list of projects."""
    try:
        conn = _get_conn()
        try:
            pattern = f"%{q}%"
            if process_type:
                rows = conn.execute(
                    """SELECT id, title, process_type, agency, state, register_date, status
                       FROM projects
                       WHERE (title LIKE ? OR agency LIKE ? OR state LIKE ?) AND process_type = ?
                       ORDER BY register_date DESC NULLS LAST""",
                    (pattern, pattern, pattern, process_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, title, process_type, agency, state, register_date, status
                       FROM projects
                       WHERE title LIKE ? OR agency LIKE ? OR state LIKE ?
                       ORDER BY register_date DESC NULLS LAST""",
                    (pattern, pattern, pattern),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/projects/{id}
# ---------------------------------------------------------------------------

@router.get("/{project_id}")
def project_detail(project_id: str) -> dict[str, Any]:
    """Single project with documents and milestones."""
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                """SELECT id, title, process_type, agency, state, county, lead_office,
                          register_date, status, project_url,
                          solana_tx_signature, solana_attested_at, solana_slot,
                          created_at, updated_at
                   FROM projects WHERE id = ?""",
                (project_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Project not found")
            out = dict(row)
            docs = conn.execute(
                """SELECT id, doc_type, title, filename, file_url, file_size, page_count, is_main, ce_category, created_at
                   FROM documents WHERE project_id = ? ORDER BY is_main DESC, created_at""",
                (project_id,),
            ).fetchall()
            out["documents"] = [dict(d) for d in docs]
            milestones = conn.execute(
                """SELECT id, event_type, event_date, description, source_doc
                   FROM milestones WHERE project_id = ? ORDER BY event_date NULLS LAST, id""",
                (project_id,),
            ).fetchall()
            out["milestones"] = [dict(m) for m in milestones]
            return out
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/scan
# ---------------------------------------------------------------------------

@router.post("/{project_id}/scan")
def project_scan(project_id: str) -> dict[str, Any]:
    """Run scan_project; pass process_type from project. Returns flags grouped by severity."""
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT id, process_type FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Project not found")
            process_type = row["process_type"] or "EA"
            flags = scan_project(project_id, conn, process_type=process_type)
            conn.commit()
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for f in flags:
                grouped[f["severity"]].append(f)
            return dict(grouped)
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/flags
# ---------------------------------------------------------------------------

@router.get("/{project_id}/flags")
def project_flags(project_id: str) -> list[dict[str, Any]]:
    """All flags previously stored for the project."""
    try:
        conn = _get_conn()
        try:
            if not conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone():
                raise HTTPException(status_code=404, detail="Project not found")
            rows = conn.execute(
                """SELECT id, project_id, document_id, flag_type, severity, title, description, excerpt, char_offset, scanned_at
                   FROM flags WHERE project_id = ? ORDER BY severity, id""",
                (project_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/attest — stub
# ---------------------------------------------------------------------------

@router.post("/{project_id}/attest")
def project_attest(project_id: str) -> dict[str, str]:
    """Stub: attestation not implemented yet."""
    return {"status": "not_implemented"}


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/verify — stub
# ---------------------------------------------------------------------------

@router.get("/{project_id}/verify")
def project_verify(project_id: str) -> dict[str, str]:
    """Stub: verify not implemented yet."""
    return {"status": "not_implemented"}
