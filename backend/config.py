"""
Centralized settings for VERA.

All hardcoded values live here. Downstream modules import from this module
rather than defining their own constants.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent  # repo root

DB_PATH: Path = _ROOT / "nepa.db"

# NEPATEC JSONL data (CE, EA, EIS) — ingest discovers *.jsonl under subdirs
NEPATEC_DATA_DIR: Path = _ROOT / "nepatec_data"

# FAISS index files written/read by embedder.py
INDEX_DIR: Path = _ROOT / "index"

# ---------------------------------------------------------------------------
# Ollama (local LLM)
# ---------------------------------------------------------------------------

OLLAMA_URL: str = "http://localhost:11434"
OLLAMA_MODEL: str = "qwen2.5:3b"

# ---------------------------------------------------------------------------
# Solana
# ---------------------------------------------------------------------------

# Public devnet RPC — swap for a paid endpoint if rate-limits become an issue
SOLANA_RPC_URL: str = "https://api.devnet.solana.com"

# Path to a Solana keypair JSON file (array of 64 bytes).
# Generate with: solana-keygen new --outfile ~/.config/solana/vera-devnet.json
SOLANA_KEYPAIR_PATH: Path = Path.home() / ".config" / "solana" / "vera-devnet.json"

# ---------------------------------------------------------------------------
# Signal gating
# ---------------------------------------------------------------------------

# These flag types are only meaningful for EA and EIS documents.
# CEs are structurally exempt — running these patterns on CE text produces
# false positives because CE templates do not include the relevant sections.
EA_EIS_ONLY_FLAGS: frozenset[str] = frozenset(
    {
        "ej_absent",
        "ej_thin_coverage",
        "no_action_absent",
        "no_action_thin",
        "cumulative_impacts_thin",
    }
)
