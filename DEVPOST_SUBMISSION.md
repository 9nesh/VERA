# VERA — Verified Environmental Review & Attestation

**Tagline:** Environmental due diligence for lenders in seconds.

---

## Inspiration

Every large infrastructure project in the United States — a solar farm, a pipeline, a highway — must clear the **National Environmental Policy Act (NEPA)** before a shovel hits the ground. The process produces three document types:

| Type | Typical length | What it means |
|------|---------------|----------------|
| **CE** — Categorical Exclusion | 2–20 pages | Minimal impact; expedited |
| **EA** — Environmental Assessment | 50–300 pages | Moderate analysis required |
| **EIS** — Environmental Impact Statement | 500–5,000+ pages | Full impact analysis |

Lenders, project developers, and regulators face the same problem: **how do you know if a specific NEPA document is litigation-ready?** Courts overturn projects on narrow, predictable grounds — deferred mitigation commitments, thin environmental justice analysis, missing no-action alternatives, approval contingent on studies not yet completed. Missing any one of these is the kind of gap litigants exploit.

Reading a 2,000-page EIS for every deal is impossible. Trusting a borrower's self-certification is unacceptable. **We built VERA to solve this.**

Three things make VERA different from "AI search on PDFs":

1. **Compliance signals are transparent and testable.** Not a black-box score — eight specific, named risk flags aligned to real litigation patterns, each backed by a regex test suite with should-fire and should-not-fire cases.
2. **Attestations are on-chain.** A lender can verify a project's compliance scan result in seconds without trusting our backend. The cryptographic hash of every scan lives on **Solana devnet** via the SPL Memo program — tamper-proof and timestamped forever.
3. **Semantic search is powered by a dedicated vector database.** We use **Actian VectorAI DB** (with a graceful local-numpy fallback) to store OpenAI `text-embedding-3-small` embeddings for every NEPA document chunk. Users can ask natural-language questions like *"What EIS documents discuss cumulative impacts near tribal lands?"* and get back passage-level answers, not just keyword hits.

---

## What We Built

### 1. Data Pipeline — 61K+ NEPA Projects Ingested

We ingest the **NEPATEC2.0** dataset (PNNL PermitAI) from HuggingFace: 54,668 CEs, 3,083 EAs, and 4,130 EIS projects across 60+ federal agencies, totalling ~6.97 million pages of government text.

**Process-type-aware ingest** is critical. EIS projects often mark 4–5 documents as "main" (RODs, errata, summaries). We prefer `FEIS`/`DEIS` document types as the primary analysis document and fall back to the `main_document` flag only when needed. CE documents get their `ce_category` stored. Milestone events (Record of Decision, Notice of Intent, etc.) are inferred from filename patterns when metadata is blank.

Everything lands in **SQLite with FTS5** full-text search, WAL mode, and indexes — fast enough for live search over millions of pages on a laptop.

**Scale of the dataset:**

$$\text{Total pages} = 366{,}876_{\text{CE}} + 469{,}106_{\text{EA}} + 6{,}131{,}757_{\text{EIS}} = 6{,}967{,}739$$

### 2. Compliance Signal Scanner — 8 Litigation-Risk Flags

Eight signal detectors, each grounded in real NEPA case law and litigation patterns:

| Flag | Severity | What it catches |
|------|----------|-----------------|
| `deferred_mitigation` | High | Mitigation pushed to "final design" or "future phases" |
| `future_studies_reliance` | High | Approval contingent on studies not yet completed |
| `ej_absent` | High | No environmental justice analysis found |
| `ej_thin_coverage` | Medium | EJ section exists but is fewer than ~150 characters |
| `no_action_absent` | High | No no-action alternative discussed |
| `no_action_thin` | Medium | No-action section is thin or boilerplate |
| `cumulative_impacts_thin` | Medium | Cumulative impacts analysis is minimal |
| `tribal_interests` | Info | Tribal consultation mentioned — flag for completeness review |

**Process-type gating** prevents false positives: CE documents are structurally exempt from EJ, no-action, and cumulative-impacts checks because CE templates don't include those sections. Applying those patterns to CE text would produce noise, not signal.

We also explicitly exclude common false-positive phrases:
- `"prior to construction"` — fires on committed mitigation language, not deferred mitigation
- `"long-term monitoring plan will be developed"` — required by regulation, not a red flag

