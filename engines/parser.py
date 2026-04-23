"""
parser.py — PDF/Excel/CSV parsing engine for UDEME Budget Scanner
Handles Format A (Federal MDA summary), Format B (State project-level),
and Format C (Federal project-level, pages ~10–end of Appropriation Bill)
"""

import re
import io
import os
import subprocess
import tempfile
import pandas as pd
from typing import Optional


# ─── Shared regex constants ───────────────────────────────────────────────────

LOCATION_CODE_RE   = re.compile(r'\d{8,11}\s*-\s*[A-Z]{2,}')
AMOUNT_RE          = re.compile(r'[\d,]+\.\d{2}')
MDA_CODE_RE        = re.compile(r'\d{12,14}\s*-')
FUNC_CODE_RE       = re.compile(r'^\s*70\d{2,3}')
LEADING_CODE_RE    = re.compile(r'^\d{10,14}\s*-\s*')
YEAR_IN_DESC_RE    = re.compile(r'\b(19|20)\d{2}\b')

# ─── Format C regex constants ─────────────────────────────────────────────────

# Project code: 2-6 uppercase letters + 8-12 digits (e.g. ERGP12234385, NIP00123456)
FORMAT_C_CODE_RE = re.compile(r'^([A-Z]{2,6}\d{8,12})\s+(.+)', re.MULTILINE)

# Reusable code extractors shared across formats
ERGP_CODE_RE   = re.compile(r'\b([A-Z]{2,6}\d{8,12})\b')
STATE_CODE_RE  = re.compile(r'\b(\d{12,14})\b')

# MDA section header: exactly 10-digit code followed by MDA name
FORMAT_C_SECTION_RE = re.compile(
    r'^(\d{10})\s{1,6}([A-Z][A-Z0-9\s/\-&,.()\[\]]{4,100})$'
)

# Expenditure-type lines: 21xx Personnel, 22xx Overhead, 23xx Capital sub-breakdowns
EXPENDITURE_CODE_RE = re.compile(r'^(21|22|23)\d{0,4}\s')

# Pages that are MDA summary pages — skip entirely
FORMAT_C_SKIP_RE = re.compile(
    r'SUMMARY\s+BY\s+MDAs|TOTAL\s+ALLOCATION\s+\d|'
    r'PERSONNEL\s+COST\s+OVERHEAD\s+COST\s+CAPITAL',
    re.IGNORECASE,
)

# ONGOING / NEW project type keyword
TYPE_RE_C = re.compile(r'\b(ONGOING|NEW)\b')

# Nigerian states for location extraction (sorted longest-first for greedy match)
_NIGERIAN_STATES = [
    'AKWA IBOM', 'CROSS RIVER', 'NASSARAWA',
    'ABIA', 'ADAMAWA', 'ANAMBRA', 'BAUCHI', 'BAYELSA', 'BENUE', 'BORNO',
    'DELTA', 'EBONYI', 'EDO', 'EKITI', 'ENUGU', 'FCT', 'ABUJA', 'GOMBE',
    'IMO', 'JIGAWA', 'KADUNA', 'KANO', 'KATSINA', 'KEBBI', 'KOGI', 'KWARA',
    'LAGOS', 'NIGER', 'OGUN', 'ONDO', 'OSUN', 'OYO', 'PLATEAU', 'RIVERS',
    'SOKOTO', 'TARABA', 'YOBE', 'ZAMFARA',
]
_NIGERIA_STATES_RE = re.compile(
    r'\b(' + '|'.join(re.escape(s) for s in _NIGERIAN_STATES) + r')\b',
    re.IGNORECASE,
)

MAX_ROWS_FORMAT_C = 5_000


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _strip_commas(val):
    if isinstance(val, str):
        return val.replace(',', '').strip()
    return val


def _to_float(val):
    try:
        return float(_strip_commas(str(val)))
    except Exception:
        return None


# ─── Entry point ─────────────────────────────────────────────────────────────

