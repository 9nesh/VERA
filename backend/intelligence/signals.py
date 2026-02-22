"""
NEPA compliance signal scanner for VERA.

Finds patterns in environmental review documents that indicate litigation risk.
Used by lenders for NEPA due diligence. Respects process_type: CE gets only
CE-applicable flags; EA/EIS get all flags (including EJ, no-action, cumulative).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from backend.config import EA_EIS_ONLY_FLAGS


# ---------------------------------------------------------------------------
# Detector definitions: (pattern_list, severity, title, description)
# Excluded patterns per spec: deferred_mitigation no "prior to construction";
# future_studies_reliance no "long-term monitoring plan will be developed"
# ---------------------------------------------------------------------------

def _find_all(pattern: re.Pattern[str], text: str) -> list[tuple[str, int]]:
    """Return [(excerpt, start_offset), ...] for all non-overlapping matches."""
    out = []
    for m in pattern.finditer(text):
        out.append((m.group(0), m.start()))
    return out


def _detect_deferred_mitigation(text: str) -> list[dict[str, Any]]:
    # Mitigation pushed to future. Do NOT match "prior to construction" (false positives).
    patterns = [
        re.compile(
            r"mitigation\s+(?:measures?|plans?|requirements?)\s+"
            r"(?:will\s+be\s+)?(?:developed|determined|finalized)\s+"
            r"(?:prior\s+to\s+final\s+design|in\s+future\s+phases?|during\s+final\s+design)",
            re.I,
        ),
        re.compile(
            r"(?:to\s+be\s+determined|TBD)\s+"
            r"(?:in\s+coordination\s+with\s+)?(?:mitigation|NEPA|subsequent)",
            re.I,
        ),
        re.compile(
            r"mitigation\s+(?:will\s+be\s+)?(?:addressed|developed)\s+"
            r"(?:in\s+a\s+later\s+phase|in\s+subsequent\s+documentation)",
            re.I,
        ),
    ]
    flags_out = []
    for pat in patterns:
        for excerpt, offset in _find_all(pat, text):
            flags_out.append({
                "flag_type": "deferred_mitigation",
                "severity": "high",
                "title": "Deferred mitigation",
                "description": "Mitigation commitments pushed to future decisions.",
                "excerpt": excerpt,
                "char_offset": offset,
            })
    return flags_out


def _detect_future_studies_reliance(text: str) -> list[dict[str, Any]]:
    """Approval contingent on studies not yet done. Do NOT flag 'long-term monitoring plan will be developed'."""
    # Build list of (match, start) then filter out any match that lies inside excluded phrase
    excluded = re.compile(
        r"long[\s-]term\s+monitoring\s+plan\s+will\s+be\s+developed",
        re.I,
    )
    patterns = [
        re.compile(
            r"approval\s+(?:is\s+)?contingent\s+on\s+(?:the\s+)?(?:completion\s+of\s+)?(?:a\s+)?(?:future\s+)?stud(?:y|ies)",
            re.I,
        ),
        re.compile(
            r"pending\s+(?:completion\s+of\s+)?(?:the\s+)?(?:required\s+)?stud(?:y|ies)",
            re.I,
        ),
        re.compile(
            r"stud(?:y|ies)\s+(?:will\s+be|to\s+be)\s+(?:completed|conducted)\s+"
            r"(?:prior\s+to\s+approval|before\s+final\s+decision)",
            re.I,
        ),
        re.compile(
            r"contingent\s+upon\s+(?:the\s+)?(?:results?\s+of\s+)?(?:future\s+)?stud(?:y|ies)",
            re.I,
        ),
    ]
    flags_out = []
    excluded_ranges = [(m.start(), m.end()) for m in excluded.finditer(text)]
    for pat in patterns:
        for m in pat.finditer(text):
            start = m.start()
            if any(a <= start < b for a, b in excluded_ranges):
                continue
            flags_out.append({
                "flag_type": "future_studies_reliance",
                "severity": "high",
                "title": "Future studies reliance",
                "description": "Approval contingent on studies not yet done.",
                "excerpt": m.group(0),
                "char_offset": start,
            })
    return flags_out


def _detect_ej_absent(text: str) -> list[dict[str, Any]]:
    # No environmental justice analysis present (explicit statement of absence).
    patterns = [
        re.compile(
            r"no\s+(?:environmental\s+justice|EJ)\s+(?:analysis|discussion|consideration|review)",
            re.I,
        ),
        re.compile(
            r"environmental\s+justice\s+(?:is\s+)?not\s+(?:addressed|analyzed|discussed|considered)",
            re.I,
        ),
        re.compile(
            r"(?:without|lack\s+of)\s+(?:any\s+)?(?:environmental\s+justice|EJ)\s+(?:analysis|review)",
            re.I,
        ),
    ]
    flags_out = []
    for pat in patterns:
        for excerpt, offset in _find_all(pat, text):
            flags_out.append({
                "flag_type": "ej_absent",
                "severity": "high",
                "title": "Environmental justice absent",
                "description": "No environmental justice analysis present.",
                "excerpt": excerpt,
                "char_offset": offset,
            })
    return flags_out


def _detect_ej_thin_coverage(text: str) -> list[dict[str, Any]]:
    # EJ mentioned but not substantively analyzed.
    patterns = [
        re.compile(
            r"(?:environmental\s+justice|EJ)\s+(?:is\s+)?(?:briefly\s+)?mentioned\s+(?:only\s+)?(?:in\s+passing)?",
            re.I,
        ),
        re.compile(
            r"no\s+(?:substantive|detailed)\s+(?:environmental\s+justice|EJ)\s+(?:analysis|discussion)",
            re.I,
        ),
        re.compile(
            r"(?:environmental\s+justice|EJ)\s+will\s+be\s+considered\s+in\s+future\s+NEPA",
            re.I,
        ),
    ]
    flags_out = []
    for pat in patterns:
        for excerpt, offset in _find_all(pat, text):
            flags_out.append({
                "flag_type": "ej_thin_coverage",
                "severity": "medium",
                "title": "Environmental justice thin coverage",
                "description": "EJ mentioned but not substantively analyzed.",
                "excerpt": excerpt,
                "char_offset": offset,
            })
    return flags_out


def _detect_no_action_absent(text: str) -> list[dict[str, Any]]:
    # No-action alternative missing entirely.
    patterns = [
        re.compile(
            r"no[\s-]action\s+alternative\s+(?:is\s+)?(?:not\s+)?(?:included|addressed|considered|discussed)",
            re.I,
        ),
        re.compile(
            r"absence\s+of\s+(?:the\s+)?no[\s-]action\s+alternative",
            re.I,
        ),
        re.compile(
            r"no\s+action\s+alternative\s+(?:was\s+)?(?:not\s+)(?:evaluated|included)",
            re.I,
        ),
    ]
    flags_out = []
    for pat in patterns:
        for excerpt, offset in _find_all(pat, text):
            flags_out.append({
                "flag_type": "no_action_absent",
                "severity": "high",
                "title": "No-action alternative absent",
                "description": "No-action alternative missing entirely.",
                "excerpt": excerpt,
                "char_offset": offset,
            })
    return flags_out


def _detect_no_action_thin(text: str) -> list[dict[str, Any]]:
    # No-action alternative dismissed without real comparison. Do not match positive phrasing like "was compared".
    patterns = [
        re.compile(
            r"no[\s-]action\s+alternative\s+(?:is\s+)?(?:summarily\s+)?dismissed",
            re.I,
        ),
        re.compile(
            r"no[\s-]action\s+(?:alternative\s+)?(?:was\s+)?(?:not\s+)(?:adequately\s+)?(?:evaluated|compared)",
            re.I,
        ),
        re.compile(
            r"rejected\s+(?:without|with\s+minimal)\s+(?:analysis|comparison)\s+of\s+alternatives",
            re.I,
        ),
    ]
    flags_out = []
    for pat in patterns:
        for excerpt, offset in _find_all(pat, text):
            flags_out.append({
                "flag_type": "no_action_thin",
                "severity": "medium",
                "title": "No-action alternative thin",
                "description": "No-action alternative dismissed without real comparison.",
                "excerpt": excerpt,
                "char_offset": offset,
            })
    return flags_out


def _detect_cumulative_impacts_thin(text: str) -> list[dict[str, Any]]:
    # Cumulative impacts not seriously analyzed.
    patterns = [
        re.compile(
            r"cumulative\s+(?:impacts?|effects?)\s+(?:are\s+)?(?:not\s+)?(?:addressed|analyzed|discussed)",
            re.I,
        ),
        re.compile(
            r"no\s+(?:meaningful|substantive)\s+(?:analysis|discussion)\s+of\s+cumulative\s+impacts?",
            re.I,
        ),
        re.compile(
            r"cumulative\s+impacts?\s+(?:will\s+be|to\s+be)\s+(?:addressed|analyzed)\s+in\s+(?:a\s+)?later",
            re.I,
        ),
    ]
    flags_out = []
    for pat in patterns:
        for excerpt, offset in _find_all(pat, text):
            flags_out.append({
                "flag_type": "cumulative_impacts_thin",
                "severity": "medium",
                "title": "Cumulative impacts thin",
                "description": "Cumulative impacts not seriously analyzed.",
                "excerpt": excerpt,
                "char_offset": offset,
            })
    return flags_out


def _detect_tribal_interests(text: str) -> list[dict[str, Any]]:
    # Tribal consultation found. Severity "info"; description per spec.
    patterns = [
        re.compile(r"tribal\s+(?:consultation|interests|concerns)", re.I),
        re.compile(r"consultation\s+with\s+(?:tribal|Indian\s+)", re.I),
        re.compile(r"tribal\s+(?:nation|government)s?", re.I),
    ]
    flags_out = []
    for pat in patterns:
        for excerpt, offset in _find_all(pat, text):
            flags_out.append({
                "flag_type": "tribal_interests",
                "severity": "info",
                "title": "Tribal interests",
                "description": "Tribal consultation was found — review for completeness.",
                "excerpt": excerpt,
                "char_offset": offset,
            })
    return flags_out


# Map flag_type -> (detector_fn, run_for_CE)
# EA_EIS_ONLY_FLAGS are skipped when process_type == "CE"
_DETECTORS: dict[str, tuple[Any, bool]] = {
    "deferred_mitigation": (_detect_deferred_mitigation, True),
    "future_studies_reliance": (_detect_future_studies_reliance, True),
    "ej_absent": (_detect_ej_absent, False),  # gated by EA_EIS_ONLY
    "ej_thin_coverage": (_detect_ej_thin_coverage, False),
    "no_action_absent": (_detect_no_action_absent, False),
    "no_action_thin": (_detect_no_action_thin, False),
    "cumulative_impacts_thin": (_detect_cumulative_impacts_thin, False),
    "tribal_interests": (_detect_tribal_interests, True),
}


def _should_run_flag(flag_type: str, process_type: str) -> bool:
    if process_type == "CE" and flag_type in EA_EIS_ONLY_FLAGS:
        return False
    return True


def _normalize_excerpt(excerpt: str | None) -> str:
    """Normalize excerpt for deduplication: strip, lowercase, collapse whitespace."""
    if not excerpt:
        return ""
    return re.sub(r"\s+", " ", str(excerpt).strip().lower())


def scan_document(
    project_id: str,
    doc_id: str,
    text: str,
    process_type: str = "EA",
) -> list[dict[str, Any]]:
    """
    Run all applicable flag detectors on raw document text.
    No DB writes. Used by tests and by scan_project (per-doc pass).
    Returns list of flag dicts with keys: flag_type, severity, title, description, excerpt, char_offset.
    Deduplicated by (flag_type, normalized lowercase excerpt); first occurrence kept.
    """
    if not text:
        return []
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for flag_type, (detector, _) in _DETECTORS.items():
        if not _should_run_flag(flag_type, process_type):
            continue
        for flag in detector(text):
            key = (flag_type, _normalize_excerpt(flag.get("excerpt")))
            if key in seen:
                continue
            seen.add(key)
            flag["project_id"] = project_id
            flag["document_id"] = doc_id
            results.append(flag)
    return results


def scan_project(
    project_id: str,
    conn: sqlite3.Connection,
    process_type: str = "EA",
) -> list[dict[str, Any]]:
    """
    Fetch is_main documents for the project, run applicable flags on each,
    insert results into the flags table, return list of flag dicts.
    """
    cur = conn.execute(
        "SELECT id, text_content FROM documents WHERE project_id = ? AND is_main = 1",
        (project_id,),
    )
    rows = cur.fetchall()
    all_flags: list[dict[str, Any]] = []
    for doc_id, text_content in rows:
        text = text_content or ""
        flags = scan_document(project_id, doc_id, text, process_type)
        for f in flags:
            conn.execute(
                """INSERT INTO flags (project_id, document_id, flag_type, severity, title, description, excerpt, char_offset)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    doc_id,
                    f["flag_type"],
                    f["severity"],
                    f.get("title"),
                    f.get("description"),
                    f.get("excerpt"),
                    f.get("char_offset"),
                ),
            )
            # Return dicts without project_id/document_id for API consistency if needed; include them for convenience
            all_flags.append({
                "flag_type": f["flag_type"],
                "severity": f["severity"],
                "title": f.get("title"),
                "description": f.get("description"),
                "excerpt": f.get("excerpt"),
                "char_offset": f.get("char_offset"),
                "document_id": doc_id,
            })
    return all_flags