Every signal has a **synthetic test suite** (`tests/test_signals.py`) with should-fire and should-not-fire cases. 18 tests, all pass.

### 3. Semantic Search — Actian VectorAI DB

For natural-language questions across the corpus, we use a dedicated vector database:

- **Embeddings:** OpenAI `text-embedding-3-small` (1,536 dimensions), cost-efficient and fast
- **Primary store:** **Actian VectorAI DB** (Cortex, gRPC on port 50051) — cosine-similarity K-NN search with payload filtering by `process_type`, `agency`, and `state`
- **Fallback:** In-process numpy cosine-similarity store, L2-normalized, persisted to disk — activates automatically when Actian is unreachable (e.g. Apple Silicon without Docker x86)
- **Chunking:** 600-character chunks with 80-character overlap; documents loaded prioritising EIS > EA > CE by analytical depth

The similarity score for a query \( q \) against stored chunk \( c \) is:

$$\text{sim}(q, c) = \frac{q \cdot c}{\|q\| \cdot \|c\|}$$

Since we L2-normalize at insert time, search reduces to a single matrix dot product — fast even at scale.

**RAG Q&A:** The `/api/semantic/ask` endpoint retrieves the top-\(k\) semantically relevant chunks, constructs a retrieval-constrained prompt, and synthesizes an answer with Ollama (qwen2.5:3b, on-device). The system prompt explicitly instructs VERA to answer only from the provided excerpts — no hallucination from pre-training.

### 4. Solana Attestation Layer

After a compliance scan, users can **Attest on Solana**:

1. We build a payload: `project_id`, `timestamp`, flag counts by severity, and a SHA-256 hash of the full flag detail + document hashes.
2. We write a compact memo (≤566 bytes: `payload_hash + project_id + flag_summary`) to the **SPL Memo program** on **Solana devnet** — no custom program required.
3. We store `solana_tx_signature`, `solana_attested_at`, and `solana_slot` on the project record.
4. The **Verify** endpoint fetches the on-chain memo and confirms the hash matches the current API output.

The attestation hash is:

$$h = \text{SHA-256}\!\left(\text{JSON}(\{v, \text{pid}, \text{ts}, \text{flags detail}, \text{doc hashes}\})\right)$$

What goes on-chain is just `h` + a short human-readable summary. Anyone can reproduce `h` from the `/api/projects/{id}/flags` endpoint and confirm it matches the on-chain record — **verification without trusting us.**

### 5. API & Intelligence Layer

**FastAPI** backend with 20+ endpoints:

- `GET /api/projects` — FTS5 keyword search + process-type filter
- `GET /api/projects/{id}` — Project detail with documents and milestones
- `POST /api/projects/{id}/scan` — Run all signal detectors, store flags
- `GET /api/projects/{id}/flags` — Flags with optional LLM explanations (Ollama)
- `POST /api/projects/{id}/attest` — Build + publish Solana attestation
- `GET /api/projects/{id}/verify` — Verify on-chain attestation
- `POST /api/projects/{id}/chat` — Project-scoped RAG Q&A
- `POST /api/chat` — Cross-project RAG Q&A
- `GET /api/dashboard` — NEPA Observatory stats (by state, by agency, process-type breakdown)
- `GET /api/radar` — FAST-41 permitting stuckness scores + optional LLM narration
- `GET /api/semantic/status` — Actian VectorAI DB health + indexed chunk count
- `POST /api/semantic/search` — Semantic similarity search
- `POST /api/semantic/ask` — RAG Q&A via Actian VectorAI DB + Ollama
- `POST /api/semantic/index` — Trigger background re-indexing

### 6. Frontend

A single-page app (Alpine.js + Tailwind CSS):

- **Search** by keyword and process type → paginated project list
- **Project drawer:** scan, flags by severity with excerpts and char offsets, Solana attestation badge, "View on Solana Explorer" link
- **Chat:** project-specific or cross-project questions, retrieval-augmented answers streamed from Ollama
- **NEPA Observatory:** dashboard with stats, by-state and by-agency breakdowns
- **Stuckness Radar:** FAST-41 CSV-powered map with stuckness scores and optional LLM narration of why projects are stuck
- **Semantic Search tab:** natural-language search backed by Actian VectorAI DB

No `innerHTML` of API data (XSS-safe); toast notifications for errors; loading spinners; attestation badge appears automatically after successful attest.

---

## How We Built It

