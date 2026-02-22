# VERA — Verified Environmental Review & Attestation

**Tagline:** Tamper-proof NEPA compliance risk for lenders, powered by civic AI and Solana.

---

## Inspiration

Lenders and developers need to know if a project’s environmental review is **litigation-ready**. NEPA (National Environmental Policy Act) documents—Categorical Exclusions (CE), Environmental Assessments (EA), and Environmental Impact Statements (EIS)—are long, dense, and full of legal risk. Missing environmental justice analysis, deferred mitigation, or thin “no action” alternatives are exactly the kinds of gaps that get projects challenged in court.

We built **VERA** so that:

1. **Lenders** can search 60+ agencies’ NEPA projects, run a compliance scan, and see risk flags in seconds—without reading 500-page PDFs.
2. **Anyone** can **attest** that scan result on **Solana**: a timestamped, tamper-proof record that a third party can verify without trusting our backend.
3. **Analysts** get a **NEPA Observatory** (dashboard) and a **Permitting Stuckness Radar** (FAST-41) to see where projects pile up and where reviews are thin.

---

## What We Built

### 1. **Data pipeline**

- **NEPATEC2.0** (PNNL PermitAI): we ingest CE, EA, and EIS JSONL from HuggingFace (54K+ CE, 3K+ EA, 4K+ EIS projects).
- **Process-type aware ingest**: for EIS we pick the main analysis document (FEIS/DEIS) correctly; for CE we store `ce_category`; we infer doc types and milestones from filenames when metadata is blank.
- **SQLite** with FTS5 full-text search, WAL, and indexes so search and analytics stay fast.

### 2. **Compliance signal scanner**

- **8 flag types** aligned with real litigation risk:
  - **Deferred mitigation** — mitigation pushed to “final design” or “future phases” (we explicitly avoid false positives like “prior to construction”).
  - **Future studies reliance** — approval contingent on studies not yet done (we exclude required “long-term monitoring plan” language).
  - **EJ absent / EJ thin coverage** — missing or shallow environmental justice analysis.
  - **No-action absent / no-action thin** — missing or weak no-action alternative discussion.
  - **Cumulative impacts thin** — minimal cumulative impacts analysis.
  - **Tribal interests** — tribal consultation mentioned (informational).
- **Process-type gating**: CE documents only get CE-relevant flags; EA/EIS get the full set (EJ, no-action, cumulative). This removes false positives from CE templates that don’t have those sections.
- **Synthetic test suite** (`tests/test_signals.py`): should-fire and should-not-fire tests per flag so we don’t regress.

### 3. **Solana attestation layer**

- After a scan, users can **Attest on Solana**.
- We build a payload: project id, timestamp, flag counts by severity, and SHA-256 of flags + document hashes.
- We write a **compact memo** (payload hash + summary) to the **SPL Memo program** on **Solana devnet**—no custom program, just a single instruction.
- We store `solana_tx_signature`, `solana_attested_at`, and `solana_slot` on the project.
- **Verify** endpoint: fetch the on-chain memo and confirm the hash matches. **Lenders can verify compliance status in seconds without trusting us.**

### 4. **API & intelligence**

- **FastAPI** backend: project search (FTS5 + filters), project detail, **scan**, **flags** (with optional LLM explanations), **attest**, **verify**, project-scoped and global **chat**.
- **Retrieval-augmented Q&A**: project chat uses the project’s documents + stored flags; global chat uses keyword search across projects. Optional **Ollama** (e.g. qwen2.5:3b) for explanations and chat—all on-device.
- **Dashboard (NEPA Observatory)**: stats, by-state and by-agency breakdowns, cached for performance.
- **Radar (FAST-41)**: load FAST-41 CSV, compute “stuckness” scores, map view, and optional LLM narration for why projects are stuck.

### 5. **Frontend**

- **Single-page app** (Alpine.js + Tailwind): search by keyword and process type → open project → run **Scan** → see flags by severity → **Attest on Solana** → see tx signature and link to Solana Explorer (devnet).
- **Chat**: project-specific or cross-project questions with retrieval-augmented answers.
- **Observatory** and **Stuckness Radar** as separate pages with clear navigation.
- No innerHTML of API data (XSS-safe); toasts for errors; attestation badge and “View on Solana Explorer” when attested.