def parse_file(contents: bytes, filename: str) -> Optional[pd.DataFrame]:
    """Entry point — dispatch by file extension."""
    try:
        name_lower = filename.lower()
        if name_lower.endswith('.pdf'):
            return _parse_pdf(contents)
        elif name_lower.endswith('.xlsx') or name_lower.endswith('.xls'):
            return _parse_excel(contents, filename)
        elif name_lower.endswith('.csv'):
            return _parse_csv(contents)
        else:
            try:
                return _parse_pdf(contents)
            except Exception:
                return _parse_excel(contents, filename)
    except Exception as e:
        print(f"[parser] parse_file error: {e}")
        return pd.DataFrame()


# ─── PDF dispatch ─────────────────────────────────────────────────────────────

def _parse_pdf(contents: bytes) -> pd.DataFrame:
    """Detect format then dispatch."""
    try:
        import pdfplumber
    except ImportError:
        return _parse_pdf_format_b(contents)

    is_format_b = False
    is_format_c = False
    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            is_format_b = _detect_format_b(pdf)
            if not is_format_b:
                is_format_c = _detect_format_c(pdf)
    except Exception:
        pass

    print(f"[diag] is_format_b={is_format_b}  is_format_c={is_format_c}")
    if is_format_b:
        return _parse_pdf_format_b(contents)
    elif is_format_c:
        return _parse_pdf_format_c(contents)
    else:
        return _parse_pdf_format_a(contents)


def _detect_format_b(pdf) -> bool:
    """Scan first 20 pages for location code pattern ≥3 matches on any single page."""
    try:
        for page in pdf.pages[:20]:
            text = page.extract_text() or ''
            if len(LOCATION_CODE_RE.findall(text)) >= 3:
                return True
    except Exception:
        pass
    return False


def _detect_format_c(pdf) -> bool:
    """
    Detect Format C: project-code-level federal budget pages.
    Sample pages 10–35; require ≥2 pages that each have 3+ project codes
    AND contain ONGOING/NEW.
    """
    try:
        total = len(pdf.pages)
        print(f"[diag] _detect_format_c: total pages={total}")
        sample = pdf.pages[10:min(36, total)] if total > 10 else pdf.pages
        hits = 0
        for i, page in enumerate(sample):
            page_num = (10 if total > 10 else 0) + i
            text = page.extract_text() or ''
            if FORMAT_C_SKIP_RE.search(text):
                continue
            codes = FORMAT_C_CODE_RE.findall(text)
            has_type = bool(TYPE_RE_C.search(text))
            if codes or has_type:
                print(f"[diag]   page {page_num}: codes={len(codes)}  has_type={has_type}  first_code={codes[0] if codes else None}")
            if len(codes) >= 3 and has_type:
                hits += 1
                print(f"[diag]   → hit #{hits} on page {page_num}")
                if hits >= 2:
                    return True
    except Exception as e:
        print(f"[diag] _detect_format_c exception: {e}")
    return False


# ─── Format A — Federal Appropriation Bill MDA-summary pages ─────────────────

MAX_PAGES_FORMAT_A = 500


def _parse_pdf_format_a(contents: bytes) -> pd.DataFrame:
    """MDA-level federal budget. Extract from pdfplumber tables."""
    import pdfplumber

    rows = []
    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            total_pages = len(pdf.pages)
            pages_to_process = pdf.pages[:MAX_PAGES_FORMAT_A]
            if total_pages > MAX_PAGES_FORMAT_A:
                print(f"[parser] Format A: {total_pages} pages — capping at {MAX_PAGES_FORMAT_A}")
            has_tables = False
            for page in pages_to_process:
                try:
                    tables = page.extract_tables()
                    if not tables:
                        continue
                    if not any(len(t) > 1 for t in tables):
                        continue
                    has_tables = True
                    for table in tables:
                        for row in table:
                            if not row or len(row) < 3:
                                continue
                            first = str(row[0] or '').strip()
                            if not first or not first.split('.')[0].isdigit():
                                continue
                            mda_name = str(row[2] or '').strip() if len(row) > 2 else ''
                            mda_name = re.sub(r'\s{2,}[\d,]+.*$', '', mda_name).strip()
                            mda_name = re.sub(r'\s+[\d,]+.*$', '', mda_name).strip()
                            if not re.search(r'[A-Za-z]{3,}', mda_name):
                                continue
                            if len(mda_name) < 3:
                                continue
                            amount_val = None
                            for cell in reversed(row):
                                v = _to_float(cell)
                                if v is not None and v > 0:
                                    amount_val = v
                                    break
                            # Best-effort: Federal budget table layout is
                            # NO | CODE | MDA | PERSONNEL | OVERHEAD | CAPITAL | TOTAL
                            overhead_val = _to_float(row[4]) if len(row) >= 7 else None
                            capital_val  = _to_float(row[5]) if len(row) >= 7 else None
                            rows.append({
                                'row_id':          None,
                                'description':     mda_name,
                                'amount':          amount_val,
                                'location':        mda_name,
                                'ministry':        mda_name,
                                'project_code':    str(row[1] or '').strip() if len(row) > 1 else None,
                                'is_mda_level':    True,
                                'overhead_amount': overhead_val,
                                'capital_amount':  capital_val,
                            })
                except Exception:
                    continue

            if not has_tables or not rows:
                return _parse_pdf_format_a_text(contents)
    except Exception as e:
        print(f"[parser] Format A pdfplumber error: {e}")
        return _parse_pdf_format_a_text(contents)

    return _finalize_df(rows)


