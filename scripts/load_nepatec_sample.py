#!/usr/bin/env python3
"""
Load a small sample from PNNL/NEPATEC2.0.
Uses one category (CE) and a single split so we don't pull the full 505-file dataset.
Run: huggingface-cli login   (if needed for gated dataset)
Then: source .venv/bin/activate && python scripts/load_nepatec_sample.py
"""

from datasets import load_dataset

# Load something simpler: one category only (CE = Categorical Exclusions).
# Don't use load_dataset("PNNL/NEPATEC2.0") without data_files — CE/EA/EIS have different schemas.
ds = load_dataset(
    "PNNL/NEPATEC2.0",
    data_files="CE/*/*.jsonl",  # one category; still many files but less than full dataset
    split="train",
)

# Keep only first 3 rows in memory for a minimal sample
n = min(3, len(ds))
subset = ds.select(range(n))

print("Dataset (CE category) length:", len(ds))
print("Column names:", subset.column_names)
print(f"\nFirst {n} row(s):")
for i in range(n):
    row = subset[i]
    # Flatten for readable print (skip huge nested 'documents' if present)
    keys = [k for k in row.keys() if k != "documents"][:8]
    preview = {k: (str(row[k])[:80] + "..." if len(str(row[k])) > 80 else row[k]) for k in keys}
    print(f"  [{i}]", preview)
