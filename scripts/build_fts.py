"""
One-time migration: create and populate the projects_fts FTS5 virtual table
on an existing nepa.db that was ingested before FTS5 was added to schema.sql.

Run from repo root: python scripts/build_fts.py

Safe to run multiple times — uses CREATE VIRTUAL TABLE IF NOT EXISTS and
a full rebuild regardless, so it is idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from backend.config import DB_PATH
import sqlite3


def build_fts(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        # Create FTS5 table (no-op if already exists)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS projects_fts USING fts5(
                id    UNINDEXED,
                title,
                agency,
                state,
                content='projects',
                content_rowid='rowid'
            )
        """)

        # Create update triggers (no-op if already exist)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_projects_fts_insert
            AFTER INSERT ON projects BEGIN
                INSERT INTO projects_fts(rowid, id, title, agency, state)
                VALUES (new.rowid, new.id, new.title, new.agency, new.state);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_projects_fts_delete
            AFTER DELETE ON projects BEGIN
                INSERT INTO projects_fts(projects_fts, rowid, id, title, agency, state)
                VALUES ('delete', old.rowid, old.id, old.title, old.agency, old.state);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_projects_fts_update
            AFTER UPDATE ON projects BEGIN
                INSERT INTO projects_fts(projects_fts, rowid, id, title, agency, state)
                VALUES ('delete', old.rowid, old.id, old.title, old.agency, old.state);
                INSERT INTO projects_fts(rowid, id, title, agency, state)
                VALUES (new.rowid, new.id, new.title, new.agency, new.state);
            END
        """)

        # Full rebuild from the projects table
        print("Rebuilding FTS5 index from projects table...")
        conn.execute("INSERT INTO projects_fts(projects_fts) VALUES('rebuild')")
        conn.commit()

        count = conn.execute("SELECT COUNT(*) FROM projects_fts").fetchone()[0]
        print(f"Done. FTS5 index contains {count:,} rows.")

        # Quick smoke test
        row = conn.execute(
            "SELECT id FROM projects_fts WHERE projects_fts MATCH '\"nuclear\"' LIMIT 1"
        ).fetchone()
        print(f"Smoke test (search 'nuclear'): {'found result' if row else 'no results (ok)'}")

    finally:
        conn.close()


if __name__ == "__main__":
    build_fts()