def _parse_pdf_format_a_text(contents: bytes) -> pd.DataFrame:
    """Fallback: pdftotext -layout for Format A."""
    text = _pdftotext(contents)
    if not text:
        return pd.DataFrame()

    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        if not tokens or not tokens[0].replace('.', '').isdigit():
            continue
        amount_val = None
        for tok in reversed(tokens):
            v = _to_float(tok)
            if v is not None and v > 0:
                amount_val = v
                break
        if len(tokens) < 3:
            continue
        mda_name = re.sub(r'\s{2,}[\d,]+\.?\d*.*$', '', stripped).strip()
        mda_name = re.sub(r'^[\d\.]+\s+[\w\d]+\s+', '', mda_name).strip()
        if not re.search(r'[A-Za-z]{3,}', mda_name):
            mda_name = re.sub(r'\s+[\d,]+.*$', '', ' '.join(tokens[2:])).strip()
        if not re.search(r'[A-Za-z]{3,}', mda_name):
            continue
        mda_name = mda_name[:120].strip()
        if len(mda_name) < 3:
            continue
        code_m = ERGP_CODE_RE.search(stripped)
        rows.append({
            'row_id': None,
            'description': mda_name,
            'amount': amount_val,
            'location': mda_name,
            'ministry': mda_name,
            'project_code': code_m.group(1) if code_m else None,
            'is_mda_level': True,
        })

    return _finalize_df(rows)


# ─── Format B — State Government project-level ───────────────────────────────

def _parse_pdf_format_b(contents: bytes) -> pd.DataFrame:
    """Project-level state budget via pdftotext -layout."""
    text = _pdftotext(contents)
    if not text:
        return pd.DataFrame()

    lines = text.splitlines()
    rows = []
    in_project_section = False
    sanity_checked = False

    for line in lines:
        if not line.strip():
            continue
        if FUNC_CODE_RE.match(line):
            continue

        has_location = bool(LOCATION_CODE_RE.search(line))
        has_amount   = bool(AMOUNT_RE.search(line))
        desc_candidate = _extract_description_b(line)
        has_desc = (desc_candidate is not None
                    and len(desc_candidate) >= 15
                    and not desc_candidate[0].isdigit())

        if not in_project_section:
            if has_location and has_amount and has_desc:
                in_project_section = True
            else:
                continue

        location    = _extract_location_b(line)
        amount_match = AMOUNT_RE.search(line)
        amount_val  = _to_float(amount_match.group()) if amount_match else None
        description = _extract_description_b(line)

        if not description or len(description) < 5:
            continue
        if description.startswith('Total') or description.lower().startswith('sub-total'):
            continue
        if re.search(r'\d', description):
            continue
        if len(description) > 120:
            continue

        if location and re.search(r'STATE\s*WIDE', location, re.I):
            location = 'State Wide'

        state_code_m = STATE_CODE_RE.search(line)
        rows.append({
            'row_id': None,
            'description': description,
            'amount': amount_val,
            'location': location,
            'ministry': None,
            'project_code': state_code_m.group(1) if state_code_m else None,
            'is_mda_level': False,
        })

        if not sanity_checked and len(rows) > 500:
            valid_loc = sum(1 for r in rows if r['location'] and len(str(r['location'])) > 3)
            if valid_loc / len(rows) < 0.05:
                rows = []
                in_project_section = False
            sanity_checked = True

    return _finalize_df(rows)


