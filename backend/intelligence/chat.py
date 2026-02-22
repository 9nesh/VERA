"""
Retrieval-augmented Q&A for VERA.

Two entry points:
  answer_project_question — scoped to one project's document text
  answer_global_question  — cross-project keyword search then synthesis

Project chat includes: project card (metadata), document list, stored compliance
flags (for risk questions), and optionally similar projects when asked.
"""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any

import httpx

from backend.llm import client as llm

_OLLAMA_UNAVAILABLE = (
    "The AI model (Ollama) is not running. "
    "Start it with `ollama serve` and try again."
)

# Max total characters of excerpts to avoid overflowing context
_MAX_EXCERPT_CHARS = 12_000
_TOP_CHUNKS = 8


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_text(text: str, size: int = 1000, overlap: int = 100) -> list[str]:
    """Split text into overlapping fixed-size character chunks."""
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + size, length)
        chunks.append(text[start:end])
        if end == length:
            break
        start += size - overlap
    return chunks


# ---------------------------------------------------------------------------
# Scoring: keyword TF-IDF (no external deps)
# ---------------------------------------------------------------------------

_STOP = frozenset(
    "a an the and or but in on at to of for is are was were be been being "
    "have has had do does did will would could should may might must shall "
    "with from by about as into through during before after above below "
    "between this that these those it its we us our they them their i my "
    "he she his her him you your".split()
)


def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[a-zA-Z]{2,}", text) if w.lower() not in _STOP]


