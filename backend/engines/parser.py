"""
parser.py — Flagly Budget Scanner
Robustly parses Nigerian budget files (PDF, Excel, CSV).
Handles merged headers, numeric column names, multi-row headers,
duplicate columns, and data that doesn't start on row 1.
"""

import io
import re
import pandas as pd
import pdfplumber
from typing import Optional


AMOUNT_KEYWORDS = [
    "amount", "sum", "allocation", "appropriation", "budget",
    "total", "value", "cost", "estimate", "provision", "naira", "ngn",
    "approved", "proposed", "actual", "expenditure", "revenue", "fund"
]

DESCRIPTION_KEYWORDS = [
    "description", "project", "activity", "item", "particulars",
    "programme", "component", "details", "title", "subject", "head",
    "name", "purpose", "objective", "scheme", "work"
]

LOCATION_KEYWORDS = [
    "location", "state", "lga", "ward", "constituency", "zone",
    "site", "address", "area", "district", "senatorial", "geopolitical"
]

MINISTRY_KEYWORDS = [
    "ministry", "department", "agency", "mda", "office", "parastatal",
    "sector", "institution", "entity", "organisation", "organization"
]

PROJECT_CODE_KEYWORDS = [
    "code", "project code", "prog code", "programme code", "ref",
    "reference", "id", "number", "no.", "serial", "s/n", "sn",
    "item no", "head", "sub-head", "subhead", "vote"
]


def _clean_amount(val) -> Optional[float]:
    """Convert messy amount strings to float."""
    if pd.isna(val):
        return None
    s = str(val).strip()
    # Remove currency symbols, commas, spaces
    s = re.sub(r"[₦,\s]", "", s)
    # Remove any non-numeric except decimal point
    s = re.sub(r"[^\d.]", "", s)
    try:
        result = float(s) if s else None
        # Sanity check - ignore values that are clearly row numbers
        if result is not None and result > 100_000_000_000_000:
            return None
        return result
    except ValueError:
        return None