### Architecture

```
NEPATEC JSONL (HuggingFace)
        │
        ▼
   ingest.py  ──────────────────────────────►  nepa.db (SQLite + FTS5)
        │                                              │
        │                              ┌──────────────┼──────────────┐
        │                              ▼              ▼              ▼
        │                         signals.py     embedder.py     radar.py
        │                              │              │              │
        │                              │         OpenAI embed    FAST-41 CSV
        │                              │              │
        │                              │    ┌─────────▼──────────┐
        │                              │    │  Actian VectorAI DB │
        │                              │    │  (numpy fallback)   │
        │                              │    └─────────┬──────────┘
        ▼                              ▼              ▼
    FastAPI ◄──────────────────── routes.py ◄── semantic.py
        │
        ├── solana/attest.py ──► SPL Memo (Solana devnet)
        │
        └── llm/client.py ──► Ollama (qwen2.5:3b, on-device)
```

### Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLite (FTS5, WAL) |
| Signals | Regex detectors, process-type gating, synthetic tests (pytest) |
| Vector DB | **Actian VectorAI DB** (Cortex gRPC) + numpy fallback |
| Embeddings | OpenAI `text-embedding-3-small` (1536d) |
| On-device LLM | Ollama, qwen2.5:3b — all inference local |
| Blockchain | Solana devnet, SPL Memo program (`solders` + `solana` Python) |
| Frontend | Alpine.js, Tailwind CSS |
| Data | NEPATEC2.0 JSONL (PNNL / HuggingFace), FAST-41 CSV |

### Development Process

We followed a structured rebuild plan: fix EIS main-doc logic first (data correctness), then rewrite signals with process-type gating and false-positive exclusions, then add Solana columns and the attest/verify flow, then layer Actian semantic search on top.

Every signal change was paired with a test. We did not move forward until `pytest tests/test_signals.py` was green.

---

## Challenges We Ran Into

### EIS "main" document ambiguity
Many EIS projects mark 4–5 documents as `main_document: YES` — Records of Decision, errata sheets, appendices, and summaries all get this flag. Using any `main_document` document as the analysis target caused scans to run against a 2-page errata sheet instead of the 1,200-page FEIS. We fixed this by preferring `FEIS`/`DEIS` doc types and only falling back to the `main_document` flag when no FEIS/DEIS is present.

### Signal false positives at scale
When you run patterns across 60K+ documents, even a 1% false positive rate is thousands of noise flags. Two specific patterns caused the most pain:

- `"prior to construction"` matched committed mitigation language like *"BMPs will be implemented prior to construction"* — the opposite of deferred mitigation. We added it as an explicit exclusion.
- `"long-term monitoring plan will be developed"` was firing as `future_studies_reliance`. Long-term monitoring plans are required by regulation — their development is a feature, not a flaw.

### CE vs EA/EIS structural differences
CE templates do not contain EJ, no-action, or cumulative impacts sections by design — CEs are approved precisely because those analyses aren't required. Running those detectors on CE text produced meaningless flags constantly. We introduced `EA_EIS_ONLY_FLAGS` in `config.py` and gated every affected detector by `process_type`.

### Solana memo size limit
The SPL Memo program enforces a 566-byte limit per instruction. Our full attestation payload (project ID, timestamps, all flag details, doc hashes) is several kilobytes. Solution: SHA-256 hash the full payload, store the hash + a compact human-readable summary (flag counts by severity) on-chain. The full detail stays in our API. Verification works by recomputing the hash from the API and comparing — you never need to trust the payload we store.

### Actian VectorAI DB on Apple Silicon
The Actian Cortex Docker image is x86-only. On Apple Silicon (M-series Macs), it cannot run natively. Rather than requiring Rosetta or blocking development, we implemented a complete local fallback: in-process numpy cosine similarity with the same interface. The `_actian_available()` function does a fast TCP pre-check (500ms timeout) before attempting gRPC, and caches the result for 120 seconds to avoid thread-pool exhaustion. Developers on Apple Silicon get full functionality; x86 Linux deployments get Actian's production performance.

---

## Accomplishments We're Proud Of

