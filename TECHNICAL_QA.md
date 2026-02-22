# VERA — Technical Judge Q&A

Anticipated questions across data, signals, AI, blockchain, architecture, and business — with direct, honest answers.

---

## Table of Contents

1. [Domain & Problem](#1-domain--problem)
2. [Data Pipeline](#2-data-pipeline)
3. [Compliance Signal Scanner](#3-compliance-signal-scanner)
4. [Semantic Search & Actian VectorAI DB](#4-semantic-search--actian-vectorai-db)
5. [Solana Attestation Layer](#5-solana-attestation-layer)
6. [LLM & Generative AI Usage](#6-llm--generative-ai-usage)
7. [Architecture Decisions](#7-architecture-decisions)
8. [Scalability & Production Readiness](#8-scalability--production-readiness)
9. [Trust, Security & Legal](#9-trust-security--legal)
10. [Business Model & Impact](#10-business-model--impact)

---

## 1. Domain & Problem

### Q: What is NEPA and why does it matter for lenders?

The National Environmental Policy Act (NEPA, 42 U.S.C. §4321) requires federal agencies to assess environmental consequences before approving major projects. Every utility-scale solar farm, pipeline, highway, mine, or federal land use that requires a federal permit must clear NEPA first.

Lenders care because **NEPA litigation is the primary permitting risk** for infrastructure projects. A court injunction during construction — even a temporary one — can trigger loan defaults, delay project revenue, and void construction contracts. Litigation success rates are meaningful: plaintiffs win roughly 20–30% of NEPA challenges, and the grounds are highly predictable. Courts repeatedly overturn approvals for the same categories of defects: inadequate alternatives analysis, missing environmental justice discussion, deferred mitigation commitments, and approvals contingent on studies not yet completed.

VERA gives lenders a machine-readable risk score on these exact litigation grounds, without requiring a paralegal to read a 2,000-page EIS.

### Q: Who is the actual user?

Primary: **infrastructure lenders and their environmental counsel** doing pre-close due diligence on project finance deals. Secondary: **government agencies** reviewing their own documents, **project developers** stress-testing their NEPA submissions before filing, and **researchers** studying permitting patterns across agencies.

### Q: Isn't this already solved by e-NEPA portals or law firms?

No. The federal ePlanning and NEPA portals are document repositories — they offer keyword search but no compliance analysis. Law firms provide legal opinions but at $500–$1,500/hour and weeks of turnaround. VERA is not a replacement for legal counsel; it is a **first-pass triage tool** that tells a lender whether to look harder and where, before engaging legal review.

---

## 2. Data Pipeline

### Q: Where does the NEPA document text come from?

The **NEPATEC2.0** dataset from PNNL (Pacific Northwest National Laboratory), published under CC0-1.0 and hosted on HuggingFace (`PNNL/NEPATEC2.0`). It contains:

| Type | Projects | Files | Pages |
|------|----------|-------|-------|
| CE (Categorical Exclusion) | 54,668 | 73,544 | 366,876 |
| EA (Environmental Assessment) | 3,083 | 14,242 | 469,106 |
| EIS (Environmental Impact Statement) | 4,130 | 54,297 | 6,131,757 |
| **Total** | **61,881** | **142,083** | **6,967,739** |

These are structured JSONL records — each line is a complete project with metadata, file metadata, and extracted page text. PNNL used OCR and parsing pipelines to extract text from the original PDFs.

### Q: How current is the data?

The NEPATEC2.0 dataset was publicly released in August 2025 and covers documents through roughly mid-2025. VERA is not a live feed — it is a snapshot. For production use, a live integration with the federal ePlanning API or EPA's NEPA database would be needed to stay current.

### Q: How does the ingest pipeline handle document selection for large EIS projects?

This is the most important data engineering decision. Many EIS projects have 5–15 documents all flagged `main_document: YES` — Records of Decision, errata sheets, appendices, and executive summaries. Running signal detection on a 3-page errata sheet instead of the 1,200-page Final EIS would produce useless results.

We resolve this with process-type-aware `is_main` selection:

```python
def _pick_main(docs, process_type):
    if process_type != "EIS":
        return {d for d in docs if _is_main_flag(d)}
    # For EIS: prefer FEIS/DEIS analysis documents
    feis = [d for d in docs if _doc_type(d) in ("FEIS", "DEIS", "EIS")]
    return {_doc_id(d) for d in (feis or [d for d in docs if _is_main_flag(d)])}
```

FEIS (Final EIS) and DEIS (Draft EIS) are the analysis documents. The ROD (Record of Decision) summarizes what was decided; the FEIS is where the analysis lives and where litigation defects appear. We fall back to `main_document: YES` only when no FEIS/DEIS is present.

### Q: What happens when document metadata is blank?

Very common, especially for older EIS files. We infer `doc_type` and milestone events from filename patterns as a fallback. For example, a file named `savannah_river_ROD_2019.pdf` gets inferred `doc_type = "ROD"` and a `Record of Decision` milestone event. This is imperfect but dramatically better than leaving those fields null.

### Q: Why is `ce_category` stored separately?

Categorical Exclusions are approved under specific regulatory categories (e.g., `B1`, `C4` under BLM's Instruction Memorandum). The CE category tells you what kind of action was approved without reading the document. This is useful for analytics — e.g., "how many B1 CEs does the Forest Service issue per year?" — and will be critical for future per-category signal tuning.

### Q: How is FAST-41 data handled differently?

FAST-41 (Fixing America's Surface Transportation Act, Title 41) created a federal permitting dashboard with structured milestone data for large infrastructure projects. We ingest this as a separate CSV (`FAST-41_Projects_Data_20260221.csv`, 22,244 rows) and compute **stuckness scores** — a heuristic for identifying projects that have been in one permitting stage longer than expected. This feeds the Stuckness Radar page and is entirely separate from the NEPATEC ingest pipeline.

---

## 3. Compliance Signal Scanner

### Q: Why regex instead of an LLM for signal detection?

Three reasons:

**1. Explainability.** Every flag includes the exact verbatim excerpt that triggered it and its character offset in the document. A lender can click a flag, see the sentence, and immediately understand why it was flagged. An LLM producing "this document has deferred mitigation" gives you a conclusion with no evidence trail.

**2. Testability.** Each detector has a `pytest` test suite with should-fire and should-not-fire cases. We can prove the detectors behave correctly and detect regressions. You cannot write unit tests for an LLM.

**3. Determinism.** The same document scanned twice always produces the same flags. LLM outputs vary by temperature, version, and prompt caching. For a compliance record that will be attested on-chain, determinism is required.

The tradeoff is recall: regex won't catch creative paraphrasing of deferred mitigation. That's an acceptable tradeoff for a first-pass triage tool — false negatives mean a lender does more work; false positives destroy trust.

### Q: How do you prevent false positives?

Two mechanisms:

**Explicit exclusion patterns.** For `deferred_mitigation`, we exclude `"prior to construction"` — this phrase appears constantly in committed mitigation language ("BMPs will be implemented prior to construction") and is the opposite of a risk signal. For `future_studies_reliance`, we exclude `"long-term monitoring plan will be developed"` — required by regulation, not a defect.

**Process-type gating.** CE documents are structurally exempt from EJ, no-action, and cumulative-impacts checks. These sections don't exist in CE templates by design. Running those detectors on CE text produces noise. The `EA_EIS_ONLY_FLAGS` frozenset in `config.py` gates these detectors at the scan layer.

### Q: How precise are the signals? Do you have ground truth?

We have synthetic ground truth — a test suite of 18 cases (one should-fire + one should-not-fire per flag type) that all pass. We do not have a labeled evaluation set of real documents with human-verified flags. That is the honest answer.

In practice, we tuned the patterns against several EIS documents manually and reviewed results for plausibility. The patterns are intentionally conservative (high precision, accept lower recall). A missed flag is less harmful than a false positive in a compliance tool.

A future improvement would be building a labeled evaluation set from actual litigated NEPA cases — using court opinions to identify what defects judges found and verifying VERA would have flagged those documents.

### Q: What does a flag record actually contain?

```json
{
  "flag_type": "deferred_mitigation",
  "severity": "high",
  "title": "Deferred mitigation",
  "description": "Mitigation commitments pushed to future decisions.",
  "excerpt": "Mitigation measures will be developed prior to final design.",
  "char_offset": 45821,
  "document_id": "BLM_EIS_0042_FEIS"
}
```

The `char_offset` means a downstream system could render the exact page and paragraph in the original PDF. `excerpt` is the verbatim matching text. `document_id` links to the specific file. All of this is stored in the `flags` table and is included in the attestation hash.

### Q: What is `tribal_interests` flagged as, and why isn't it a risk flag?

`tribal_interests` has severity `info` — it's informational, not a litigation risk indicator. We flag it because tribal consultation under NHPA Section 106 and NEPA adds a process requirement that a lender should verify was completed and documented, but the presence of tribal consultation language in a document is not a defect. It's a signal to review, not a red flag.

### Q: Could someone game the signals by removing flagged language from a document?

In a production deployment with live document ingestion, yes — someone could theoretically clean a document of flagged phrases. However:
1. The underlying risk would still exist — removing "mitigation will be developed in future phases" from a document doesn't change whether mitigation was actually committed.
2. Courts look at the substance of the NEPA analysis, not just whether specific phrases appear. A document scrubbed of red-flag language but still lacking substantive EJ analysis remains vulnerable to litigation.
3. The Solana attestation records the hash of the document text alongside the flags — if the document is modified after attestation, the hash will not match.

---

## 4. Semantic Search & Actian VectorAI DB

### Q: Why a vector database in addition to SQLite FTS5?

FTS5 and vector search answer different questions:

| | SQLite FTS5 | Actian VectorAI DB |
|---|---|---|
| Query type | Exact keyword / BM25 | Semantic similarity |
| Example | `"cumulative impacts"` | `"What documents discuss downstream watershed effects?"` |
| Vocabulary | Must match document words | Matches by meaning |
| Speed | Sub-millisecond at this scale | Fast with ANN index |
| Use case | Finding known projects | Discovering relevant context |

For compliance Q&A ("does this project discuss environmental justice for low-income communities?"), a user may not know the exact phrasing a 1990s-era BLM document uses. Semantic search finds relevant passages regardless.

### Q: How does the embedding and indexing pipeline work?

1. Load up to 100 documents from SQLite, prioritizing EIS > EA > CE by analytical depth
2. Chunk each document into 600-character segments with 80-character overlap
3. Embed each chunk via OpenAI `text-embedding-3-small` (1,536 dimensions) in batches of 100
4. Upsert to Actian VectorAI DB with payload metadata: `project_id`, `doc_type`, `process_type`, `agency`, `state`, `chunk_idx`

Each chunk gets a deterministic integer ID: `abs(hash(f"{doc_id}::{chunk_idx}")) % 2^31`, so re-indexing is idempotent.

### Q: Why cap indexing at 100 documents?

API cost control for the hackathon. At 100 documents averaging ~120KB of text each, chunked to 600 chars with 80-char overlap, we generate roughly 20,000 chunks. At `text-embedding-3-small` pricing (~$0.02/million tokens), this costs approximately $0.50. Indexing the full 142,083-document corpus would cost significantly more and is feasible in production with proper budget controls.

### Q: What happens if Actian VectorAI DB is not available?

The system degrades gracefully to a local numpy store. The fallback:
- Stores L2-normalized float32 vectors as a NumPy array on disk (`index/vectors.npy`) with a pickled payload list
- On search, computes cosine similarity as a matrix dot product: since vectors are L2-normalized at insert, `sim(q, c) = q · c`
- Supports the same metadata filters as Actian (via Python-side filtering)
- Loads into memory at startup, persists atomically on every upsert

The fallback activates in under 500ms (TCP pre-check timeout), and the `_actian_available()` result is cached for 120 seconds to avoid spawning a thread on every request. Callers cannot distinguish Actian from fallback — the public API is identical.

### Q: Why use OpenAI embeddings instead of a local model?

`text-embedding-3-small` produces higher-quality embeddings on domain-specific regulatory text than comparably-sized local models (e.g., `all-MiniLM-L6-v2`). For a hackathon, the API cost is trivial. In a production system with privacy requirements (government clients), switching to a self-hosted model like `e5-large-v2` or `bge-m3` would be the right call — the architecture supports this since `embedder.py` is a thin wrapper.

### Q: How does filtered search work in Actian?

```python
from cortex.filters import Filter, Field
f = Filter().must(Field("process_type").eq("EIS")).must(Field("state").eq("NV"))
results = client.search_filtered(COLLECTION_NAME, query_vector, f, top_k=10)
```

The filter is evaluated server-side before returning results, so the top-k are semantically closest among documents matching all filter conditions. This allows queries like "find semantically relevant EIS documents from Nevada" without post-filtering on the client.

---

## 5. Solana Attestation Layer

### Q: Why does a compliance tool need a blockchain?

The core problem is **trust**. When VERA reports "this project has 2 high-severity flags," the lender has to trust:
- That the scan was run on the actual document, not a cleaned version
- That the results haven't been modified after the fact
- That the timestamp is accurate

A hash in our database is only as trustworthy as our database. A hash written to a public blockchain — where neither we nor anyone else can alter it retroactively — creates a record that is verifiable by any party without trusting us. This is the same reason document notarization exists, but instant, programmable, and globally accessible.

### Q: Why the SPL Memo program and not a custom Anchor contract?

The SPL Memo program (`MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr`) is a system program deployed on all Solana clusters that accepts arbitrary UTF-8 data and writes it permanently to the transaction log. This gives us everything we need:

- **No deployment cost** — it's already live on devnet and mainnet
- **No IDL or ABI** — verification only requires reading the transaction, not knowing a schema
- **No program bugs** — the Memo program has been in production for years
- **Human readable** — the memo is JSON, directly visible in Solana Explorer

A custom Anchor program would add deployment risk, upgrade authority complexity, and program account rent — none of which we need for a "proof of existence" use case.

### Q: What exactly is written on-chain?

```json
{
  "v": 1,
  "pid": "BLM_EIS_NV_0042",
  "ts": "2026-02-22T09:41:00Z",
  "flags": {"high": 2, "medium": 1, "info": 3},
  "hash": "sha256:a3f7c2e1..."
}
```

The `hash` is SHA-256 of the full attestation payload (project ID + timestamp + complete flag detail including excerpts + SHA-256 of each scanned document's text content). The full payload never goes on-chain; only its hash does. This is the same design as certificate transparency logs and git commits.

### Q: How does verification work?

A verifier calls `GET /api/projects/{id}/verify`. VERA:
1. Fetches the stored `solana_tx_signature` from the DB
2. Calls `client.get_transaction(sig)` on the Solana RPC
3. Decodes the memo from the transaction's instruction data (base58 → UTF-8 → JSON)
4. Recomputes the expected hash from the current flag API output
5. Compares — if `on_chain_hash == sha256:recomputed_hash`, `verified: true`

A verifier can also do this independently: fetch the raw transaction from any Solana explorer, decode the memo, call the VERA flags API, compute SHA-256 themselves, and confirm it matches. At no point do they have to trust VERA's verify endpoint.

### Q: Why devnet and not mainnet?

Cost and risk for a hackathon. Devnet SOL is free (faucet airdrop), confirmation is typically <2 seconds, and all Solana tooling works identically. The code is mainnet-ready — the only change required is swapping `SOLANA_RPC_URL` to a mainnet endpoint and using a funded mainnet keypair.

### Q: Who pays for the Solana transaction fees? Could this be abused?

In the current implementation, VERA's keypair pays the transaction fee (~0.000005 SOL, roughly $0.001 at current prices). In production, there are several options:
- **User pays:** Connect a browser wallet (Phantom, Backpack) and have the user sign + pay
- **Service pays:** Absorb the cost as part of a subscription (fees are negligible at scale)
- **User-signed memo:** User provides their own keypair for the memo — their signature is the provenance proof

### Q: What if VERA's backend is compromised and flags are manipulated after attestation?

The attestation hash covers the flag detail *and* the document text hashes. If flags are changed in the VERA DB, the recomputed hash will not match the on-chain hash, and verify will return `verified: false`. This is the tamper-evidence property. The document text hashes further ensure that if the underlying document was replaced, the hash chain breaks.

---

## 6. LLM & Generative AI Usage

### Q: Does the LLM make the compliance determinations?

No. The compliance determinations — the flags — are made entirely by the regex-based signal detectors. The LLM is used for:

1. **Flag explanations** — given a flag type and the triggering excerpt, explain in plain English why this is a litigation risk (Ollama, retrieval-constrained)
2. **Project Q&A** — answer natural-language questions about a specific project using retrieved document chunks as context
3. **Stuckness narration** — explain why a FAST-41 project appears stuck, given its milestone data

This separation is intentional. The compliance signal is deterministic, auditable, and testable. The LLM provides explanation and exploration, not judgment.

### Q: How do you prevent hallucination in the Q&A?

Two mechanisms:

**Retrieval-constrained prompts.** The system prompt is explicit: *"Answer the user's question using ONLY the provided document excerpts."* The prompt is constructed by inserting retrieved chunks directly before the question. The model is given no opportunity to draw on pre-training knowledge because the expected answer is present in the context.

**Local inference (Ollama).** Because inference runs on-device, we can log every prompt and response to `llm_audit` table with a SHA-256 of the prompt. If an answer is questioned, we can reproduce exactly what the model was given.

### Q: Why Ollama and qwen2.5:3b specifically?

- **Privacy:** Government document text never leaves the lender's machine or server. This is a non-negotiable requirement for institutional finance clients handling NDA-protected deal documents.
- **Cost:** Zero marginal cost at inference time after initial setup.
- **Speed:** 3B parameter models run in real-time on a modern laptop GPU or CPU.
- **Hackathon pragmatics:** `ollama pull qwen2.5:3b` takes 2 minutes. Larger models degrade demo responsiveness.

In production, a 7B or 13B instruction-tuned model would produce higher-quality explanations. The architecture is model-agnostic — `OLLAMA_MODEL` in `config.py` is the only thing to change.

### Q: What is the RAG retrieval strategy?

For project-scoped chat: retrieve the top-k chunks from the project's main document by keyword similarity (BM25 via SQLite FTS5) or semantic similarity (vector search), construct the context, and generate.

For global chat: keyword search across all projects using FTS5, retrieve matching document snippets, and generate.

For semantic ask: embed the question with OpenAI, K-NN search in Actian VectorAI DB with optional metadata filters, retrieve chunks, and generate.

All three paths converge on the same Ollama `generate()` call with a retrieval-constrained prompt. The difference is how context is assembled.

---

## 7. Architecture Decisions

### Q: Why SQLite instead of PostgreSQL?

Three reasons that apply specifically to this use case:

1. **The data is read-mostly.** After ingest, projects and documents are rarely updated. SQLite's WAL mode handles the limited concurrent writes (flag inserts, attestation column updates) without contention.
2. **FTS5 is exceptional.** SQLite's built-in full-text search with BM25 ranking is fast, feature-complete, and requires zero infrastructure. At 61,881 projects, sub-100ms search is easily achievable.
3. **Operational simplicity.** The entire database is a single file. For a tool that analyzes government documents on a lender's infrastructure, this means simple deployment, easy backup, and no database server to manage.

At production scale (millions of projects, high concurrent users), migrating to PostgreSQL with `pg_trgm` and `tsvector` would be straightforward — the schema is fully relational and the queries are ANSI SQL.

### Q: Why FastAPI over Django or Flask?

- **Native async.** Solana RPC calls, Ollama inference, and Actian gRPC are all async. FastAPI's `async def` endpoints handle these without threading overhead.
- **Automatic OpenAPI docs.** `/docs` gives judges and users an immediate interactive API reference.
- **Pydantic validation.** Request body validation is declarative; invalid requests fail fast with descriptive errors.

### Q: Why Alpine.js + Tailwind instead of React/Vue?

A single-page app with Alpine.js has zero build toolchain — no webpack, no node_modules, no build step. For a hackathon where the backend is the interesting part, this was the right tradeoff. The full frontend is in two files (`index.html`, `app.js`).

### Q: Why is everything configured in a single `config.py`?

Scattered hardcoded values across modules are the #1 cause of "works on my machine" failures. `config.py` is the single authoritative source for all environment-specific values. Every module imports from it. Changing the SQLite path, Ollama URL, Solana RPC, or Actian host requires editing exactly one file.

### Q: Is there authentication on the API?

No, for the hackathon. The backend is designed to run on a developer's local machine or a private server. Production hardening would add:
- API key middleware for all endpoints
- Rate limiting per key
- Optional OAuth2 for multi-user deployments
- Project-level access controls (a lender should only see projects they've scanned)

### Q: How are LLM calls audited?

Every call through `llm/client.py` triggers an insert into `llm_audit`:

```sql
CREATE TABLE llm_audit (
    prompt_hash  TEXT,  -- sha256 of prompt
    model        TEXT,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    duration_ms  INTEGER,
    response     TEXT,
    called_at    TEXT
);
```

This provides a complete audit trail: what was asked, what model answered, how long it took, and what it said. In a regulated financial context, this kind of logging is a compliance requirement, not a feature.

---

## 8. Scalability & Production Readiness

### Q: What is the current performance profile?

On a MacBook Pro M3 with the database on an external SSD (T7):
- **FTS5 project search:** <100ms for most queries
- **Signal scan (single EIS):** 200–800ms depending on document length
- **Ollama flag explanation (qwen2.5:3b):** 1–3 seconds
- **Solana attestation:** 3–8 seconds (RPC round-trip + confirmation)
- **Actian vector search:** <50ms when running

Dashboard and radar aggregates are cached in-memory after first computation.

### Q: Can it handle the full 61K+ project corpus?

Ingest: yes, with time. The full NEPATEC2.0 corpus is ~14.6GB of JSONL. Ingest processes in batches with transaction rollback on per-record errors, so it can be interrupted and resumed. Estimated ingest time for the full corpus: several hours.

At-rest query performance: yes. SQLite FTS5 scales to millions of documents. The `flags`, `documents`, and `projects` tables have appropriate indexes. The bottleneck at scale would be concurrent scan requests (CPU-bound regex over large documents), which would benefit from a worker queue (Celery, ARQ) in production.

### Q: What would production hardening look like?

| Concern | Hackathon state | Production fix |
|---------|----------------|----------------|
| Auth | None | API keys + JWT |
| DB | SQLite | PostgreSQL (Supabase, RDS) |
| Vector store | Actian (local) / numpy fallback | Actian hosted / Qdrant Cloud |
| LLM | Ollama local | Self-hosted vLLM or API-gated Ollama cluster |
| Solana | Devnet | Mainnet with multisig keypair |
| Queue | Synchronous scan | Async worker (ARQ/Celery) |
| Monitoring | None | Structured logging (structlog), Prometheus metrics |
| Data freshness | Static NEPATEC snapshot | Live ePlanning API integration |

---

## 9. Trust, Security & Legal

### Q: What happens if the document text stored in VERA is wrong (bad OCR, truncation)?

The signals are only as good as the text. NEPATEC2.0 used automated OCR and parsing. OCR errors are present, especially in scanned PDFs from the 1990s. A missed flag due to OCR corruption is a false negative — the risk exists in the document but VERA doesn't see it.

Mitigations:
1. Every flag shows the verbatim excerpt — a user can spot garbled text immediately
2. The `char_offset` allows pinpointing the exact location in the stored text
3. The `sha256` column on `documents` lets users verify the text content hasn't changed since scan

The honest position: VERA is a screening tool, not a definitive audit. It tells you where to look, not whether a project is legally compliant.

### Q: Is this legal advice?

No. VERA is a document analysis tool. The flags are pattern-matched indicators, not legal opinions. All output should include a disclaimer: "This output is not legal advice. Consult qualified environmental counsel before making investment decisions."

### Q: Is document text sent to any third-party service?

Conditionally:
- **OpenAI Embeddings API**: Chunk text (up to 8,000 characters per chunk) is sent to OpenAI for embedding. This is the only third-party service that receives document content.
- **Ollama**: Inference is entirely local. Document text in prompts never leaves the machine.
- **Solana RPC**: Only the hash and flag summary go on-chain. No document text.
- **Actian VectorAI DB**: In the hackathon setup, Actian runs locally via Docker. In a hosted setup, chunk text would be sent to Actian.

For air-gapped deployments, replacing OpenAI embeddings with a local model (e.g., `bge-m3` via `sentence-transformers`) eliminates all external data transfer.

### Q: Can the Solana attestation be deleted or altered?

No. Solana's ledger is immutable — confirmed transactions cannot be rolled back or modified. The memo written to the SPL Memo program at slot N is permanent. Even if VERA's backend is decommissioned, the attestation records remain on-chain and verifiable via any Solana RPC.

---

## 10. Business Model & Impact

### Q: What is the go-to-market strategy?

Three initial segments:

1. **Infrastructure lenders (banks, project finance):** Subscription SaaS, priced per project scanned. The value proposition is replacing a partial legal review ($5,000–$15,000) with a $50 automated screen.
2. **Environmental consultants:** White-label API access to add VERA signals to their existing due diligence workflows.
3. **Government agencies:** Licensing for agencies to self-assess their NEPA documents before filing — reducing litigation exposure on their own approvals.

### Q: What is the market size?

The U.S. infrastructure investment pipeline is in the trillions (IRA, IIJA, CHIPS Act). Every major project requires NEPA clearance. The **environmental due diligence** segment of project finance is estimated at $2–3 billion annually in legal and consulting fees. VERA targets a small but defensible slice of this: the screening layer before full legal review.

### Q: Why is this better than just prompting GPT-4 with the document?

1. **Determinism.** The same document produces the same flags every time. GPT-4 at temperature 0 is not guaranteed to be consistent.
2. **Auditability.** Every flag has a verbatim excerpt and character offset. "GPT-4 said there's a deferred mitigation problem" is not actionable; "line 4,821 of the FEIS says 'mitigation measures will be developed prior to final design'" is.
3. **Attestation.** You cannot hash a GPT-4 conversation and write it to a blockchain and guarantee reproduction. VERA's deterministic signals make the hash meaningful.
4. **Cost at scale.** GPT-4 on a 2,000-page EIS would cost $5–15 per document. VERA's regex scan costs $0 per document after ingest.
5. **Regulatory defensibility.** In a legal proceeding, "our AI flagged it" is weaker than "our system detected the phrase 'mitigation will be developed in future phases' at character offset 45,821 of document BLM_FEIS_0042."

### Q: What is the long-term vision?

VERA as a **permitting intelligence layer** for the energy transition. As the U.S. attempts to permit hundreds of gigawatts of clean energy by 2035, NEPA review quality and speed is one of the primary bottlenecks. VERA provides:
- Lenders: confidence that NEPA risk is quantified before close
- Developers: pre-filing document quality checks that reduce litigation exposure
- Regulators: portfolio-level analytics on where approvals are thin and where delays cluster
- The public: transparent, machine-readable compliance records that are verifiable without trusting any single institution

The Solana attestation layer is the foundation for a **permitting transparency protocol** — an open ledger of compliance records that any party can query and verify, independent of government IT systems.
