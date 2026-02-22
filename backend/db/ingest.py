"""
NEPATEC JSONL ingest: discover CE/EA/EIS files, insert projects, documents, milestones.

Run from repo root: python -m backend.db.ingest

Uses process-type aware is_main (EIS: prefer FEIS/DEIS), ce_category for CE,
filename fallback for doc_type/milestones, one transaction per project with rollback on error.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

# Run from repo root so backend package is importable
if __name__ == "__main__":
    _repo = Path(__file__).resolve().parent.parent.parent
    if str(_repo) not in sys.path:
        sys.path.insert(0, str(_repo))

from backend.config import DB_PATH, NEPATEC_DATA_DIR


# ---------------------------------------------------------------------------
# Helpers: unwrap NEPATEC { "value": ... } and single-element lists
# ---------------------------------------------------------------------------

def _val(x):
    if x is None:
        return None
    if isinstance(x, dict) and "value" in x:
        x = x["value"]
    if isinstance(x, list):
        if len(x) == 0:
            return None
        if len(x) == 1:
            return x[0]
        return x  # keep list for multi-value fields
    return x


def _doc_id(doc: dict) -> str | None:
    """Unique document id from metadata."""
    meta = doc.get("metadata") or {}
    dm = meta.get("document_metadata") or {}
    fm = meta.get("file_metadata") or {}
    raw = _val(dm.get("document_ID")) or _val(fm.get("file_ID"))
    return str(raw).strip() if raw else None


def _doc_type(doc: dict) -> str | None:
    """document_type from document_metadata, or None if blank."""
    meta = doc.get("metadata") or {}
    dm = meta.get("document_metadata") or {}
    raw = _val(dm.get("document_type"))
    if raw and str(raw).strip():
        return str(raw).strip()
    return None


def _filename(doc: dict) -> str | None:
    """File name from file_metadata."""
    meta = doc.get("metadata") or {}
    fm = meta.get("file_metadata") or {}
    raw = _val(fm.get("file_name"))
    return str(raw).strip() if raw else None


def _is_main_flag(doc: dict) -> bool:
    """True if main_document is YES."""
    meta = doc.get("metadata") or {}
    fm = meta.get("file_metadata") or {}
    raw = _val(fm.get("main_document"))
    return str(raw).upper().strip() == "YES"


# Filename substring -> (doc_type, milestone_event_type) for blank document_type
_FILENAME_DOC_TYPE_MILESTONE = [
    ("ROD", "ROD", "Record of Decision"),
    ("DEIS", "DEIS", "Draft Environmental Impact Statement"),
    ("FEIS", "FEIS", "Final Environmental Impact Statement"),
    ("Draft EIS", "DEIS", "Draft Environmental Impact Statement"),
    ("Final EIS", "FEIS", "Final Environmental Impact Statement"),
    ("EIS.pdf", "EIS", "Environmental Impact Statement"),
    ("NOI", "NOI", "Notice of Intent"),
    ("FONSI", "FONSI", "Finding of No Significant Impact"),
    ("EA.pdf", "EA", "Environmental Assessment"),
    ("CE.pdf", "CE", "Categorical Exclusion"),
]


def _doc_type_and_milestone_from_filename(filename: str | None) -> tuple[str | None, str | None]:
    """When document_type is blank, infer doc_type and milestone event from filename."""
    if not filename:
        return None, None
    u = filename.upper()
    for sub, doc_type, event_type in _FILENAME_DOC_TYPE_MILESTONE:
        if sub.upper() in u:
            return doc_type, event_type
    return None, None


def _pick_main(docs: list[dict], process_type: str) -> set[str]:
    """Set of document IDs that are the main analysis document(s)."""
    if process_type != "EIS":
        return {did for d in docs if (did := _doc_id(d)) and _is_main_flag(d)}
    # EIS: prefer FEIS/DEIS/EIS doc types; fall back to main_document flag
    main_ids = set()
    for d in docs:
        did = _doc_id(d)
        if not did:
            continue
        dt = _doc_type(d)
        if not dt:
            fn = _filename(d)
            dt, _ = _doc_type_and_milestone_from_filename(fn)
        if dt in ("FEIS", "DEIS", "EIS"):
            main_ids.add(did)
    if main_ids:
        return main_ids
    return {did for d in docs if (did := _doc_id(d)) and _is_main_flag(d)}


def _resolve_doc_type(doc: dict) -> str | None:
    """document_type from metadata or filename fallback."""
    dt = _doc_type(doc)
    if dt:
        return dt
    fn = _filename(doc)
    dt, _ = _doc_type_and_milestone_from_filename(fn)
    return dt


def _milestone_event_type(doc: dict) -> str | None:
    """Event type for milestones: from document_type or filename."""
    dt = _doc_type(doc)
    if dt:
        # Map common doc types to display names
        names = {
            "ROD": "Record of Decision",
            "DEIS": "Draft Environmental Impact Statement",
            "FEIS": "Final Environmental Impact Statement",
            "EIS": "Environmental Impact Statement",
            "NOI": "Notice of Intent",
            "FONSI": "Finding of No Significant Impact",
            "EA": "Environmental Assessment",
            "CE": "Categorical Exclusion",
        }
        return names.get(dt, dt)
    _, event = _doc_type_and_milestone_from_filename(_filename(doc))
    return event


def _text_content(doc: dict) -> str:
    """Concatenate all page text."""
    pages = doc.get("pages") or []
    parts = []
    for p in pages:
        if isinstance(p, dict) and "page text" in p:
            parts.append(p["page text"] or "")
    return "\n".join(parts)


def _sha256(text: str) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Process type normalization
# ---------------------------------------------------------------------------

def _normalize_process_type(raw: str) -> str:
    """Map NEPATEC process_type to CE | EA | EIS."""
    if not raw:
        return "EA"
    r = str(raw).strip().upper()
    if "CATEGORICAL" in r or r == "CE":
        return "CE"
    if "EIS" in r or "ENVIRONMENTAL IMPACT" in r:
        return "EIS"
    if "EA" in r or "ENVIRONMENTAL ASSESSMENT" in r:
        return "EA"
    return "EA"


def _parse_state_county(location) -> tuple[str | None, str | None]:
    """Parse state (and optionally county) from location string.
    e.g. 'Coldfoot, Yukon-Koyukuk Census Area, AK (Lat/Lon: ...)' -> (AK, Yukon-Koyukuk Census Area)
    """
    if not location:
        return None, None
    s = str(location).strip()
    # Drop parenthetical (Lat/Lon: ...)
    if "(" in s:
        s = s[: s.index("(")].strip()
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return None, None
    # Last part is often state (2-letter or name)
    state = parts[-1] if parts else None
    county = parts[-2] if len(parts) >= 2 else None
    return state, county


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_jsonl(data_dir: Path) -> list[tuple[str, Path]]:
    """Discover all JSONL files under data_dir/CE, data_dir/EA, data_dir/EIS.
    Returns [(process_type, path), ...] with process_type in CE, EA, EIS.
    """
    out = []
    for sub in ("CE", "EA", "EIS"):
        folder = data_dir / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.jsonl")):
            out.append((sub, path))
    return out


# ---------------------------------------------------------------------------
# Ingest one record (one line = one project + documents + milestones)
# ---------------------------------------------------------------------------

def ingest_record(conn: sqlite3.Connection, record: dict, process_type: str) -> tuple[int, int]:
    """Insert one NEPATEC record into projects, documents, milestones.
    Returns (num_documents, num_milestones). Raises on constraint/DB error.
    """
    project = record.get("project") or {}
    process = record.get("process") or {}
    documents = record.get("documents") or []

    pt_raw = _val(process.get("process_type"))
    pt = _normalize_process_type(pt_raw)

    pid = _val(project.get("project_ID"))
    if not pid:
        raise ValueError("record has no project_ID")
    pid = str(pid).strip()

    title = _val(project.get("project_title"))
    if not title:
        title = "(Untitled project)"
    title = str(title).strip()

    location = _val(project.get("location"))
    state, county = _parse_state_county(location)
    agency = _val(process.get("lead_agency"))
    if isinstance(agency, list):
        agency = ", ".join(str(a) for a in agency if a) or None
    else:
        agency = str(agency).strip() if agency else None

    raw_json = json.dumps(record, ensure_ascii=False)

    conn.execute(
        """INSERT INTO projects (
            id, title, process_type, agency, state, county, lead_office,
            register_date, status, project_url, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pid,
            title,
            pt,
            agency,
            state,
            county,
            None,  # lead_office
            None,  # register_date
            None,  # status
            None,  # project_url
            raw_json,
        ),
    )

    main_ids = _pick_main(documents, pt)
    num_docs = 0
    num_milestones = 0

    for doc in documents:
        did = _doc_id(doc)
        if not did:
            continue

        doc_type = _resolve_doc_type(doc)
        meta = doc.get("metadata") or {}
        dm = meta.get("document_metadata") or {}
        fm = meta.get("file_metadata") or {}
        title_doc = _val(dm.get("document_title")) or _val(fm.get("section_or_volume_title"))
        if isinstance(title_doc, list):
            title_doc = title_doc[0] if title_doc else None
        title_doc = str(title_doc).strip() if title_doc else None

        filename = _filename(doc)
        file_size = None
        page_count = _val(fm.get("total_pages"))
        if page_count is not None:
            try:
                page_count = int(page_count)
            except (TypeError, ValueError):
                page_count = None

        is_main = 1 if did in main_ids else 0

        ce_category = None
        if pt == "CE":
            ce_val = _val(dm.get("ce_category"))
            if isinstance(ce_val, list):
                ce_category = ", ".join(str(x) for x in ce_val if x) or None
            else:
                ce_category = str(ce_val).strip() if ce_val else None

        text = _text_content(doc)
        sha = _sha256(text)

        conn.execute(
            """INSERT INTO documents (
                id, project_id, doc_type, title, filename, file_url, file_size, page_count,
                is_main, ce_category, text_content, sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                did,
                pid,
                doc_type,
                title_doc,
                filename,
                None,  # file_url
                file_size,
                page_count,
                is_main,
                ce_category,
                text,
                sha,
            ),
        )
        num_docs += 1

        event_type = _milestone_event_type(doc)
        if event_type:
            conn.execute(
                """INSERT INTO milestones (project_id, event_type, event_date, description, source_doc)
                 VALUES (?, ?, ?, ?, ?)""",
                (pid, event_type, None, None, did),
            )
            num_milestones += 1

    return num_docs, num_milestones


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_ingest(
    data_dir: Path | None = None,
    db_path: Path | None = None,
    limit: int | None = None,
) -> None:
    data_dir = data_dir or NEPATEC_DATA_DIR
    db_path = db_path or DB_PATH

    if not data_dir.is_dir():
        print(f"Data directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    discovered = discover_jsonl(data_dir)
    if not discovered:
        print(f"No *.jsonl files under {data_dir}/CE, {data_dir}/EA, {data_dir}/EIS", file=sys.stderr)
        sys.exit(0)

    # Ensure DB and schema exist
    if not db_path.exists():
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            conn0 = sqlite3.connect(str(db_path))
            conn0.executescript(schema_path.read_text())
            conn0.close()
        else:
            print("schema.sql not found; create the DB and apply schema first.", file=sys.stderr)
            sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    stats = {"CE": {"projects": 0, "documents": 0, "errors": []}, "EA": {"projects": 0, "documents": 0, "errors": []}, "EIS": {"projects": 0, "documents": 0, "errors": []}}

    total_projects = 0
    total_documents = 0
    total_milestones = 0

    for process_type, path in discovered:
        print(f"Reading {path} ...", flush=True)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    if limit is not None and total_projects >= limit:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as e:
                        stats[process_type]["errors"].append(f"line {line_num}: {e}")
                        continue

                    pt_raw = _val((record.get("process") or {}).get("process_type"))
                    pt = _normalize_process_type(pt_raw)

                    try:
                        conn.execute("BEGIN")
                        nd, nm = ingest_record(conn, record, pt)
                        conn.commit()
                        stats[process_type]["projects"] += 1
                        stats[process_type]["documents"] += nd
                        total_projects += 1
                        total_documents += nd
                        total_milestones += nm
                    except Exception as e:
                        conn.rollback()
                        pid = _val((record.get("project") or {}).get("project_ID")) or "(unknown)"
                        stats[process_type]["errors"].append(f"project {pid}: {e}")

        except OSError as e:
            print(f"  Error opening file: {e}", file=sys.stderr)
            continue

    conn.close()

    print()
    print("--- Ingest summary ---")
    for pt in ("CE", "EA", "EIS"):
        s = stats[pt]
        print(f"  {pt}: {s['projects']} projects, {s['documents']} documents", end="")
        if s["errors"]:
            print(f" — {len(s['errors'])} error(s)")
            for err in s["errors"][:10]:
                print(f"    - {err}")
            if len(s["errors"]) > 10:
                print(f"    ... and {len(s['errors']) - 10} more")
        else:
            print()
    print(f"  Total: {total_projects} projects, {total_documents} documents, {total_milestones} milestones")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Ingest NEPATEC JSONL into VERA DB.")
    p.add_argument("--limit", type=int, default=None, help="Max projects to ingest (for testing).")
    args = p.parse_args()
    run_ingest(limit=args.limit)