---

## How We Built It

- **Backend:** Python 3, FastAPI, SQLite, `solders` + `solana` for Solana.
- **Data:** NEPATEC2.0 JSONL (CE/EA/EIS) from HuggingFace; FAST-41 CSV for the radar.
- **Config:** Single `backend/config.py` (DB path, Ollama URL, Solana RPC, keypair path, EA/EIS-only flags).
- **Signals:** Regex-based detectors with process-type gating and curated exclusions to reduce false positives; results stored in `flags` with excerpt and char offset.
- **Solana:** SPL Memo on devnet; keypair from file; attestation payload hashed with SHA-256; memo ≤566 bytes (truncate to summary if needed).
- **Tests:** `pytest tests/test_signals.py` for signal behavior.

We followed a **hackathon rebuild plan** that called out: fix EIS main-doc logic, add Solana columns and attest/verify, gate flags by process type, remove known false-positive patterns, add tests, and a clean attestation UI.

---

## Challenges We Ran Into

- **EIS “main” document:** Many EIS projects mark multiple files as “main” (ROD, errata, etc.). We had to prefer FEIS/DEIS as the analysis document and fall back to `main_document` only when needed.
- **False positives in signals:** Phrases like “prior to construction” and “long-term monitoring plan will be developed” were firing inappropriately. We tightened patterns and added explicit exclusions (and tests) so only real risk language is flagged.
- **CE vs EA/EIS:** Running EJ and no-action detectors on CE text caused noise because CE templates don’t have those sections. We introduced `EA_EIS_ONLY_FLAGS` and gated detectors by `process_type`.
- **Solana memo size:** SPL Memo has a 566-byte limit. We hashed the full payload (including doc hashes) but store only a compact summary + hash on-chain so verification still works.

---

## Accomplishments We’re Proud Of

- **End-to-end flow:** Search → open project → scan → see litigation-relevant flags → attest on Solana → verify on-chain. A lender can go from “nuclear” to “this project has 2 high flags and here’s the proof on Solana” in one session.
- **Civic AI that’s auditable:** Pattern-based signals are transparent and testable; optional LLM explanations are retrieval-constrained and logged.
- **Real data:** NEPATEC2.0 (60+ agencies, 60K+ projects) and FAST-41 for permitting radar.
- **No trust required for verification:** The attestation hash is on-chain; anyone can call our verify endpoint or replicate the hash from our API and confirm the record matches.

---

## What We Learned

- NEPA document structure varies a lot by process type and agency; process-type awareness in both ingest and signals is essential.
- Solana’s SPL Memo program is enough for “proof of existence” and integrity without writing a custom program.
- Caching dashboard and radar aggregates (in-memory + optional disk) makes a big difference when the DB lives on a large drive.

---

## What’s Next

- **Mainnet / permissioned attestations:** Optional mainnet or dedicated validator set for production attestations.
- **More signal types:** e.g. climate, water, species; tuned per agency.
- **Embeddings + semantic search:** FAISS/sentence-transformers for “find projects like this one” and richer retrieval for chat.
- **Bulk attest:** Attest many projects in one flow for portfolio due diligence.

---

## Try It Yourself

1. **Setup**
   - Clone the repo, create a venv, install dependencies (FastAPI, httpx, solders, solana, etc.).
   - Add NEPATEC data under `nepatec_data/` (CE/EA/EIS JSONL) and run:  
     `python -m backend.db.ingest`
   - Optional: run Ollama with `qwen2.5:3b` for chat and flag explanations.
   - Optional: add a funded devnet keypair at `devnet-keypair.json` for attestation.

2. **Run**
   - `uvicorn backend.main:app --reload`
   - Open the app in the browser; use “Stuckness Radar” and “Observatory” from the header.

3. **Demo flow**
   - Search e.g. “nuclear” or “Savannah River” → open a project → **Scan** → see flags (e.g. `ej_absent`, `deferred_mitigation`) → **Attest on Solana** → open the Solana Explorer link → use **Verify** to confirm the on-chain memo matches.

---

**VERA** = Verified Environmental Review & Attestation.  
NEPA compliance risk, in plain language, with proof on Solana.
