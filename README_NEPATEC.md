# NEPATEC2.0 CE Dataset Downloader

Scripts to download Categorical Exclusions (CE) from the NEPATEC2.0 dataset.

## Overview

The NEPATEC2.0 dataset contains National Environmental Policy Act (NEPA) documents from 60+ federal agencies. This tool specifically downloads the **Categorical Exclusions (CE)** portion:

- **54,668 projects**
- **73,544 files**  
- **366,876 pages**

## Quick Start

### 1. Preview the Dataset (No Authentication Required)

```bash
source .venv/bin/activate
python preview_dataset.py
```

This shows you the data structure without downloading anything.

### 2. Authenticate with HuggingFace

The dataset is **gated** - you need to:

1. **Get access**:
   - Visit: https://huggingface.co/datasets/PNNL/NEPATEC2.0
   - Click "Access repository"
   - Agree to the terms (approval is usually instant)

2. **Add your token to .env file**:
   - Get token: https://huggingface.co/settings/tokens
   - Add to `.env` file: `HF_TOKEN=hf_your_token_here`

### 3. Download Datasets

**Option A: Download Specific Dataset Types**
```bash
source .venv/bin/activate

# Download Categorical Exclusions (1.1 GB)
python download_ce.py

# Download Environmental Assessments (1.5 GB)
python download_ea.py

# Download Environmental Impact Statements (12 GB)
python download_eis.py
```

**Option B: Download Multiple Types at Once**
```bash
source .venv/bin/activate

# Download all three datasets
python download_all.py --all

# Or select specific ones
python download_all.py --ce --ea
python download_all.py --eis
```

**Option C: Verify Authentication First**
```bash
source .venv/bin/activate
python check_auth.py  # Check if authentication works
```

## Output

Downloads will create:
```
nepatec_data/
├── CE/
│   └── ce_full_dataset.jsonl      (1.1 GB, 54,668 records)
├── EA/
│   └── ea_full_dataset.jsonl      (1.5 GB, 3,083 records)
└── EIS/
    └── eis_full_dataset.jsonl     (12 GB, 4,130 records)
```

Each line in the JSONL files is a complete project record with all associated documents and pages.

## Dataset Structure

Each record contains:

```json
{
  "project": {
    "project_ID": "...",
    "project_title": {"value": "..."},
    "project_sector": {"value": "..."},
    "project_type": {"value": "..."},
    "project_description": {"value": "..."},
    "project_sponsor": {"value": "..."},
    "location": {"value": "..."}
  },
  "process": {
    "process_family": {"value": "NEPA"},
    "process_type": {"value": "Categorical Exclusion"},
    "lead_agency": {"value": "..."}
  },
  "documents": [
    {
      "metadata": {
        "document_metadata": {
          "document_ID": {"value": "..."},
          "document_type": {"value": "..."},
          "document_title": {"value": "..."},
          "prepared_by": {"value": "..."},
          "ce_category": {"value": "..."}
        },
        "file_metadata": {
          "file_ID": {"value": "..."},
          "file_name": {"value": "..."},
          "section_or_volume_title": {"value": "..."},
          "main_document": {"value": "Yes/No"},
          "total_pages": {"value": "..."},
          "file_provider": {"value": "..."}
        }
      },
      "pages": [
        {
          "page number": 1,
          "page text": "..."
        }
      ]
    }
  ]
}
```

## Scripts

| Script | Description |
|--------|-------------|
| `preview_dataset.py` | Preview the dataset structure (no download) |
| `check_auth.py` | Verify HuggingFace authentication |
| `download_ce.py` | Download CE dataset only (1.1 GB) |
| `download_ea.py` | Download EA dataset only (1.5 GB) |
| `download_eis.py` | Download EIS dataset only (12 GB) |
| `download_all.py` | Download multiple datasets with options |
| `setup_and_download.sh` | Legacy all-in-one setup script |

## Troubleshooting

### "Dataset is a gated dataset"
- You need to accept terms at: https://huggingface.co/datasets/PNNL/NEPATEC2.0
- Click "Access repository" and agree to terms

### "Not authenticated"
- Get token: https://huggingface.co/settings/tokens
- Run: `huggingface-cli login`
- Paste your token

### Download is slow
- Normal - datasets are large (CE: 1.1GB, EA: 1.5GB, EIS: 12GB)
- Progress shown every 250-1,000 records depending on dataset
- Let it run to completion

### Need EA or EIS instead of CE?
Use the dedicated scripts:
```bash
python download_ea.py   # Environmental Assessments
python download_eis.py  # Environmental Impact Statements
```

Or use the combined downloader:
```bash
python download_all.py --all     # Download everything
python download_all.py --ea      # Just EA
python download_all.py --ce --ea # CE and EA
```

## Dataset Information

**Full NEPATEC2.0 Stats:**
- **Agencies**: 60+
- **Total Projects**: 61,881
- **Total Files**: 142,083
- **Total Pages**: 6,967,739

**Breakdown by Type:**
| Type | Projects | Files | Pages |
|------|----------|-------|-------|
| CE | 54,668 | 73,544 | 366,876 |
| EA | 3,083 | 14,242 | 469,106 |
| EIS | 4,130 | 54,297 | 6,131,757 |

## Citation

If you use this dataset in your research:

```bibtex
@misc{NEPATECv2,  
  author={Sai Munikoti and Dan Nally and Sai Dileep Koneru and others}, 
  title={NEPATEC v2.0: Standardized Metadata and Text Corpus of National Environmental Policy Act Documents}, 
  howpublished={\url{https://www.pnnl.gov/sites/default/files/media/file/PNNL_PermitAI_NEPATECv2_Public_Release_20_08_25.pdf}}, 
  year={2025}
}
```

## License

CC0-1.0 (Creative Commons 0 Public Domain Dedication)

## Contact

For questions about the dataset:
- Email: permitai@pnnl.gov
- Dataset page: https://huggingface.co/datasets/PNNL/NEPATEC2.0

## References

- [HuggingFace Dataset](https://huggingface.co/datasets/PNNL/NEPATEC2.0)
- [PNNL PermitAI](https://www.pnnl.gov/projects/permitai)
- [Council on Environmental Quality (CEQ)](https://www.whitehouse.gov/ceq/)