def _extract_description_b(line: str) -> Optional[str]:
    desc = LEADING_CODE_RE.sub('', line)
    mda_match = MDA_CODE_RE.search(desc)
    if mda_match:
        desc = desc[:mda_match.start()]
    desc = desc.strip()
    return desc[:120] if desc else None


_LOCATION_REJECT_WORDS = {
    'MONITORING', 'EVALUATION', 'SERVICES', 'EXPENDITURE', 'RECURRENT',
    'CAPITAL', 'PERSONNEL', 'OVERHEAD', 'REVENUE', 'SECTOR', 'BUDGET',
    'FUND', 'GRANTS', 'LOANS', 'FINANCING', 'BORROWING', 'BONDS',
    'DOMESTIC', 'EXTERNAL', 'INTERNATIONAL', 'TRAINING', 'RESEARCH',
    'PURCHASE', 'CONSTRUCTION', 'REHABILITATION', 'AIDS', 'DONOR',
}


def _extract_location_b(line: str) -> Optional[str]:
    pattern = re.compile(
        r'(\d{8,11}\s*-\s*(?!CAPITAL|GRANTS|RECURRENT|REVENUE|EXPENDITURE|PERSONNEL|OVERHEAD|'
        r'PURCHASE|CONSTRUCTION|REHABILITATION|TRAINING|RESEARCH|INTERNATIONAL|LOANS|'
        r'DOMESTIC|EXTERNAL|BONDS|BORROWING|FINANCING|AIDS|DONOR)[A-Z][A-Z\s]{2,})'
    )
    for m in reversed(list(pattern.finditer(line))):
        raw = m.group(1)
        parts = raw.split('-', 1)
        name = parts[1].strip() if len(parts) > 1 else raw.strip()
        name = re.sub(r'\s{2,}.*$', '', name).strip()
        if not name:
            continue
        if set(name.upper().split()) & _LOCATION_REJECT_WORDS:
            continue
        return name
    return None


# ─── Format C — Federal Appropriation Bill project-level pages ───────────────

