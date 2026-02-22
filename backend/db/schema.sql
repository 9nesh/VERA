-- VERA database schema
-- All additions from the rebuild plan are integrated directly into CREATE TABLE
-- statements so the schema is authoritative for fresh installs.
-- (No ALTER TABLE needed; legacy upgrades handled separately.)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- projects
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS projects (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    process_type         TEXT NOT NULL CHECK (process_type IN ('CE', 'EA', 'EIS')),
    agency               TEXT,
    state                TEXT,
    county               TEXT,
    lead_office          TEXT,
    register_date        TEXT,   -- ISO-8601 date string
    status               TEXT,
    project_url          TEXT,
    raw_json             TEXT,   -- full source record for debugging

    -- Solana attestation columns (new)
    solana_tx_signature  TEXT,
    solana_attested_at   TEXT,   -- ISO-8601 datetime of attestation
    solana_slot          INTEGER,

    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_projects_process_type ON projects (process_type);
CREATE INDEX IF NOT EXISTS idx_projects_state        ON projects (state);
CREATE INDEX IF NOT EXISTS idx_projects_agency       ON projects (agency);

-- ---------------------------------------------------------------------------
-- documents
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    doc_type     TEXT,           -- FEIS, DEIS, EA, ROD, FONSI, CE, etc.
    title        TEXT,
    filename     TEXT,
    file_url     TEXT,
    file_size    INTEGER,        -- bytes
    page_count   INTEGER,
    is_main      INTEGER NOT NULL DEFAULT 0 CHECK (is_main IN (0, 1)),
    ce_category  TEXT,           -- CE-specific category code (new); NULL for EA/EIS
    text_content TEXT,           -- extracted plain text
    sha256       TEXT,           -- hex digest of text_content for attestation

    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_documents_project_id ON documents (project_id);
CREATE INDEX IF NOT EXISTS idx_documents_is_main    ON documents (is_main);      -- new
CREATE INDEX IF NOT EXISTS idx_documents_doc_type   ON documents (doc_type);     -- new

-- ---------------------------------------------------------------------------
-- milestones
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS milestones (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,  -- e.g. "Notice of Intent", "Public Comment Period", "ROD"
    event_date   TEXT,           -- ISO-8601 date string; nullable (some records lack dates)
    description  TEXT,
    source_doc   TEXT            -- document id that triggered this milestone
);

CREATE INDEX IF NOT EXISTS idx_milestones_project_id ON milestones (project_id);
CREATE INDEX IF NOT EXISTS idx_milestones_date       ON milestones (event_date);  -- new

-- ---------------------------------------------------------------------------
-- flags  (compliance signal hits)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS flags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    document_id  TEXT REFERENCES documents (id) ON DELETE SET NULL,
    flag_type    TEXT NOT NULL,  -- e.g. "ej_absent", "deferred_mitigation"
    severity     TEXT NOT NULL CHECK (severity IN ('high', 'medium', 'low', 'info')),
    title        TEXT,
    description  TEXT,
    excerpt      TEXT,           -- verbatim text that triggered the flag
    char_offset  INTEGER,        -- character position of excerpt in document text
    scanned_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_flags_project_id ON flags (project_id);
CREATE INDEX IF NOT EXISTS idx_flags_flag_type  ON flags (flag_type);
CREATE INDEX IF NOT EXISTS idx_flags_severity   ON flags (severity);

-- ---------------------------------------------------------------------------
-- llm_audit  (audit trail for LLM calls)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS llm_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT REFERENCES projects (id) ON DELETE SET NULL,
    document_id  TEXT REFERENCES documents (id) ON DELETE SET NULL,
    prompt_hash  TEXT,           -- sha256 of the prompt text
    model        TEXT,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    duration_ms  INTEGER,
    response     TEXT,
    called_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_audit_project_id ON llm_audit (project_id);

-- ---------------------------------------------------------------------------
-- Trigger: keep projects.updated_at current
-- ---------------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_projects_updated_at
AFTER UPDATE ON projects
FOR EACH ROW
BEGIN
    UPDATE projects
    SET    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE  id = OLD.id;
END;