- **End-to-end, auditable flow:** Search → open project → scan → see litigation-relevant flags with exact text excerpts → attest on Solana → verify on-chain. A lender can go from "nuclear power plant" to "2 high-severity flags, here's the proof on Solana" in under 60 seconds.
- **Transparent, testable AI:** Pattern-based signals are inspectable — every flag includes the exact matching excerpt and character offset. No black-box score. The test suite enforces this stays accurate.
- **Real data, real scale:** 61,881 projects, 6.97 million pages, 60+ agencies. Not a toy dataset.
- **No trust required for verification:** The attestation hash is on-chain. Anyone — a lender's counsel, a regulator, a counterparty — can call our verify endpoint or independently recompute the hash from the flags API and confirm the on-chain memo matches.
- **Graceful degradation everywhere:** Actian unavailable? Numpy fallback. Ollama not running? Signals still work, explanations gracefully disabled. Solana RPC slow? Attestation queued with informative errors.

---

## What We Learned

- **NEPA document structure is deeply heterogeneous.** Process-type awareness is not optional — the same text patterns mean completely different things in a CE vs an EIS. Treating all 61K projects identically produces useless signals.
- **The SPL Memo program is underused for integrity proofs.** No custom Anchor program, no IDL, no token — just a single instruction to a deployed system program and you have tamper-proof, timestamped, publicly verifiable proof of existence. For compliance records, that's exactly the right primitive.
- **Graceful degradation makes development tractable.** By designing Actian VectorAI DB as an optional accelerator (with a correct numpy fallback), the entire team could iterate on semantic search features without needing a running Docker container. The fallback isn't a workaround — it's a first-class code path with its own tests.
- **False positive discipline matters more than recall.** A compliance tool that fires on benign language destroys trust faster than missing a real flag. Every signal tightening came with a test that prevented regression.
- **SQLite at this scale is underrated.** FTS5 full-text search over 6 million pages of text, WAL mode, and good indexes — sub-second search on a laptop, no Postgres required.

---

## What's Next

- **Mainnet attestations:** Production attestations on Solana mainnet with a permissioned keypair or multisig for institutional use.
- **Expanded signal library:** Climate risk, water impacts, species habitat, section 4(f), each tuned per agency. Different agencies have different documentation standards — per-agency calibration would significantly reduce false positives.
- **Portfolio due diligence:** Bulk attest many projects in one flow for a lender underwriting an energy portfolio.
- **Richer semantic search:** As the Actian vector index grows (currently capped at 100 docs for API cost control), "find projects like this one" and cross-project pattern discovery become viable at scale.
- **MCP integration:** VERA already has a Model Context Protocol server stub (`backend/mcp/server.py`). Exposing NEPA project data and compliance signals as MCP tools would let any MCP-compatible AI assistant query VERA as a structured data source.

---

## Try It Yourself

### Setup

```bash
# 1. Clone, create venv, install dependencies
git clone <repo>
cd vera
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Add NEPATEC data (CE/EA/EIS JSONL) under nepatec_data/
#    then ingest into SQLite
python -m backend.db.ingest

# 3. (Optional) Run Ollama for on-device LLM
ollama serve &
ollama pull qwen2.5:3b

# 4. (Optional) Add a funded devnet keypair for Solana attestation
#    Visit https://faucet.solana.com and paste the public key to airdrop SOL
#    Save keypair JSON as devnet-keypair.json in the repo root

# 5. (Optional) Run Actian VectorAI DB for production vector search
#    docker run -p 50051:50051 actian/cortex:latest
#    Then trigger indexing via the Semantic Search tab
```

### Run

```bash
export OPENAI_API_KEY=sk-...   # for semantic indexing
uvicorn backend.main:app --reload
# Open http://localhost:8000
```

### Demo Flow

1. Search `"nuclear"` or `"Savannah River"` → select a project
2. Click **Scan** → see flags (e.g. `ej_absent` HIGH, `deferred_mitigation` HIGH) with exact text excerpts
3. Click **Attest on Solana** → tx signature appears → open Solana Explorer (devnet) link
4. Click **Verify** → confirms the on-chain memo hash matches current scan output
5. Navigate to **Semantic Search** → ask *"Which EIS documents have the weakest environmental justice analysis?"*
6. Navigate to **Stuckness Radar** → see which FAST-41 permitting projects are stuck and why

### Run Tests

```bash
pytest tests/test_signals.py -v   # 18 synthetic signal tests
```

---

**VERA** = Verified Environmental Review & Attestation.  
NEPA compliance risk, in plain language, semantically searchable via Actian VectorAI, with tamper-proof proof on Solana.
