# Flagly — Nigerian Budget Red Flag Scanner

Automated detection of red flags in Nigerian budget appropriations.

---

## What it detects

| Flag | Description |
|------|-------------|
| **DUPLICATE** | Near-identical line items (≥85% fuzzy match) within the same budget |
| **AMOUNT_ANOMALY** | Amounts far above typical range for that project category, or statistical outliers |
| **MISSING_LOCATION** | Items ≥₦5m with no traceable state, LGA, ward, or constituency |
| **GHOST_PROJECT** | Same project re-appropriated across multiple budget years with no completion evidence |

---

## Project structure

```
udeme-scanner/
├── backend/
│   ├── app.py               ← FastAPI server (main entry point)
│   ├── requirements.txt
│   └── engines/
│       ├── parser.py        ← Parses PDF / Excel / CSV into standard format
│       └── flags.py         ← All 4 flag engines + risk scorer
└── frontend/
    └── index.html           ← Upload dashboard (open in browser)
```

---

## Setup

### 1. Install Python dependencies

```bash
cd backend
pip install -r requirements.txt
```

You also need the spaCy English model:
```bash
python -m spacy download en_core_web_sm
```

### 2. Start the backend server

```bash
cd backend
uvicorn app:app --reload --port 8000
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### 3. Open the frontend

Just open `frontend/index.html` in your browser — no server needed for the frontend.

---

## How to use

1. Open `frontend/index.html` in Chrome or Firefox
2. Enter the budget year (e.g. `2024`)
3. Drop or select your budget file (PDF, Excel, or CSV)
4. Click **Run Red-Flag Scan**
5. View flagged items sorted by risk score
6. Click any row to see full flag details

---

## Supported file formats

| Format | Notes |
|--------|-------|
| `.xlsx` / `.xls` | Works best — column headers are auto-detected |
| `.csv`  | Must have column headers in row 1 |
| `.pdf`  | Must have embedded tables (not scanned/image PDFs) |

### Tips for Excel/CSV files
The parser auto-detects column names. It looks for columns containing words like:
- **Description**: `description`, `project`, `activity`, `item`, `particulars`
- **Amount**: `amount`, `allocation`, `appropriation`, `budget`, `cost`
- **Location**: `location`, `state`, `lga`, `constituency`, `zone`
- **Ministry**: `ministry`, `department`, `agency`, `mda`

If your columns use different names, rename them to match before uploading.

---

## Multi-year ghost project detection

To compare budgets across years and detect recurring ghost projects:

```bash
curl -X POST http://localhost:8000/scan/multi-year \
  -F "files=@budget_2022.xlsx" \
  -F "files=@budget_2023.xlsx" \
  -F "files=@budget_2024.xlsx" \
  -F "years=2022,2023,2024"
```

---

## API reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/scan` | POST | Scan a single budget file |
| `/scan/multi-year` | POST | Scan multiple years simultaneously |
| `/health` | GET | Check if server is running |

---

## Customising flag sensitivity

Edit `backend/engines/flags.py`:

- **Duplicate threshold**: Change `threshold=85` in `flag_duplicates()` (lower = more flags)
- **Category amount ranges**: Edit `CATEGORY_KEYWORDS` dict for Nigeria-specific project types
- **Statistical fence**: Change `3.0` in `upper_fence = q3 + 3.0 * iqr` (lower = more sensitive)
- **Minimum amount for location flag**: Change `MIN_AMOUNT = 5_000_000`

---

## Next steps (Phase 2)

- [ ] AI narrative summaries using Claude API (one-click "explain this flag" for journalists)
- [ ] Export flagged items to PDF report
- [ ] Connect to Open Treasury portal for automated ingestion
- [ ] Cross-reference against NEITI/BudgIT project completion data
- [ ] Multi-user sessions for team annotation

---

Flagly · Public Accountability Tool
