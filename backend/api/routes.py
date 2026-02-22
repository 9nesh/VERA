"""
FastAPI routes for VERA — lender-facing NEPA risk tool.

All DB access uses config.DB_PATH. Every endpoint wraps in try/except
and returns a clean 500 on failure. No hardcoded paths.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel

from backend.config import DB_PATH, SOLANA_KEYPAIR_PATH, SOLANA_RPC_URL
from backend.intelligence.chat import answer_global_question, answer_project_question, explain_flag
from backend.intelligence.signals import scan_project
from backend.solana.attest import attest_project, verify_attestation


router = APIRouter(prefix="/projects", tags=["projects"])


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# GET /api/projects/search
# ---------------------------------------------------------------------------

def _fts_available(conn: sqlite3.Connection) -> bool:
    """Return True if the projects_fts virtual table exists and has rows."""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projects_fts'"
        ).fetchone()
        if not row:
            return False
        count = conn.execute("SELECT COUNT(*) FROM projects_fts").fetchone()[0]
        return count > 0
    except Exception:
        return False


def _fts_query(q: str) -> str:
    """
    Escape a raw user query for FTS5 MATCH syntax.
    Wraps each token in double-quotes so special characters are treated literally,
    then joins with AND so all terms must appear.
    """
    tokens = [t.strip() for t in q.split() if t.strip()]
    if not tokens:
        return '""'
    return " ".join(f'"{t.replace(chr(34), "")}"' for t in tokens)


@router.get("/search")
def projects_search(
    q: str = Query(..., min_length=1),
    process_type: str | None = Query(None),
) -> list[dict[str, Any]]:
    """Full-text search on project title, agency, state (FTS5 when available, LIKE fallback)."""
    try:
        conn = _get_conn()
        try:
            if _fts_available(conn):
                # FTS5 path: fast, ranked by relevance
                fts_q = _fts_query(q)
                if process_type:
                    rows = conn.execute(
                        """SELECT p.id, p.title, p.process_type, p.agency, p.state,
                                  p.register_date, p.status
                           FROM projects_fts f
                           JOIN projects p ON p.id = f.id
                           WHERE projects_fts MATCH ?
                             AND p.process_type = ?
                           ORDER BY rank
                           LIMIT 50""",
                        (fts_q, process_type),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT p.id, p.title, p.process_type, p.agency, p.state,
                                  p.register_date, p.status
                           FROM projects_fts f
                           JOIN projects p ON p.id = f.id
                           WHERE projects_fts MATCH ?
                           ORDER BY rank
                           LIMIT 50""",
                        (fts_q,),
                    ).fetchall()
            else:
                # LIKE fallback (before fts index is built)
                pattern = f"%{q}%"
                if process_type:
                    rows = conn.execute(
                        """SELECT id, title, process_type, agency, state, register_date, status
                           FROM projects
                           WHERE (title LIKE ? OR agency LIKE ? OR state LIKE ?)
                             AND process_type = ?
                           ORDER BY register_date DESC NULLS LAST
                           LIMIT 50""",
                        (pattern, pattern, pattern, process_type),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, title, process_type, agency, state, register_date, status
                           FROM projects
                           WHERE title LIKE ? OR agency LIKE ? OR state LIKE ?
                           ORDER BY register_date DESC NULLS LAST
                           LIMIT 50""",
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
    """Run scan_project; returns deduplicated flags grouped by severity with source_documents."""
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
            doc_ids = list({f["document_id"] for f in flags if f.get("document_id")})
            doc_map = {}
            if doc_ids:
                placeholders = ",".join("?" * len(doc_ids))
                doc_rows = conn.execute(
                    f"SELECT id, title, filename FROM documents WHERE id IN ({placeholders})",
                    doc_ids,
                ).fetchall()
                doc_map = {r["id"]: dict(r) for r in doc_rows}
            deduped = _dedupe_flags_with_sources(flags, doc_map)
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for f in deduped:
                grouped[f["severity"]].append(f)
            return dict(grouped)
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _dedupe_flags_with_sources(
    rows: list[dict[str, Any]],
    doc_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group flags by (flag_type, severity), attach source_documents (id, title, filename)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = (r["flag_type"], r["severity"])
        groups[key].append(r)
    out: list[dict[str, Any]] = []
    _sev_order = ("high", "medium", "low", "info")
    for (flag_type, severity), group in sorted(
        groups.items(),
        key=lambda x: (_sev_order.index(x[0][1]) if x[0][1] in _sev_order else 99, x[0][0]),
    ):
        first = group[0]
        doc_ids = list({r["document_id"] for r in group if r.get("document_id")})
        source_documents = []
        for did in doc_ids:
            d = doc_map.get(did) or {}
            source_documents.append({
                "document_id": did,
                "title": d.get("title"),
                "filename": d.get("filename"),
            })
        out.append({
            "flag_type": flag_type,
            "severity": severity,
            "title": first.get("title"),
            "description": first.get("description"),
            "excerpt": first.get("excerpt"),
            "char_offset": first.get("char_offset"),
            "source_documents": source_documents,
        })
    return out


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/flags
# ---------------------------------------------------------------------------

@router.get("/{project_id}/flags")
async def project_flags(
    project_id: str,
    include_explanation: bool = Query(False, description="Generate LLM explanation per unique flag"),
) -> list[dict[str, Any]]:
    """Deduplicated flags for the project with source document(s); optional context-specific LLM explanation."""
    try:
        conn = _get_conn()
        try:
            proj = conn.execute("SELECT id, title FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj:
                raise HTTPException(status_code=404, detail="Project not found")
            project_title = proj["title"] or project_id
            rows = conn.execute(
                """SELECT id, project_id, document_id, flag_type, severity, title, description, excerpt, char_offset, scanned_at
                   FROM flags WHERE project_id = ? ORDER BY severity, id""",
                (project_id,),
            ).fetchall()
            raw = [dict(r) for r in rows]
            doc_ids = list({r["document_id"] for r in raw if r.get("document_id")})
            doc_map = {}
            if doc_ids:
                placeholders = ",".join("?" * len(doc_ids))
                doc_rows = conn.execute(
                    f"SELECT id, title, filename FROM documents WHERE id IN ({placeholders})",
                    doc_ids,
                ).fetchall()
                doc_map = {r["id"]: dict(r) for r in doc_rows}
        finally:
            conn.close()
        deduped = _dedupe_flags_with_sources(raw, doc_map)
        if include_explanation and deduped:
            for f in deduped:
                source_names = [
                    (d.get("filename") or d.get("title") or d.get("document_id") or "document")
                    for d in f.get("source_documents", [])
                ]
                f["explanation"] = await explain_flag(
                    project_title,
                    f["flag_type"],
                    f.get("title"),
                    f.get("description"),
                    f.get("excerpt"),
                    source_names,
                )
        return deduped
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/attest
# ---------------------------------------------------------------------------

@router.post("/{project_id}/attest")
async def project_attest(project_id: str) -> dict[str, Any]:
    """Build attestation payload from stored flags, send to Solana via SPL Memo, store result."""
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT id, process_type, solana_tx_signature FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Project not found")

            # Fetch stored flags
            flag_rows = conn.execute(
                "SELECT flag_type, severity, title FROM flags WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            flags = [dict(f) for f in flag_rows]

            # Fetch doc hashes for integrity
            doc_rows = conn.execute(
                "SELECT id, sha256 FROM documents WHERE project_id = ? AND sha256 IS NOT NULL",
                (project_id,),
            ).fetchall()
            doc_hashes = {r["id"]: r["sha256"] for r in doc_rows}
        finally:
            conn.close()

        # Call Solana attestation
        result = await attest_project(
            project_id=project_id,
            flags=flags,
            doc_hashes=doc_hashes,
            keypair_path=str(SOLANA_KEYPAIR_PATH),
            rpc_url=SOLANA_RPC_URL,
        )

        # Store in DB
        attested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn2 = _get_conn()
        try:
            conn2.execute(
                """UPDATE projects
                   SET solana_tx_signature = ?,
                       solana_attested_at  = ?,
                       solana_slot         = ?
                   WHERE id = ?""",
                (result["tx_signature"], attested_at, result.get("slot"), project_id),
            )
            conn2.commit()
        finally:
            conn2.close()

        return {
            "tx_signature": result["tx_signature"],
            "explorer_url": result["explorer_url"],
            "attested_at": attested_at,
            "payload_hash": result["payload_hash"],
            "slot": result.get("slot"),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/verify
# ---------------------------------------------------------------------------

@router.get("/{project_id}/verify")
async def project_verify(project_id: str) -> dict[str, Any]:
    """Verify the stored on-chain attestation for a project."""
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT solana_tx_signature FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Project not found")
            tx_sig = row["solana_tx_signature"]
        finally:
            conn.close()

        if not tx_sig:
            raise HTTPException(status_code=409, detail="Project has not been attested yet")

        result = await verify_attestation(
            tx_signature=tx_sig,
            expected_hash="",  # hash self-validates via memo_content check
            rpc_url=SOLANA_RPC_URL,
        )
        return {**result, "tx_signature": tx_sig}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/chat
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    question: str


@router.post("/{project_id}/chat")
async def project_chat(project_id: str, body: ChatRequest) -> dict[str, Any]:
    """Answer a question scoped to a single project's documents."""
    try:
        conn = _get_conn()
        try:
            if not conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone():
                raise HTTPException(status_code=404, detail="Project not found")
            result = await answer_project_question(project_id, body.question, conn)
        finally:
            conn.close()
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/chat  (cross-project — mounted separately in main.py)
# ---------------------------------------------------------------------------

global_router = APIRouter(tags=["chat"])


@global_router.post("/chat")
async def global_chat(body: ChatRequest) -> dict[str, Any]:
    """Answer a question across all projects."""
    try:
        conn = _get_conn()
        try:
            result = await answer_global_question(body.question, conn)
        finally:
            conn.close()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