def _score_chunks(question: str, chunks: list[str], top_n: int = 5) -> list[tuple[float, str]]:
    """
    Score each chunk against the question using TF × IDF weighting.
    Returns top_n (score, chunk_text) pairs, highest score first.
    """
    q_terms = set(_tokenize(question))
    if not q_terms:
        return [(0.0, c) for c in chunks[:top_n]]

    n_docs = len(chunks)
    # IDF: log(N / df) where df = number of chunks containing the term
    df: dict[str, int] = {}
    chunk_tokens: list[list[str]] = []
    for chunk in chunks:
        tokens = _tokenize(chunk)
        chunk_tokens.append(tokens)
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    scored: list[tuple[float, str]] = []
    for tokens, chunk in zip(chunk_tokens, chunks):
        if not tokens:
            scored.append((0.0, chunk))
            continue
        tf_idf = 0.0
        token_count = len(tokens)
        for term in q_terms:
            if term not in df:
                continue
            tf = tokens.count(term) / token_count
            idf = math.log((n_docs + 1) / (df[term] + 1)) + 1
            tf_idf += tf * idf
        scored.append((tf_idf, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_n]


# ---------------------------------------------------------------------------
# Project context helpers (DB)
# ---------------------------------------------------------------------------

def _get_project_card(conn: sqlite3.Connection, project_id: str) -> str:
    """One-line project metadata for prompt context."""
    row = conn.execute(
        "SELECT title, process_type, agency, state FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if not row:
        return ""
    parts = [f"Title: {row['title']}", f"Process: {row['process_type']}"]
    if row["agency"]:
        parts.append(f"Agency: {row['agency']}")
    if row["state"]:
        parts.append(f"State: {row['state']}")
    return " | ".join(parts)


def _get_document_list(conn: sqlite3.Connection, project_id: str) -> str:
    """Formatted list of documents in this project."""
    rows = conn.execute(
        """SELECT doc_type, title, filename, page_count, is_main
           FROM documents WHERE project_id = ? ORDER BY is_main DESC, created_at""",
        (project_id,),
    ).fetchall()
    if not rows:
        return "No documents listed."
    lines = []
    for r in rows:
        label = r["title"] or r["filename"] or "(no title)"
        doc_type = r["doc_type"] or "—"
        pages = f", {r['page_count']} pages" if r["page_count"] else ""
        main = " [main]" if r["is_main"] else ""
        lines.append(f"  - {label} (type: {doc_type}{pages}){main}")
    return "\n".join(lines)


def _get_project_flags(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    """Stored compliance flags for this project, deduped by (flag_type, severity) with source document names."""
    rows = conn.execute(
        """SELECT f.flag_type, f.severity, f.title, f.document_id, d.filename, d.title AS doc_title
           FROM flags f
           LEFT JOIN documents d ON d.id = f.document_id
           WHERE f.project_id = ? ORDER BY f.severity, f.id""",
        (project_id,),
    ).fetchall()
    raw = [dict(r) for r in rows]
    # Group by (flag_type, severity), collect source doc names
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in raw:
        key = (r["flag_type"], r["severity"])
        if key not in groups:
            groups[key] = []
        groups[key].append(r)
    out = []
    for (flag_type, severity), group in groups.items():
        first = group[0]
        names = list({
            (r.get("filename") or r.get("doc_title") or r.get("document_id") or "document")
            for r in group if r.get("document_id")
        })
        out.append({
            "flag_type": flag_type,
            "severity": severity,
            "title": first.get("title"),
            "source_document_names": names,
        })
    return out


_FLAG_EXPLAIN_SYSTEM = (
    "You are VERA, an environmental compliance assistant for NEPA due diligence. "
    "In one or two short sentences, explain what this compliance flag means in the context of this project "
    "and why a lender or reviewer should care. Be specific to the project and flag; do not repeat the flag title."
)


async def explain_flag(
    project_title: str,
    flag_type: str,
    title: str | None,
    description: str | None,
    excerpt: str | None,
    source_doc_names: list[str],
) -> str:
    """Generate a context-specific LLM explanation for a compliance flag."""
    source_str = ", ".join(source_doc_names) if source_doc_names else "project documents"
    prompt = (
        f"Project: {project_title}\n"
        f"Flag type: {flag_type}\n"
        f"Title: {title or flag_type}\n"
        f"Description: {description or 'N/A'}\n"
        f"Source document(s): {source_str}\n"
    )
    if excerpt:
        prompt += f"Relevant excerpt (abbreviated): {excerpt[:500]}{'...' if len(excerpt) > 500 else ''}\n"
    prompt += "\nExplain in 1–2 sentences what this flag means for this project and why it matters to a lender."
    try:
        out = await llm.generate(prompt, system=_FLAG_EXPLAIN_SYSTEM, temperature=0.2)
        return (out or "").strip()
    except Exception:
        return ""


def _get_similar_projects(
    conn: sqlite3.Connection,
    project_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Other projects with same process_type and agency (exclude current)."""
    row = conn.execute(
        "SELECT process_type, agency FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if not row:
        return []
    pt, agency = row["process_type"], row["agency"]
    if agency:
        rows = conn.execute(
            """SELECT id, title FROM projects
               WHERE id != ? AND process_type = ? AND agency = ?
               ORDER BY register_date DESC NULLS LAST
               LIMIT ?""",
            (project_id, pt, agency, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, title FROM projects
               WHERE id != ? AND process_type = ?
               ORDER BY register_date DESC NULLS LAST
               LIMIT ?""",
            (project_id, pt, limit),
        ).fetchall()
    return [{"project_id": r["id"], "title": r["title"]} for r in rows]


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are VERA, an environmental compliance assistant for infrastructure lenders doing NEPA due diligence. "
    "Answer based on the PROJECT CARD, DOCUMENTS LIST, and EXCERPTS provided. Use the document list when asked what documents exist. "
    "Each excerpt is labeled 'from: [filename or title]'—when the user asks about a specific document by name or filename, use only the excerpts that are from that document to answer; summarize what that document contains. "
    "If the answer is not in the provided context, say so explicitly. Do not hallucinate. Be concise and precise. "
    "When the user asks about financial, litigation, or delay risks: (1) Use the COMPLIANCE FLAGS if provided—each flag indicates litigation or delay risk; (2) From the excerpts, interpret stated issues (e.g. cost, contamination, regulatory) as risk factors; (3) Frame your answer in terms a lender cares about: litigation risk, permitting delay risk, cost overrun risk, reputational risk."
)

# Injected into the prompt when the user asks about financial/risk; gives the model a clear framework
_RISK_INTERPRETATION_BLOCK = """=== HOW TO INTERPRET RISKS (for the lender) ===
For infrastructure lenders, NEPA-related financial risks are usually:
• Litigation risk — issues that could be challenged in court (e.g. deferred mitigation, missing EJ or no-action analysis) and lead to delay or remedy costs.
• Delay risk — permitting or NEPA process delays that push back project timeline and revenue.
• Cost / scope risk — from the documents: cost increases, remediation, regulatory conditions, or reliance on future studies.
• Reputational risk — high-profile environmental or equity concerns that affect financing or stakeholders.

Use the COMPLIANCE FLAGS (if any) and the DOCUMENT EXCERPTS to identify concrete items that map to these. Do not say "no financial details" — interpret what is there (costs, contamination, regulatory language, deferred decisions) as risk factors and list them clearly."""

_SYSTEM_GLOBAL = (
    "You are VERA, an environmental compliance assistant for infrastructure lenders. "
    "Answer based ONLY on the excerpts from the listed projects. "
    "If the answer is not found, say so. Do not hallucinate. Be concise."
)


def _question_asks_risk(question: str) -> bool:
    """True if the user is asking about financial, litigation, or delay risk."""
    q = question.lower().strip()
    return any(
        phrase in q
        for phrase in (
            "financial risk",
            "financial risks",
            "litigation risk",
            "delay risk",
            "risks can be interpreted",
            "what risks",
            "what are the risks",
            "risk does this",
            "risk pose",
        )
    )


def _build_project_prompt(
    project_card: str,
    document_list: str,
    excerpts: str,
    question: str,
    flags_blob: str = "",
    similar_blob: str = "",
    risk_block: str = "",
) -> str:
    sections = [
        "=== PROJECT CARD ===",
        project_card,
        "",
        "=== DOCUMENTS IN THIS PROJECT ===",
        document_list,
    ]
    if flags_blob:
        sections.extend(["", "=== COMPLIANCE FLAGS (from prior scan) ===", flags_blob])
    if risk_block:
        sections.extend(["", risk_block])
    if similar_blob:
        sections.extend(["", "=== OTHER PROJECTS YOU MAY COMPARE (titles only; no full text here) ===", similar_blob])
    sections.extend(["", "=== DOCUMENT EXCERPTS ===", excerpts, "", "---", f"Question: {question}"])
    return "\n".join(sections)


def _build_global_prompt(
    items: list[dict[str, str]],  # [{"project_id", "title", "chunk"}, ...]
    question: str,
) -> str:
    excerpts = "\n\n---\n\n".join(
        f"[Project: {it['title']} (ID: {it['project_id']})]\n{it['chunk'].strip()}"
        for it in items
    )
    return (
        f"The following excerpts are from multiple NEPA environmental review projects.\n\n"
        f"{excerpts}\n\n"
        f"---\n"
        f"Question: {question}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _question_asks_similar(question: str) -> bool:
    """True if the user is asking for similar or comparable projects."""
    q = question.lower().strip()
    return any(
        phrase in q
        for phrase in (
            "similar project",
            "similar projects",
            "other projects",
            "compare to",
            "compare with",
            "others like",
            "like this",
            "same type",
        )
    )


async def answer_project_question(
    project_id: str,
    question: str,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """
    Q&A scoped to one project.

    Injects: project card, document list, stored compliance flags (for risk questions),
    and when the user asks for similar projects, a short list of comparable project titles.
    Uses top 8 chunks with a total character cap for excerpts.
    """
    project_row = conn.execute(
        "SELECT title, process_type, agency, state FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    title = project_row["title"] if project_row else project_id

    project_card = _get_project_card(conn, project_id)
    document_list = _get_document_list(conn, project_id)
    flags = _get_project_flags(conn, project_id)
    flags_blob = ""
    if flags:
        lines = []
        for f in flags:
            src = f.get("source_document_names") or []
            src_str = f" (from: {', '.join(src)})" if src else ""
            lines.append(f"  - [{f['severity']}] {f['flag_type']}: {f['title'] or f['flag_type']}{src_str}")
        flags_blob = "\n".join(lines)

    similar_blob = ""
    if _question_asks_similar(question):
        similar = _get_similar_projects(conn, project_id, limit=5)
        if similar:
            similar_blob = "\n".join(f"  - {s['title']} (id: {s['project_id']})" for s in similar)
        else:
            similar_blob = "  (No other projects with same process type and agency in the database.)"

    doc_rows = conn.execute(
        """SELECT id, title, filename, text_content
           FROM documents WHERE project_id = ? AND text_content IS NOT NULL""",
        (project_id,),
    ).fetchall()

    # Build (chunk, source_label) per document so we can say which excerpt is from which file
    def _doc_label(r: Any) -> str:
        return r["filename"] or r["title"] or r["id"] or "unknown"

    chunks_with_src: list[tuple[str, str]] = []
    for r in doc_rows:
        text = r["text_content"] or ""
        if not text.strip():
            continue
        label = _doc_label(r)
        for c in _chunk_text(text):
            chunks_with_src.append((c, label))

    risk_block = _RISK_INTERPRETATION_BLOCK if _question_asks_risk(question) else ""

    if not chunks_with_src:
        # No document text
        excerpts_text = "(No full document text available for this project.)"
        prompt = _build_project_prompt(
            project_card, document_list, excerpts_text, question,
            flags_blob=flags_blob, similar_blob=similar_blob, risk_block=risk_block,
        )
        try:
            answer = await llm.generate(prompt, system=_SYSTEM)
        except (httpx.ConnectError, httpx.TimeoutException):
            return {"answer": _OLLAMA_UNAVAILABLE, "sources": [], "project_title": title}
        return {
            "answer": answer.strip() or "No answer could be generated.",
            "sources": [],
            "project_title": title,
        }

    # If the question mentions a specific document (e.g. filename), boost chunks from that document
    q_lower = question.lower()
    doc_labels = list({label for _, label in chunks_with_src})
    pinned: list[tuple[str, str]] = []
    for label in doc_labels:
        if label.lower() in q_lower or any(
            len(part) > 4 and part.lower() in q_lower
            for part in re.split(r"[\s_\-./]+", label)
        ):
            doc_chunks = [(c, l) for c, l in chunks_with_src if l == label]
            if doc_chunks:
                chunks_only_doc = [c for c, _ in doc_chunks]
                top_doc = _score_chunks(question, chunks_only_doc, top_n=3)
                for _, chunk in top_doc:
                    pinned.append((chunk, label))
            break

    chunks_only = [c for c, _ in chunks_with_src]
    top = _score_chunks(question, chunks_only, top_n=_TOP_CHUNKS)
    top_with_label = list(pinned)
    seen_chunks: set[str] = {c for c, _ in pinned}
    for _, chunk in top:
        if chunk in seen_chunks:
            continue
        seen_chunks.add(chunk)
        label = next((l for c, l in chunks_with_src if c == chunk), "unknown")
        top_with_label.append((chunk, label))
        if len(top_with_label) >= _TOP_CHUNKS:
            break

    top_chunks: list[tuple[str, str]] = []
    total_chars = 0
    for chunk, label in top_with_label:
        if total_chars + len(chunk) > _MAX_EXCERPT_CHARS:
            take = _MAX_EXCERPT_CHARS - total_chars
            if take > 200:
                top_chunks.append((chunk[:take] + "…", label))
            total_chars = _MAX_EXCERPT_CHARS
            break
        top_chunks.append((chunk, label))
        total_chars += len(chunk)

    excerpts = "\n\n---\n\n".join(
        f"[Excerpt {i + 1} — from: {label}]\n{c.strip()}"
        for i, (c, label) in enumerate(top_chunks)
    )

    prompt = _build_project_prompt(
        project_card,
        document_list,
        excerpts,
        question,
        flags_blob=flags_blob,
        similar_blob=similar_blob,
        risk_block=risk_block,
    )
    try:
        answer = await llm.generate(prompt, system=_SYSTEM)
    except (httpx.ConnectError, httpx.TimeoutException):
        return {
            "answer": _OLLAMA_UNAVAILABLE,
            "sources": [],
            "project_title": title,
        }

    return {
        "answer": answer.strip() or "No answer could be generated.",
        "sources": [c[:300] + ("…" if len(c) > 300 else "") for c, _ in top_chunks],
        "project_title": title,
    }


async def answer_global_question(
    question: str,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """
    Cross-project Q&A.

    Steps:
    1. SQL LIKE search on text_content across main documents using question keywords.
    2. Take the top 5 matching projects (by number of keyword hits in text).
    3. Pull the best single chunk per project via TF-IDF.
    4. Send to Ollama with per-project context.
    5. Return {answer, sources: [{project_id, title}]}.
    """
    keywords = [w for w in _tokenize(question) if len(w) > 3]
    if not keywords:
        return {
            "answer": "Please provide a more specific question.",
            "sources": [],
        }

    # Build a LIKE filter for each keyword
    like_clauses = " OR ".join("d.text_content LIKE ?" for _ in keywords)
    like_params = [f"%{kw}%" for kw in keywords]

    rows = conn.execute(
        f"""SELECT p.id AS project_id, p.title, d.text_content
            FROM documents d
            JOIN projects p ON p.id = d.project_id
            WHERE d.is_main = 1 AND d.text_content IS NOT NULL
              AND ({like_clauses})
            LIMIT 50""",
        like_params,
    ).fetchall()

    if not rows:
        return {
            "answer": "No relevant projects were found for this question.",
            "sources": [],
        }

    # Score each row's text and deduplicate to one best chunk per project
    seen_projects: dict[str, dict[str, str]] = {}  # project_id -> best item
    project_scores: dict[str, float] = {}

    for row in rows:
        pid = row["project_id"]
        chunks = _chunk_text(row["text_content"] or "")
        if not chunks:
            continue
        top = _score_chunks(question, chunks, top_n=1)
        score, best_chunk = top[0]
        if pid not in project_scores or score > project_scores[pid]:
            project_scores[pid] = score
            seen_projects[pid] = {
                "project_id": pid,
                "title": row["title"],
                "chunk": best_chunk,
            }

    # Sort by score, take top 5
    top5 = sorted(seen_projects.values(), key=lambda x: project_scores[x["project_id"]], reverse=True)[:5]

    if not top5:
        return {
            "answer": "No relevant content found across projects.",
            "sources": [],
        }

    prompt = _build_global_prompt(top5, question)
    sources = [{"project_id": it["project_id"], "title": it["title"]} for it in top5]
    try:
        answer = await llm.generate(prompt, system=_SYSTEM_GLOBAL)
    except (httpx.ConnectError, httpx.TimeoutException):
        return {"answer": _OLLAMA_UNAVAILABLE, "sources": sources}

    return {
        "answer": answer.strip() or "No answer could be generated.",
        "sources": sources,
    }
