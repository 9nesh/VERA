"""
VERA NEPA MCP Server

Exposes the VERA SQLite database to an MCP-compatible chatbot (e.g. Claude
Desktop) via 10 structured tools covering project discovery, document search,
compliance flags, and aggregate statistics.

Usage:
    python server.py          # stdio transport (default for MCP)
    python server.py --sse    # SSE transport on port 8001
"""

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — allow running from anywhere by resolving relative to this file
# ---------------------------------------------------------------------------

_MCP_DIR = Path(__file__).parent
_BACKEND_DIR = _MCP_DIR.parent
_ROOT = _BACKEND_DIR.parent

sys.path.insert(0, str(_BACKEND_DIR))
from config import DB_PATH

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "vera-nepa",
    instructions=(
        "You have access to the VERA NEPA database, which contains information "
        "about National Environmental Policy Act (NEPA) projects across the United "
        "States. Projects are categorized as CE (Categorical Exclusion), EA "
        "(Environmental Assessment), or EIS (Environmental Impact Statement). "
        "Use the available tools to search projects, retrieve documents, inspect "
        "compliance flags, and surface statistics. Always call get_database_stats() "
        "first when a user asks a broad or open-ended question about the database."
    ),
)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    """Open a read-only connection to the VERA SQLite database."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _excerpt(text: str, query: str, window: int = 300) -> str:
    """Return a ~window-char snippet of *text* centered on the first hit of *query*."""
    if not text or not query:
        return ""
    idx = text.lower().find(query.lower())
    if idx == -1:
        return text[:window]
    start = max(0, idx - window // 2)
    end = min(len(text), start + window)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _clamp(value: int | None, default: int = 20, maximum: int = 100) -> int:
    if value is None:
        return default
    return max(1, min(value, maximum))


# ---------------------------------------------------------------------------
# Tools — Project discovery
# ---------------------------------------------------------------------------


@mcp.tool()
def search_projects(
    query: str | None = None,
    process_type: str | None = None,
    agency: str | None = None,
    state: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search for NEPA projects.

    Use this tool whenever the user asks about projects in a specific state,
    agency, or of a certain NEPA process type (CE / EA / EIS). Returns a list
    of matching projects with key metadata. Call get_project() to drill into
    a specific project's full details.

    Args:
        query: Free-text search across title, agency, state, county, and
            lead_office (case-insensitive partial match).
        process_type: Filter by NEPA process type — one of "CE", "EA", "EIS".
        agency: Filter by agency name (partial match). E.g. "BLM", "Forest Service".
        state: Filter by US state abbreviation or full name (partial match).
        status: Filter by project status (partial match). E.g. "complete".
        date_from: Only include projects with register_date >= this value (ISO-8601).
        date_to: Only include projects with register_date <= this value (ISO-8601).
        limit: Maximum number of results to return (1–100, default 20).

    Returns:
        List of project objects with fields: id, title, process_type, agency,
        state, county, lead_office, register_date, status, project_url.
    """
    limit = _clamp(limit)
    clauses: list[str] = []
    params: list[Any] = []

    if query:
        clauses.append(
            "(title LIKE ? OR agency LIKE ? OR state LIKE ? OR county LIKE ? OR lead_office LIKE ?)"
        )
        q = f"%{query}%"
        params.extend([q, q, q, q, q])
    if process_type:
        clauses.append("process_type = ?")
        params.append(process_type.upper())
    if agency:
        clauses.append("agency LIKE ?")
        params.append(f"%{agency}%")
    if state:
        clauses.append("state LIKE ?")
        params.append(f"%{state}%")
    if status:
        clauses.append("status LIKE ?")
        params.append(f"%{status}%")
    if date_from:
        clauses.append("register_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("register_date <= ?")
        params.append(date_to)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT id, title, process_type, agency, state, county, lead_office,
               register_date, status, project_url
        FROM   projects
        {where}
        ORDER BY register_date DESC NULLS LAST
        LIMIT  ?
    """
    params.append(limit)

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
def get_project(project_id: str) -> dict[str, Any]:
    """Get full details for a single NEPA project.

    Use this after search_projects() when the user wants to know more about a
    specific project. Returns project metadata, a list of its documents, its
    milestone timeline, and a summary of compliance flag counts by severity.

    Args:
        project_id: The project's unique ID (from search_projects results).

    Returns:
        Object with fields:
        - metadata: all project columns
        - documents: list of documents (id, doc_type, title, filename,
          file_url, page_count, is_main, ce_category)
        - milestones: list of timeline events ordered by date
        - flag_summary: dict mapping severity level to count
          (e.g. {"high": 2, "medium": 1, "low": 0, "info": 3})
    """
    try:
        with _connect() as conn:
            project = conn.execute(
                """SELECT id, title, process_type, agency, state, county,
                          lead_office, register_date, status, project_url,
                          solana_tx_signature, solana_attested_at, solana_slot,
                          created_at, updated_at
                   FROM   projects WHERE id = ?""",
                [project_id],
            ).fetchone()

            if project is None:
                return {"error": f"Project '{project_id}' not found."}

            docs = conn.execute(
                """SELECT id, doc_type, title, filename, file_url,
                          page_count, is_main, ce_category
                   FROM   documents WHERE project_id = ?
                   ORDER BY is_main DESC, doc_type""",
                [project_id],
            ).fetchall()

            milestones = conn.execute(
                """SELECT event_type, event_date, description, source_doc
                   FROM   milestones WHERE project_id = ?
                   ORDER BY event_date NULLS LAST""",
                [project_id],
            ).fetchall()

            flag_rows = conn.execute(
                """SELECT severity, COUNT(*) as cnt
                   FROM   flags WHERE project_id = ?
                   GROUP BY severity""",
                [project_id],
            ).fetchall()

        flag_summary: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "info": 0}
        for row in flag_rows:
            flag_summary[row["severity"]] = row["cnt"]

        return {
            "metadata": dict(project),
            "documents": _rows_to_dicts(docs),
            "milestones": _rows_to_dicts(milestones),
            "flag_summary": flag_summary,
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def list_agencies(process_type: str | None = None) -> list[dict[str, Any]]:
    """List all agencies in the database with their project counts.

    Use this when the user asks which agencies have NEPA projects, or wants to
    compare activity across agencies. Optionally filter by process type.

    Args:
        process_type: Optional filter — "CE", "EA", or "EIS".

    Returns:
        List of objects with fields: agency, project_count, process_types
        (comma-separated list of process types this agency has), sorted by
        project_count descending.
    """
    params: list[Any] = []
    where = ""
    if process_type:
        where = "WHERE process_type = ?"
        params.append(process_type.upper())

    sql = f"""
        SELECT agency,
               COUNT(*)                              AS project_count,
               GROUP_CONCAT(DISTINCT process_type)  AS process_types
        FROM   projects
        {where}
        WHERE  agency IS NOT NULL
        GROUP BY agency
        ORDER BY project_count DESC
    """
    # SQLite doesn't allow two WHERE clauses — merge conditions
    if process_type:
        sql = f"""
            SELECT agency,
                   COUNT(*)                              AS project_count,
                   GROUP_CONCAT(DISTINCT process_type)  AS process_types
            FROM   projects
            WHERE  process_type = ? AND agency IS NOT NULL
            GROUP BY agency
            ORDER BY project_count DESC
        """
    else:
        sql = """
            SELECT agency,
                   COUNT(*)                              AS project_count,
                   GROUP_CONCAT(DISTINCT process_type)  AS process_types
            FROM   projects
            WHERE  agency IS NOT NULL
            GROUP BY agency
            ORDER BY project_count DESC
        """

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
def list_states(process_type: str | None = None) -> list[dict[str, Any]]:
    """List all US states in the database with their project counts.

    Use this when the user asks about project distribution by state, or wants
    to know which states have the most NEPA activity.

    Args:
        process_type: Optional filter — "CE", "EA", or "EIS".

    Returns:
        List of objects with fields: state, project_count, sorted by
        project_count descending.
    """
    params: list[Any] = []
    if process_type:
        sql = """
            SELECT state, COUNT(*) AS project_count
            FROM   projects
            WHERE  process_type = ? AND state IS NOT NULL
            GROUP BY state
            ORDER BY project_count DESC
        """
        params.append(process_type.upper())
    else:
        sql = """
            SELECT state, COUNT(*) AS project_count
            FROM   projects
            WHERE  state IS NOT NULL
            GROUP BY state
            ORDER BY project_count DESC
        """

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Tools — Documents
# ---------------------------------------------------------------------------


@mcp.tool()
def get_project_documents(
    project_id: str,
    doc_type: str | None = None,
) -> list[dict[str, Any]]:
    """Get all documents associated with a NEPA project.

    Use this when the user asks about the documents, files, or attachments for
    a specific project. Can optionally filter by document type (e.g. "FEIS",
    "DEIS", "ROD", "EA", "FONSI", "CE").

    Args:
        project_id: The project's unique ID.
        doc_type: Optional document type filter (partial match, case-insensitive).
            Common types: FEIS, DEIS, ROD, EA, FONSI, CE, NOI, SUPPLEMENTAL.

    Returns:
        List of document objects with fields: id, doc_type, title, filename,
        file_url, file_size, page_count, is_main, ce_category, sha256,
        created_at. text_content is excluded to keep responses concise — use
        search_document_content() to search within document text.
    """
    params: list[Any] = [project_id]
    where_extra = ""
    if doc_type:
        where_extra = "AND doc_type LIKE ?"
        params.append(f"%{doc_type}%")

    sql = f"""
        SELECT id, doc_type, title, filename, file_url, file_size,
               page_count, is_main, ce_category, sha256, created_at
        FROM   documents
        WHERE  project_id = ? {where_extra}
        ORDER BY is_main DESC, doc_type
    """

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return [{"message": f"No documents found for project '{project_id}'."}]
        return _rows_to_dicts(rows)
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
def search_document_content(
    query: str,
    process_type: str | None = None,
    agency: str | None = None,
    state: str | None = None,
    doc_type: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Full-text search within the text content of NEPA documents.

    Use this when the user wants to find projects or documents that discuss a
    specific topic, phrase, or concept — for example "tribal consultation",
    "endangered species", "groundwater contamination", or "deferred mitigation".
    Returns matching excerpts with surrounding context and the project they
    belong to.

    Args:
        query: The text to search for (case-insensitive partial match).
        process_type: Limit to projects of this type — "CE", "EA", or "EIS".
        agency: Limit to documents from projects of this agency (partial match).
        state: Limit to documents from projects in this state (partial match).
        doc_type: Limit to documents of this type (partial match). E.g. "FEIS".
        limit: Maximum results to return (1–100, default 10).

    Returns:
        List of objects with fields: document_id, doc_type, document_title,
        filename, project_id, project_title, agency, state, process_type,
        excerpt (300-char snippet centered on the match).
    """
    limit = _clamp(limit)
    params: list[Any] = [f"%{query}%"]
    extra_clauses: list[str] = []

    if process_type:
        extra_clauses.append("p.process_type = ?")
        params.append(process_type.upper())
    if agency:
        extra_clauses.append("p.agency LIKE ?")
        params.append(f"%{agency}%")
    if state:
        extra_clauses.append("p.state LIKE ?")
        params.append(f"%{state}%")
    if doc_type:
        extra_clauses.append("d.doc_type LIKE ?")
        params.append(f"%{doc_type}%")

    extra_where = ""
    if extra_clauses:
        extra_where = "AND " + " AND ".join(extra_clauses)

    sql = f"""
        SELECT d.id          AS document_id,
               d.doc_type,
               d.title       AS document_title,
               d.filename,
               d.text_content,
               p.id          AS project_id,
               p.title       AS project_title,
               p.agency,
               p.state,
               p.process_type
        FROM   documents d
        JOIN   projects  p ON p.id = d.project_id
        WHERE  d.text_content LIKE ?
        {extra_where}
        LIMIT  ?
    """
    params.append(limit)

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            text = d.pop("text_content", "") or ""
            d["excerpt"] = _excerpt(text, query)
            results.append(d)
        return results
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Tools — Compliance flags
# ---------------------------------------------------------------------------


@mcp.tool()
def get_project_flags(
    project_id: str,
    severity: str | None = None,
    flag_type: str | None = None,
) -> list[dict[str, Any]]:
    """Get compliance flags (risk signals) for a specific NEPA project.

    Use this when the user asks about compliance issues, risk signals, or
    problems identified in a specific project's documents. Flags are generated
    by automated scanning of document text.

    Flag types include:
    - deferred_mitigation: Mitigation measures pushed to future phases
    - future_studies_reliance: Critical analysis deferred to future studies
    - ej_absent: Environmental justice analysis missing
    - ej_thin_coverage: Environmental justice analysis is superficial
    - no_action_absent: No-action alternative not analyzed
    - no_action_thin: No-action alternative analysis is thin
    - cumulative_impacts_thin: Cumulative impacts analysis is superficial
    - tribal_interests: Tribal consultation or interests flagged

    Args:
        project_id: The project's unique ID.
        severity: Filter by severity — "high", "medium", "low", or "info".
        flag_type: Filter by flag type (exact match, e.g. "deferred_mitigation").

    Returns:
        List of flag objects with fields: id, flag_type, severity, title,
        description, excerpt, document_id, scanned_at.
    """
    params: list[Any] = [project_id]
    extra: list[str] = []

    if severity:
        extra.append("severity = ?")
        params.append(severity.lower())
    if flag_type:
        extra.append("flag_type = ?")
        params.append(flag_type)

    extra_where = ("AND " + " AND ".join(extra)) if extra else ""

    sql = f"""
        SELECT id, flag_type, severity, title, description, excerpt,
               document_id, scanned_at
        FROM   flags
        WHERE  project_id = ? {extra_where}
        ORDER BY
            CASE severity
                WHEN 'high'   THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low'    THEN 3
                ELSE               4
            END,
            flag_type
    """

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return [{"message": f"No flags found for project '{project_id}' with the given filters."}]
        return _rows_to_dicts(rows)
    except Exception as exc:
        return [{"error": str(exc)}]


@mcp.tool()
def get_flags_across_projects(
    severity: str | None = None,
    flag_type: str | None = None,
    agency: str | None = None,
    state: str | None = None,
    process_type: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get compliance flags across multiple projects with optional filters.

    Use this when the user asks cross-project questions about compliance issues,
    such as "Which EIS projects in California have high-severity flags?" or
    "Show all deferred mitigation findings from BLM projects." Also useful for
    identifying patterns and systemic issues across the database.

    Args:
        severity: Filter by severity — "high", "medium", "low", or "info".
        flag_type: Filter by flag type (exact match). See get_project_flags()
            for the full list of flag types.
        agency: Filter to projects from this agency (partial match).
        state: Filter to projects in this state (partial match).
        process_type: Filter by NEPA process type — "CE", "EA", or "EIS".
        limit: Maximum results (1–100, default 20).

    Returns:
        List of flag objects with fields: flag_id, flag_type, severity, title,
        description, excerpt, project_id, project_title, agency, state,
        process_type, scanned_at.
    """
    limit = _clamp(limit)
    clauses: list[str] = []
    params: list[Any] = []

    if severity:
        clauses.append("f.severity = ?")
        params.append(severity.lower())
    if flag_type:
        clauses.append("f.flag_type = ?")
        params.append(flag_type)
    if agency:
        clauses.append("p.agency LIKE ?")
        params.append(f"%{agency}%")
    if state:
        clauses.append("p.state LIKE ?")
        params.append(f"%{state}%")
    if process_type:
        clauses.append("p.process_type = ?")
        params.append(process_type.upper())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    sql = f"""
        SELECT f.id          AS flag_id,
               f.flag_type,
               f.severity,
               f.title,
               f.description,
               f.excerpt,
               f.scanned_at,
               p.id          AS project_id,
               p.title       AS project_title,
               p.agency,
               p.state,
               p.process_type
        FROM   flags    f
        JOIN   projects p ON p.id = f.project_id
        {where}
        ORDER BY
            CASE f.severity
                WHEN 'high'   THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low'    THEN 3
                ELSE               4
            END,
            f.scanned_at DESC
        LIMIT  ?
    """
    params.append(limit)

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return [{"message": "No flags found matching the given filters."}]
        return _rows_to_dicts(rows)
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Tools — Statistics & summaries
# ---------------------------------------------------------------------------


@mcp.tool()
def get_database_stats() -> dict[str, Any]:
    """Get high-level statistics about the VERA NEPA database.

    Call this at the start of a session or when the user asks broad questions
    like "What's in the database?", "How many projects do you have?", or
    "Give me an overview." Returns aggregate counts and breakdowns useful for
    orienting the conversation.

    Returns:
        Object with:
        - total_projects: total project count
        - projects_by_type: count for each of CE, EA, EIS
        - projects_by_status: top statuses with counts
        - total_documents: total document count
        - total_flags: total flag count
        - flags_by_severity: count per severity level
        - flags_by_type: count per flag type
        - flagged_projects: number of projects that have at least one flag
        - top_agencies: top 10 agencies by project count
        - top_states: top 10 states by project count
        - date_range: earliest and latest register_date in the database
        - scanned_projects: number of projects that have been scanned for flags
    """
    try:
        with _connect() as conn:
            total_projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

            by_type = conn.execute(
                "SELECT process_type, COUNT(*) AS cnt FROM projects GROUP BY process_type"
            ).fetchall()

            by_status = conn.execute(
                """SELECT status, COUNT(*) AS cnt FROM projects
                   WHERE status IS NOT NULL
                   GROUP BY status ORDER BY cnt DESC LIMIT 10"""
            ).fetchall()

            total_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

            total_flags = conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0]

            flags_by_sev = conn.execute(
                "SELECT severity, COUNT(*) AS cnt FROM flags GROUP BY severity"
            ).fetchall()

            flags_by_type = conn.execute(
                "SELECT flag_type, COUNT(*) AS cnt FROM flags GROUP BY flag_type ORDER BY cnt DESC"
            ).fetchall()

            flagged_projects = conn.execute(
                "SELECT COUNT(DISTINCT project_id) FROM flags"
            ).fetchone()[0]

            scanned_projects = conn.execute(
                "SELECT COUNT(DISTINCT project_id) FROM flags"
            ).fetchone()[0]

            top_agencies = conn.execute(
                """SELECT agency, COUNT(*) AS cnt FROM projects
                   WHERE agency IS NOT NULL
                   GROUP BY agency ORDER BY cnt DESC LIMIT 10"""
            ).fetchall()

            top_states = conn.execute(
                """SELECT state, COUNT(*) AS cnt FROM projects
                   WHERE state IS NOT NULL
                   GROUP BY state ORDER BY cnt DESC LIMIT 10"""
            ).fetchall()

            date_range = conn.execute(
                """SELECT MIN(register_date) AS earliest, MAX(register_date) AS latest
                   FROM projects WHERE register_date IS NOT NULL"""
            ).fetchone()

        return {
            "total_projects": total_projects,
            "projects_by_type": {r["process_type"]: r["cnt"] for r in by_type},
            "projects_by_status": _rows_to_dicts(by_status),
            "total_documents": total_docs,
            "total_flags": total_flags,
            "flags_by_severity": {r["severity"]: r["cnt"] for r in flags_by_sev},
            "flags_by_type": _rows_to_dicts(flags_by_type),
            "flagged_projects": flagged_projects,
            "scanned_projects": scanned_projects,
            "top_agencies": _rows_to_dicts(top_agencies),
            "top_states": _rows_to_dicts(top_states),
            "date_range": dict(date_range) if date_range else {},
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def get_project_timeline(project_id: str) -> list[dict[str, Any]]:
    """Get the full milestone timeline for a NEPA project.

    Use this when the user asks about the history, timeline, key dates, or
    phases of a specific project — for example "When was the Notice of Intent
    published?" or "What is the current stage of this project?"

    Common event types include: Notice of Intent (NOI), Public Comment Period,
    Draft EIS, Final EIS, Record of Decision (ROD), FONSI, CE Determination.

    Args:
        project_id: The project's unique ID.

    Returns:
        List of milestone objects ordered by event_date, with fields:
        event_type, event_date, description, source_doc.
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT event_type, event_date, description, source_doc
                   FROM   milestones
                   WHERE  project_id = ?
                   ORDER BY event_date NULLS LAST""",
                [project_id],
            ).fetchall()

        if not rows:
            return [{"message": f"No milestones found for project '{project_id}'."}]
        return _rows_to_dicts(rows)
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VERA NEPA MCP Server")
    parser.add_argument(
        "--sse",
        action="store_true",
        help="Use SSE transport instead of stdio (runs HTTP server on port 8001)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port for SSE transport (default: 8001)",
    )
    args = parser.parse_args()

    if args.sse:
        mcp.run(transport="sse", port=args.port)
    else:
        mcp.run(transport="stdio")