def _fix_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Convert all column names to strings and fix duplicates."""
    # Convert all to string
    df.columns = [str(c).strip() for c in df.columns]
    
    # Fix duplicate column names
    cols = []
    seen = {}
    for c in df.columns:
        if c in seen:
            seen[c] += 1
            cols.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            cols.append(c)
    df.columns = cols
    return df


def _detect_column(df: pd.DataFrame, keywords: list) -> Optional[str]:
    """Find the best matching column name from a list of keywords."""
    cols_lower = {c: str(c).lower() for c in df.columns}
    for kw in keywords:
        for col, col_l in cols_lower.items():
            if kw in col_l:
                return col
    return None


def _find_best_header_row(raw_df: pd.DataFrame) -> int:
    """
    Many Nigerian budget files have 2-4 rows of merged title cells
    before the actual column headers. Find the row that looks most
    like real column headers.
    """
    best_row = 0
    best_score = -1
    
    all_keywords = AMOUNT_KEYWORDS + DESCRIPTION_KEYWORDS + LOCATION_KEYWORDS + MINISTRY_KEYWORDS
    
    # Check first 10 rows as potential headers
    for i in range(min(10, len(raw_df))):
        row = raw_df.iloc[i]
        row_text = " ".join([str(v).lower() for v in row if pd.notna(v)])
        
        score = sum(1 for kw in all_keywords if kw in row_text)
        # Bonus: row has many non-null string values
        non_null = sum(1 for v in row if pd.notna(v) and str(v).strip() not in ("", "nan"))
        score += non_null * 0.1
        
        if score > best_score:
            best_score = score
            best_row = i
    
    return best_row


def _try_parse_sheet(xl: pd.ExcelFile, sheet_name: str) -> Optional[pd.DataFrame]:
    """
    Try to parse a single sheet intelligently,
    detecting where the real headers are.
    """
    try:
        # First read without headers to inspect
        raw = xl.parse(sheet_name, header=None)
        if raw.empty or len(raw) < 3:
            return None
        
        # Find best header row
        header_row = _find_best_header_row(raw)
        
        # Re-read with the correct header row
        df = xl.parse(sheet_name, header=header_row)
        df = _fix_columns(df)
        
        # Drop rows that are entirely empty
        df = df.dropna(how="all")
        
        # Drop columns that are entirely empty
        df = df.dropna(axis=1, how="all")
        
        if len(df) < 2:
            return None
            
        return df
        
    except Exception:
        return None


def _standardise(df: pd.DataFrame) -> pd.DataFrame:
    """Rename detected columns to standard names and clean data."""
    mapping = {}

    desc_col = _detect_column(df, DESCRIPTION_KEYWORDS)
    if desc_col:
        mapping[desc_col] = "description"

    amt_col = _detect_column(df, AMOUNT_KEYWORDS)
    if amt_col:
        mapping[amt_col] = "amount_raw"

    loc_col = _detect_column(df, LOCATION_KEYWORDS)
    if loc_col:
        mapping[loc_col] = "location"

    mda_col = _detect_column(df, MINISTRY_KEYWORDS)
    if mda_col:
        mapping[mda_col] = "ministry"

    code_col = _detect_column(df, PROJECT_CODE_KEYWORDS)
    if code_col and code_col not in mapping.values():
        mapping[code_col] = "project_code"

    df = df.rename(columns=mapping)

    # Ensure required columns exist
    for col in ["description", "amount_raw", "location", "ministry", "project_code"]:
        if col not in df.columns:
            df[col] = None

    # If no description column found, use the first text-heavy column
    if df["description"].isna().all():
        for col in df.columns:
            if col in ["amount_raw", "location", "ministry"]:
                continue
            sample = df[col].dropna().astype(str)
            avg_len = sample.str.len().mean() if len(sample) > 0 else 0
            if avg_len > 10:
                df["description"] = df[col]
                break

    df["amount"] = df["amount_raw"].apply(_clean_amount)
    df = df.dropna(subset=["description"])
    df["description"] = df["description"].astype(str).str.strip()
    
    # Remove rows where description is just a number or very short
    df = df[df["description"].str.len() > 4]
    df = df[~df["description"].str.match(r"^\d+\.?\d*$")]
    
    df = df.reset_index(drop=True)
    df["row_id"] = df.index

    keep_cols = ["row_id", "project_code", "description", "amount", "location", "ministry"]
    extra_cols = [c for c in df.columns if c not in keep_cols + ["amount_raw"]]
    return df[keep_cols + extra_cols[:2]]


def parse_excel(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Parse Excel or CSV budget file with robust header detection."""
    buf = io.BytesIO(file_bytes)
    
    if filename.lower().endswith(".csv"):
        # Try different encodings
        for encoding in ["utf-8", "latin-1", "cp1252"]:
            try:
                buf.seek(0)
                df = pd.read_csv(buf, encoding=encoding, errors="replace")
                df = _fix_columns(df)
                return _standardise(df)
            except Exception:
                continue
        raise ValueError("Could not read CSV file.")
    
    # Excel file — try each sheet
    xl = pd.ExcelFile(buf)
    frames = []
    
    for sheet in xl.sheet_names:
        df = _try_parse_sheet(xl, sheet)
        if df is not None and len(df) > 3:
            frames.append((len(df), df))
    
    if not frames:
        raise ValueError(
            "No readable data found in Excel file. "
            "Make sure the file has budget line items in rows and columns."
        )
    
    # Pick the sheet with the most rows
    frames.sort(key=lambda x: x[0], reverse=True)
    best_df = frames[0][1]
    
    return _standardise(best_df)


def parse_pdf(file_bytes: bytes) -> pd.DataFrame:
    """Extract tables from a budget PDF using pdfplumber."""
    buf = io.BytesIO(file_bytes)
    all_tables = []

    with pdfplumber.open(buf) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                # Clean header row
                header = []
                for i, h in enumerate(table[0]):
                    val = str(h).strip() if h and str(h).strip() not in ("", "None") else f"col_{i}"
                    header.append(val)
                
                rows = table[1:]
                try:
                    df_page = pd.DataFrame(rows, columns=header)
                    df_page = _fix_columns(df_page)
                    all_tables.append(df_page)
                except Exception:
                    continue

    if not all_tables:
        raise ValueError(
            "No tables found in PDF. "
            "Make sure the PDF contains actual budget tables, not scanned images."
        )

    combined = pd.concat(all_tables, ignore_index=True)
    return _standardise(combined)


def parse_file(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Main entry — routes to the right parser based on file extension."""
    fn = filename.lower()
    if fn.endswith(".pdf"):
        return parse_pdf(file_bytes)
    elif fn.endswith((".xlsx", ".xls", ".csv")):
        return parse_excel(file_bytes, filename)
    else:
        raise ValueError(
            f"Unsupported file type: {filename}. "
            "Please use PDF, Excel (.xlsx/.xls), or CSV."
        )