def _parse_pdf_format_c(contents: bytes) -> pd.DataFrame:
    """
    Parse Format C: project-level pages of the Federal Appropriation Bill.
    Uses pdftotext -layout; splits output by form-feed into pages;
    skips MDA summary pages; extracts CODE | DESCRIPTION | TYPE | AMOUNT.
    Multi-line descriptions are folded into the previous row.
    """
    text = _pdftotext(contents, timeout=240)
    if not text:
        print("[parser] Format C: pdftotext returned no text")
        return pd.DataFrame()

    pages = text.split('\x0c')
    print(f"[diag] Format C pdftotext: {len(pages)} page chunks, total chars={len(text)}")
    if len(pages) > 10:
        p10_lines = pages[10].splitlines()[:8]
        print(f"[diag] Page-10 first 8 lines raw: {p10_lines}")
    rows = []
    current_ministry = None
    current_mda_code = None
    pages_parsed = 0

    for page_text in pages:
        # Skip summary / header-only pages
        if FORMAT_C_SKIP_RE.search(page_text):
            continue
        # Must have at least one type keyword to be worth parsing
        if not TYPE_RE_C.search(page_text):
            continue
        # Quick check for any project codes
        if not FORMAT_C_CODE_RE.search(page_text):
            continue

        pages_parsed += 1
        lines = page_text.splitlines()
        prev_row = None

        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) < 4:
                continue

            # ── MDA section header? ───────────────────────────────────────────
            section_m = FORMAT_C_SECTION_RE.match(stripped)
            if section_m:
                current_mda_code = section_m.group(1).strip()
                current_ministry = section_m.group(2).strip()[:120]
                prev_row = None
                continue

            # Skip decorative lines, column headers, expenditure code lines
            if re.match(r'^[-=\s]+$', stripped):
                continue
            if re.match(r'^(CODE|S/?N|TYPE|AMOUNT|PROJECT\s+NAME)\b', stripped, re.I):
                continue
            if EXPENDITURE_CODE_RE.match(stripped):
                continue  # Skip Personnel (21xx), Overhead (22xx), Capital (23xx) sub-lines

            # ── Project line? ─────────────────────────────────────────────────
            # Code is always the first token: 2-6 uppercase letters + 8-12 digits
            code_m = re.match(r'^([A-Z]{2,6}\d{8,12})\s+', stripped)
            if code_m:
                code      = code_m.group(1)
                remainder = stripped[code_m.end():]

                # Single-pass: DESCRIPTION   ONGOING|NEW   AMOUNT
                # Amount may have or lack decimal places (e.g. 350,000,000 or 350,000,000.00)
                full_m = re.match(
                    r'^(.+?)\s+(ONGOING|NEW)\s+([\d,]+(?:\.\d+)?)\s*$',
                    remainder.strip(),
                    re.IGNORECASE,
                )
                if full_m:
                    desc_part  = full_m.group(1).strip().rstrip('.,')
                    amount_val = _to_float(full_m.group(3))
                else:
                    # Fallback: type keyword present but amount may be on a later line,
                    # or amount is missing — keep description, amount stays None
                    type_m = TYPE_RE_C.search(remainder)
                    desc_part  = (remainder[:type_m.start()] if type_m else remainder).strip().rstrip('.,')
                    # Last numeric-looking token
                    amount_val = None
                    for tok in reversed(remainder.split()):
                        v = _to_float(tok)
                        if v is not None and v > 0:
                            amount_val = v
                            break

                if not desc_part or len(desc_part) < 5:
                    prev_row = None
                    continue

                location = _extract_location_c(desc_part)

                row_dict = {
                    'row_id':        None,
                    'description':   desc_part[:200],
                    'amount':        amount_val,
                    'location':      location,
                    'ministry':      current_ministry,
                    'project_code':  code,
                    'is_mda_level':  False,
                }
                rows.append(row_dict)
                prev_row = row_dict

                if len(rows) >= MAX_ROWS_FORMAT_C:
                    print(f"[parser] Format C: row cap {MAX_ROWS_FORMAT_C} reached")
                    return _finalize_df(rows)

                continue

            # ── Continuation line? ────────────────────────────────────────────
            # A line with no project code, no type keyword, no trailing large
            # number, but containing letters — belongs to the previous description.
            if (prev_row is not None
                    and re.search(r'[A-Za-z]{3,}', stripped)
                    and not TYPE_RE_C.search(stripped)
                    and not re.search(r'[\d,]{5,}\s*$', stripped)
                    and not FORMAT_C_SECTION_RE.match(stripped)):
                cur = prev_row['description']
                merged = (cur + ' ' + stripped)[:200]
                prev_row['description'] = merged
                if not prev_row['location']:
                    prev_row['location'] = _extract_location_c(merged)

    ergp_rows = [r for r in rows if r.get('project_code') and r['project_code'].startswith('ERGP')]
    print(f"[diag] FORMAT C ROWS WITH ERGP CODES: {len(ergp_rows)}")
    print(f"[diag] TOTAL ROWS COLLECTED: {len(rows)}")
    print(f"[diag] PAGES THAT PASSED ALL GUARDS: {pages_parsed}")
    if ergp_rows:
        print(f"[diag] SAMPLE: {ergp_rows[0]}")
    elif rows:
        print(f"[diag] SAMPLE (no ERGP): {rows[0]}")
    print(f"[parser] Format C: extracted {len(rows)} project rows")
    return _finalize_df(rows)


def _extract_location_c(description: str) -> Optional[str]:
    """Extract all Nigerian state names from a project description, joined by ', '."""
    found = []
    seen: set = set()
    for m in _NIGERIA_STATES_RE.finditer(description):
        name = m.group(0).title()
        key  = name.lower()
        if key not in seen:
            seen.add(key)
            found.append(name)
    return ', '.join(found) if found else None


# ─── Excel / CSV ──────────────────────────────────────────────────────────────

def _parse_excel(contents: bytes, filename: str) -> pd.DataFrame:
    try:
        name_lower = filename.lower()
        if name_lower.endswith('.xls'):
            df = pd.read_excel(io.BytesIO(contents), engine='xlrd')
        else:
            df = pd.read_excel(io.BytesIO(contents), engine='openpyxl')
        return _auto_detect_columns(df)
    except Exception as e:
        print(f"[parser] Excel parse error: {e}")
        return pd.DataFrame()


