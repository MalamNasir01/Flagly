"""
Generic profile-driven state budget parser.

Today: Niger State Capital Expenditure by Project (niger_v1 profile).
A second state is added by shipping a new JSON profile — not a parser fork.
See PROFILE.md for the profile schema.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from engines.parser import _finalize_df, _pdftotext, _to_float


AMOUNT_TOKEN_RE = re.compile(r"[\d,]+\.\d{2}|(?<![\d.])-(?![\d.])")
ADMIN_ON_LINE_RE = re.compile(r"(\d{12})\s*-\s*")
LOCATION_ON_LINE_RE = re.compile(
    r"(1\d{7})\s*-\s*([A-Z][A-Z\s/&.-]{2,}?)(?=\s{2,}|\s+[\d,]|\s+-|\s*$)"
)
ECONOMIC_ON_LINE_RE = re.compile(r"(2[123]\d{6})\s*-")
NEW_PROJECT_START_RE = re.compile(
    r"^(?:"
    r"Purchase|Procurement|Procuring|Construction|Renovation|Remodelling|"
    r"Remodeling|Provision|Provission|Supply|Training|Rehabilitation|"
    r"Building|Establishment|Development|Completion|Upgrading|Upgrade|"
    r"Repairs|Fencing|Reconstruction|Drilling|Installation|Equip|"
    r"Food and Agricultural|Alliance for|Publicity|"
    r"(?:[ivx]+\.|[0-9]+\.)\s*"
    r")",
    re.IGNORECASE,
)
FRAGMENT_LEFT_RE = re.compile(
    r"^(?:AND FITTINGS|ORGANS|OFFICE BUILDINGS|RESIDENTIAL BUILDING|"
    r"HOUSING|EQUIPMENT|BUILDINGS|SET|AGRICULTURAL FACILITIES|"
    r"FISHING AND HUNTING|INFRASTRUCTURES)\s*$",
    re.IGNORECASE,
)


def _profiles_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "profiles")


def load_profile(profile_id: str) -> dict:
    mapping = {
        "state_niger": "niger_v1.json",
        "niger_v1": "niger_v1.json",
    }
    filename = mapping.get(profile_id, f"{profile_id}.json")
    path = os.path.join(_profiles_dir(), filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_profiles() -> List[str]:
    return ["state_niger"]


def _detect_columns_present(header_blob: str, profile: dict) -> List[str]:
    found = []
    for col in profile.get("columns") or []:
        frags = col.get("header_fragments") or []
        if any(f in header_blob for f in frags):
            found.append(col["id"])
    return found


def _is_skip_line(line: str, profile: dict) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    rules = profile.get("row_rules") or {}
    for prefix in rules.get("skip_line_prefixes") or []:
        if stripped.startswith(prefix):
            return True
    if stripped.upper().startswith("TOTAL"):
        return True
    # Column-header continuation rows
    headerish = (
        "Location Code and",
        "2025 Performance",
        "January to September",
        "Project Name",
        "Administrative Code",
        "Economic Code and Description",
        "Function Code and Description",
        "2024 Full Year Actuals",
        "2026 Approved Budget",
        "Description January",
    )
    if any(h in stripped for h in headerish) and not ADMIN_ON_LINE_RE.search(line):
        return True
    return False


def _is_fragment_line(line: str, profile: dict) -> bool:
    """Economic/function continuation junk that must not become its own row."""
    stripped = line.strip()
    if not stripped:
        return True
    if ECONOMIC_ON_LINE_RE.match(stripped):
        return True
    if re.match(r"^7\d{4}\s*-", stripped):
        return True
    rules = profile.get("row_rules") or {}
    upper = stripped.upper()
    for word in rules.get("fragment_reject_words") or []:
        # Whole-ish fragment lines (short, mostly the reject word)
        if upper == word or upper.startswith(word + " ") or upper.endswith(" " + word):
            if len(stripped) <= 40:
                return True
        if upper in {word, f"{word} ORGANS", f"ORGANS"}:
            return True
    # Heavily indented short uppercase blobs
    leading = len(line) - len(line.lstrip(" "))
    if leading >= 50 and len(stripped) <= 48 and stripped.upper() == stripped:
        return True
    return False


def _left_text_before_admin(line: str) -> str:
    m = ADMIN_ON_LINE_RE.search(line)
    if not m:
        # text before economic code if present
        em = ECONOMIC_ON_LINE_RE.search(line)
        if em and em.start() > 0:
            return line[: em.start()].strip()
        loc = LOCATION_ON_LINE_RE.search(line)
        if loc and loc.start() > 0:
            return line[: loc.start()].strip()
        return line.strip()
    return line[: m.start()].strip()


def _parse_admin(line: str) -> Tuple[Optional[str], Optional[str]]:
    m = ADMIN_ON_LINE_RE.search(line)
    if not m:
        return None, None
    code = m.group(1)
    rest = line[m.end() :]
    # MDA name ends before location / economic / big gap + amount
    name_m = re.match(
        r"(.+?)(?=\s{2,}(?:1\d{7}\s*-|2[123]\d{6}\s*-|7\d{4}\s*-)|"
        r"\s+1\d{7}\s*-|\s+2[123]\d{6}\s*-|\s*$)",
        rest,
    )
    name = (name_m.group(1) if name_m else rest).strip()
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    # Truncate if location code leaked into name
    loc_in_name = re.search(r"\s+1\d{7}\s*-", name)
    if loc_in_name:
        name = name[: loc_in_name.start()].strip()
    return code, name or None


def _parse_location(line: str) -> Tuple[Optional[str], Optional[str]]:
    m = LOCATION_ON_LINE_RE.search(line)
    if not m:
        return None, None
    code, name = m.group(1), m.group(2).strip()
    name = re.sub(r"\s{2,}.*$", "", name).strip()
    if re.search(r"STATE\s*WIDE", name, re.I):
        # Canonicalize spelling only — value comes from the PDF location column
        # (e.g. 12642600 - State Wide), never used as a missing-location fallback.
        name = "State Wide"
    return code, name or None


def _parse_yoy_amounts(line: str, location_end: int = 0) -> Dict[str, Optional[float]]:
    region = line[location_end:] if location_end else line
    # Prefer the amount region after the location match
    loc = LOCATION_ON_LINE_RE.search(line)
    if loc:
        region = line[loc.end() :]
    tokens = AMOUNT_TOKEN_RE.findall(region)
    # Keep at most the last 4 amount-like tokens (YoY block)
    tokens = tokens[-4:] if len(tokens) > 4 else tokens

    def tok(v: str) -> Optional[float]:
        if v == "-":
            return None
        return _to_float(v)

    vals = [tok(t) for t in tokens]
    # Pad on the left so the last slot is always approved_2026 when <4 tokens
    while len(vals) < 4:
        vals.insert(0, None)
    if len(vals) > 4:
        vals = vals[-4:]
    return {
        "actuals_2024": vals[0],
        "budget_2025": vals[1],
        "performance_2025": vals[2],
        "budget_2026": vals[3],
        "amount": vals[3],
    }


def _looks_like_desc_continuation(line: str) -> bool:
    """Left-column wrap of a project name (no new admin+location amount row)."""
    stripped = line.strip()
    if not stripped:
        return False
    if ADMIN_ON_LINE_RE.search(line) and LOCATION_ON_LINE_RE.search(line):
        return False
    if ECONOMIC_ON_LINE_RE.match(stripped):
        return False
    leading = len(line) - len(line.lstrip(" "))
    if leading > 80:
        return False
    left = _left_text_before_admin(line)
    if not left or len(left) < 3:
        return False
    if FRAGMENT_LEFT_RE.match(left):
        return False
    # A new project title on the left must not be folded into the previous row
    if NEW_PROJECT_START_RE.match(left):
        return False
    return True


def _join_desc(parts: List[str]) -> str:
    cleaned = []
    for p in parts:
        s = (p or "").strip()
        if not s or FRAGMENT_LEFT_RE.match(s):
            continue
        s = re.sub(
            r"^(?:OFFICE BUILDINGS|RESIDENTIAL BUILDING|HOUSING|BUILDINGS|SET|"
            r"AND FITTINGS|ORGANS|EQUIPMENT|Location Code and|2025 Performance|"
            r"Description January to September)\s+",
            "",
            s,
            flags=re.I,
        )
        s = re.sub(
            r"\s+(?:AND FITTINGS|ORGANS|OFFICE BUILDINGS|EQUIPMENT)\b",
            "",
            s,
            flags=re.I,
        )
        if s and not FRAGMENT_LEFT_RE.match(s):
            cleaned.append(s)
    text = " ".join(cleaned)
    text = re.sub(r"\s{2,}", " ", text).strip()
    # Drop accidental header bleed at the start of the first project
    text = re.sub(
        r"^(?:Location Code and\s+)?(?:2025 Performance\s+)?(?:Description\s+)?"
        r"(?:January to September\s+)?",
        "",
        text,
        flags=re.I,
    ).strip()
    return text


def parse_capital_section(text: str, profile: dict) -> Tuple[List[dict], dict]:
    """Parse Capital Expenditure by Project lines from pdftotext -layout output."""
    starts = (profile.get("section_markers") or {}).get("start") or [
        "Capital Expenditure by Project"
    ]
    stops = (profile.get("section_markers") or {}).get("stop") or []

    lines = text.splitlines()
    # Find first start marker
    start_idx = None
    for i, line in enumerate(lines):
        if any(s in line for s in starts):
            start_idx = i
            break
    if start_idx is None:
        return [], {
            "columns_detected": [],
            "amount_column_used": None,
            "section_found": False,
            "error": "Capital Expenditure by Project section not found",
        }

    # Header blob for column detection (next ~15 lines)
    header_blob = "\n".join(lines[start_idx : start_idx + 15])
    columns_detected = _detect_columns_present(header_blob, profile)
    amount_col = profile.get("amount_column") or "approved_2026"

    rows: List[dict] = []
    pending_desc: List[str] = []

    i = start_idx
    while i < len(lines):
        line = lines[i]
        # Allow nested titles like "Basic Education Capital Expenditure by Project"
        if any(s in line for s in starts):
            i += 1
            continue
        if any(s in line for s in stops):
            break
        if _is_skip_line(line, profile):
            i += 1
            continue

        has_admin = bool(ADMIN_ON_LINE_RE.search(line))
        has_loc = bool(LOCATION_ON_LINE_RE.search(line))
        # Require admin + location so MDA summary tables (codes + amounts only)
        # are not treated as capital projects.
        is_anchor = has_admin and has_loc

        if is_anchor:
            mda_code, mda_name = _parse_admin(line)
            loc_code, loc_name = _parse_location(line)
            yoy = _parse_yoy_amounts(line)
            left = _left_text_before_admin(line)
            desc_parts = list(pending_desc)
            if left:
                desc_parts.append(left)
            pending_desc = []

            # Peek following lines for wrapped description tails (no new anchor)
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if _is_skip_line(nxt, profile):
                    break
                if any(s in nxt for s in starts) or any(s in nxt for s in stops):
                    break
                nxt_has_admin = bool(ADMIN_ON_LINE_RE.search(nxt))
                nxt_has_loc = bool(LOCATION_ON_LINE_RE.search(nxt))
                if nxt_has_admin and nxt_has_loc:
                    break
                if _is_fragment_line(nxt, profile):
                    j += 1
                    continue
                cont = _left_text_before_admin(nxt)
                if cont and NEW_PROJECT_START_RE.match(cont):
                    break
                if _looks_like_desc_continuation(nxt):
                    if cont and not ECONOMIC_ON_LINE_RE.search(cont) and not FRAGMENT_LEFT_RE.match(cont):
                        desc_parts.append(cont)
                    j += 1
                    continue
                break

            description = _join_desc(desc_parts)
            if description and len(description) >= 5:
                eco_m = ECONOMIC_ON_LINE_RE.search(line)
                rows.append(
                    {
                        "row_id": None,
                        "description": description,
                        "amount": yoy["amount"],
                        "location": loc_name,
                        "ministry": mda_name,
                        "project_code": mda_code,
                        "is_mda_level": False,
                        "mda_code": mda_code,
                        "mda_name": mda_name,
                        "project_name": description,
                        "project_status": None,
                        "expenditure_code": eco_m.group(1) if eco_m else None,
                        "economic_code": eco_m.group(1) if eco_m else None,
                        "function_code": None,
                        "location_code": loc_code,
                        "actuals_2024": yoy["actuals_2024"],
                        "budget_2025": yoy["budget_2025"],
                        "performance_2025": yoy["performance_2025"],
                        "budget_2026": yoy["budget_2026"],
                    }
                )
            i = j
            continue

        # Non-anchor: accumulate project-title lines until the admin+location row
        if _is_fragment_line(line, profile):
            i += 1
            continue
        left = _left_text_before_admin(line)
        if left and not FRAGMENT_LEFT_RE.match(left) and not ECONOMIC_ON_LINE_RE.match(left.strip()):
            if NEW_PROJECT_START_RE.match(left):
                pending_desc = [left]
            elif _looks_like_desc_continuation(line) or (
                not ADMIN_ON_LINE_RE.search(line) and len(left) >= 5
            ):
                pending_desc.append(left)
        i += 1

    meta = {
        "section_found": True,
        "columns_detected": columns_detected,
        "amount_column_used": amount_col,
        "amount_column_label": "2026 Approved Budget",
        "rows_parsed": len(rows),
        "profile_id": profile.get("id"),
        "jurisdiction": profile.get("jurisdiction"),
    }
    return rows, meta


def parse_state_pdf(contents: bytes, profile_id: str = "state_niger") -> Tuple[pd.DataFrame, dict]:
    """Parse a state budget PDF using the named profile. Returns (df, parse_meta)."""
    profile = load_profile(profile_id)
    text = _pdftotext(contents, timeout=240)
    if not text:
        return pd.DataFrame(), {
            "section_found": False,
            "error": "pdftotext returned no text",
            "columns_detected": [],
            "amount_column_used": None,
            "profile_id": profile.get("id"),
        }
    rows, meta = parse_capital_section(text, profile)
    df = _finalize_df(rows)
    meta["rows_after_finalize"] = len(df)
    return df, meta


def parse_state_text(text: str, profile_id: str = "state_niger") -> Tuple[pd.DataFrame, dict]:
    """Parse pre-extracted layout text (for tests/fixtures)."""
    profile = load_profile(profile_id)
    rows, meta = parse_capital_section(text, profile)
    return _finalize_df(rows), meta
