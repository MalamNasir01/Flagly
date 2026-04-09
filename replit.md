# UDEME Budget Red-Flag Scanner

A web tool for accountability journalists at the Centre for Journalism Innovation & Development (CJID) that automatically detects suspicious items in Nigerian public budget files.

## Architecture

**Stack:** Python 3.11, FastAPI, uvicorn, vanilla JS + CSS (single HTML file)

**Structure:**
```
main.py              # FastAPI app, serves frontend + /scan + /health endpoints
requirements.txt     # Python dependencies
engines/
  parser.py          # PDF/Excel/CSV parsing (Format A federal, Format B state)
  flags.py           # 5-flag detection engine
  scorer.py          # Risk scoring (0-100 scale)
frontend/
  index.html         # Complete single-file frontend, dark theme, UDEME branding
```

## Features

- **5 Flag Types:** Inflated Amount, Context Mismatch, Missing Location, Duplicate Cluster, Ghost Project
- **PDF Formats Supported:**
  - Format A: Federal Appropriation Bill (MDA-level, pdfplumber tables)
  - Format B: State government project-level (pdftotext -layout)
- **File Types:** PDF, XLSX, XLS, CSV
- **Client-side PDF report** generation via jsPDF (max 10 pages)
- **No data storage** — fully stateless, all processing in memory

## Running

The app runs on port 5000 via `python main.py`.

**API Endpoints:**
- `POST /scan` — upload budget file, returns flagged items JSON
- `GET /health` — health check

## Dependencies

- **System:** poppler (for pdftotext)
- **Python:** fastapi, uvicorn, pdfplumber, pandas, rapidfuzz, openpyxl, xlrd, python-multipart, aiofiles

## Risk Scoring

- +40 for any HIGH severity flag
- +20 for any MEDIUM severity flag
- +15 per additional flag type (max +30)
- +10 if amount > ₦1B
- +5 if location missing
- Cap at 100 → HIGH ≥70, MEDIUM ≥40, LOW <40