def _parse_csv(contents: bytes) -> pd.DataFrame:
    try:
        df = pd.read_csv(io.BytesIO(contents))
        return _auto_detect_columns(df)
    except Exception as e:
        print(f"[parser] CSV parse error: {e}")
        return pd.DataFrame()


def _auto_detect_columns(df: pd.DataFrame) -> pd.DataFrame:
    cols_lower = {c: c.lower() for c in df.columns}

    amount_kw = ['amount', 'total', 'allocation', 'approved budget', 'total allocation', 'capital', 'overhead']
    desc_kw   = ['description', 'project', 'item', 'activity', 'mda', 'administrative unit']
    loc_kw    = ['location', 'state', 'lga', 'constituency', 'ward', 'zone']

    def first_match(keywords):
        for kw in keywords:
            for col, cl in cols_lower.items():
                if kw in cl:
                    return col
        return None

    amount_col = first_match(amount_kw)
    desc_col   = first_match(desc_kw)
    loc_col    = first_match(loc_kw)

    rows = []
    for _, row in df.iterrows():
        desc       = str(row[desc_col]).strip() if desc_col else ''
        amount_val = _to_float(row[amount_col]) if amount_col else None
        location   = str(row[loc_col]).strip() if loc_col else None
        rows.append({
            'row_id':       None,
            'description':  desc,
            'amount':       amount_val,
            'location':     location,
            'ministry':     None,
            'project_code': None,
            'is_mda_level': False,
        })

    return _finalize_df(rows)


# ─── Helpers ──────────────────────────────────────────────────────────────────

PDFTOTEXT_PATHS = [
    'pdftotext',
    '/nix/store/s41bqqrym7dlk8m3nk74fx26kgrx0kv8-replit-runtime-path/bin/pdftotext',
    '/usr/bin/pdftotext',
    '/usr/local/bin/pdftotext',
]


def _find_pdftotext() -> Optional[str]:
    import shutil
    cmd = shutil.which('pdftotext')
    if cmd:
        return cmd
    for path in PDFTOTEXT_PATHS[1:]:
        if os.path.isfile(path):
            return path
    try:
        nix = '/nix/store'
        for entry in os.listdir(nix):
            if 'replit-runtime-path' in entry or 'poppler' in entry:
                candidate = os.path.join(nix, entry, 'bin', 'pdftotext')
                if os.path.isfile(candidate):
                    return candidate
    except Exception:
        pass
    return None


def _pdftotext(contents: bytes, timeout: int = 120) -> Optional[str]:
    """Run pdftotext -layout on PDF bytes, return text."""
    try:
        binary = _find_pdftotext()
        if not binary:
            print('[parser] pdftotext not found — poppler may not be installed')
            return None
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name
        result = subprocess.run(
            [binary, '-layout', tmp_path, '-'],
            capture_output=True, timeout=timeout,
        )
        os.unlink(tmp_path)
        if result.returncode == 0:
            return result.stdout.decode('utf-8', errors='replace')
        print(f"[parser] pdftotext failed: {result.stderr.decode()}")
        return None
    except subprocess.TimeoutExpired:
        print(f"[parser] pdftotext timed out after {timeout}s")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[parser] pdftotext error: {e}")
        return None


def _finalize_df(rows: list) -> pd.DataFrame:
    """Post-processing: drop short descriptions, normalize NaN, assign row_id."""
    if not rows:
        return pd.DataFrame()

    all_cols = [
        'row_id', 'description', 'amount', 'location',
        'ministry', 'project_code', 'is_mda_level',
        'overhead_amount', 'capital_amount',
    ]
    # Only keep columns that are present in the data
    present_cols = [c for c in all_cols if any(c in r for r in rows)]
    for r in rows:
        for c in all_cols:
            r.setdefault(c, None)

    df = pd.DataFrame(rows, columns=all_cols)

    df = df[df['description'].notna()]
    df = df[df['description'].str.strip().str.len() >= 5]
    df = df.where(pd.notnull(df), None)
    df = df.reset_index(drop=True)
    df['row_id'] = df.index + 1
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce')

    return df
