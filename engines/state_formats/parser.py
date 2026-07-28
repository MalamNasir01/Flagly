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
FUNCTION_ON_LINE_RE = re.compile(r"(7\d{4})\s*-")
NEW_PROJECT_START_RE = re.compile(
    r"^(?:"
    r"Purchase|Procurement|Procuring|Proceurement|Construction|Construcion|"
    r"Renovation|Remodelling|Remodeling|Provision|Provission|Supply|"
    r"Training|Rehabilitation|Building|Establishment|Development|Completion|"
    r"Upgrading|Upgrade|Repairs|Fencing|Reconstruction|Drilling|Installation|"
    r"Equip|Equipping|Extension|Compensation|Payment|Ecological|Erosion|"
    r"Disiltation|Consultancy|Production|Opening|Review|"
    r"World Bank|Food and Agricultural|Alliance for|Publicity|Sensitization|"
    r"Monitoring|Support|Reconstruction|"
    r"(?:[ivx]+|[0-9]+)\.(?!\d)\s*"  # "ii." / "1." list heads — not decimals like "1.5"
    r")",
    re.IGNORECASE,
)
# True wrap tails — must NOT start a new project row
_CONTINUATION_START_RE = re.compile(
    r"^(?:"
    r"and\s+|or\s+|of\s+|for\s+|to\s+|in\s+|at\s+|with\s+|within\s+|from\s+|"
    r"ones?\b|system\b|community\b|Bank\s+Loan|facilities\b|machines?\b|"
    r"sorting\b|copier\b|stand\b|e\.t\.c|organs\b|conference\b"
    r")",
    re.IGNORECASE,
)
_MDA_NAME_WRAP_RE = re.compile(
    r"^(?:"
    r"Intergovernmental\s+Affairs|Affairs|Corporation|Agency|Commission|"
    r"Board|Office|Department|Ministry|Bureau|Government|\(SDGs\)\s*Office|"
    r"and\s+Intergovernmental\s+Affairs|Education|Ed\b"
    r")\b",
    re.IGNORECASE,
)
# Project-verb heads used by parse-quality merge detection (kept for tests/exports)
_PROJECT_VERB_RE = re.compile(
    r"\b(?:"
    r"construction|construcion|supply|development|procurement|procuring|"
    r"renovation|remodelling|remodeling|provision|rehabilitation|"
    r"reconstruction|purchase|establishment|extension|compensation|"
    r"payment|erosion|drilling|installation|upgrading|upgrade|repairs|"
    r"fencing|building|completion|equip(?:ping)?|consultancy|production|"
    r"monitoring|sensitization|publicity"
    r")\b",
    re.IGNORECASE,
)
FRAGMENT_LEFT_RE = re.compile(
    r"^(?:AND FITTINGS|ORGANS|OFFICE BUILDINGS|RESIDENTIAL BUILDING|"
    r"HOUSING|EQUIPMENT|BUILDINGS|SET|AGRICULTURAL FACILITIES|"
    r"FISHING AND HUNTING|INFRASTRUCTURES|ROADS|WATER-WAYS|"
    r"WATER FACILITIES|ELECTRICITY|PROTECTION)\s*$",
    re.IGNORECASE,
)
# Project-name column is left-aligned; MDA wraps sit much further right
_PROJECT_COL_MAX_INDENT = 45


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _title_looks_incomplete(text: str) -> bool:
    """True when a project title likely continues on the next line."""
    t = (text or "").strip()
    if not t:
        return True
    if t[-1] in "-,;:/&":
        return True
    if re.search(
        r"\b(?:for|of|and|or|to|in|at|with|within|from|the|a|an|by)$",
        t,
        re.I,
    ):
        return True
    # Mid-count wraps: "…Additional 17no" / "…8 no."
    if re.search(r"\b\d+\s*nos?\.?$", t, re.I):
        return True
    return False


def _looks_like_new_project_title(line: str) -> bool:
    """True when left-column text starts a new project via a known project verb/list head.

    Capitalized mid-title wraps (\"Guest Houses…\", \"Rice, Maize…\") are NOT
    new projects — those are handled as continuations during peek.
    """
    stripped = line.strip()
    if not stripped or len(stripped) < 8:
        return False
    if _line_indent(line) >= _PROJECT_COL_MAX_INDENT:
        return False
    left = _left_text_before_admin(line)
    if not left or len(left) < 8:
        return False
    if FRAGMENT_LEFT_RE.match(left) or _MDA_NAME_WRAP_RE.match(left):
        return False
    if ECONOMIC_ON_LINE_RE.match(left.strip()) or FUNCTION_ON_LINE_RE.match(left.strip()):
        return False
    if _CONTINUATION_START_RE.match(left):
        return False
    return bool(NEW_PROJECT_START_RE.match(left))


def _looks_like_orphan_project_title(line: str) -> bool:
    """Reset pending when a left-column title appears without a structural row yet.

    Includes known verbs plus substantial capitalized titles that lack a verb
    (e.g. \"Suleja, Chanchaga Model Cities Masterplan…\").
    """
    if _looks_like_new_project_title(line):
        return True
    stripped = line.strip()
    if not stripped or len(stripped) < 18:
        return False
    if _line_indent(line) >= _PROJECT_COL_MAX_INDENT:
        return False
    if ADMIN_ON_LINE_RE.search(line) or LOCATION_ON_LINE_RE.search(line):
        return False
    left = _left_text_before_admin(line)
    if not left or len(left) < 18:
        return False
    if FRAGMENT_LEFT_RE.match(left) or _MDA_NAME_WRAP_RE.match(left):
        return False
    if _CONTINUATION_START_RE.match(left) or left[0].islower():
        return False
    return left[0].isupper()


def _looks_like_desc_continuation(line: str) -> bool:
    """Left-column wrap of the current project name (not a new project / anchor)."""
    stripped = line.strip()
    if not stripped:
        return False
    if ADMIN_ON_LINE_RE.search(line) and LOCATION_ON_LINE_RE.search(line):
        return False
    if ECONOMIC_ON_LINE_RE.match(stripped) or FUNCTION_ON_LINE_RE.match(stripped):
        return False
    if _line_indent(line) >= _PROJECT_COL_MAX_INDENT:
        return False
    left = _left_text_before_admin(line)
    if not left or len(left) < 3:
        return False
    if FRAGMENT_LEFT_RE.match(left) or _MDA_NAME_WRAP_RE.match(left):
        return False
    # Verb-headed new projects must never fold into the prior row
    if _looks_like_new_project_title(line):
        return False
    # Explicit wrap tails and any non-verb left-column fragment
    if _CONTINUATION_START_RE.match(left) or left[0].islower():
        return True
    if not NEW_PROJECT_START_RE.match(left):
        return True
    return False


def _is_mda_name_wrap_line(line: str) -> bool:
    if ADMIN_ON_LINE_RE.search(line) or LOCATION_ON_LINE_RE.search(line):
        return False
    if _line_indent(line) < _PROJECT_COL_MAX_INDENT:
        return False
    left = line.strip()
    # Strip trailing economic/function fragment labels for the match
    left_head = re.split(r"\s{2,}", left, maxsplit=1)[0].strip()
    if not left_head or ECONOMIC_ON_LINE_RE.match(left_head) or FUNCTION_ON_LINE_RE.match(left_head):
        return False
    if FRAGMENT_LEFT_RE.match(left_head):
        return False
    return bool(
        _MDA_NAME_WRAP_RE.match(left_head)
        or (
            len(left_head) <= 48
            and left_head[0].isupper()
            and not NEW_PROJECT_START_RE.match(left_head)
        )
    )


def _is_right_column_noise(line: str, profile: dict) -> bool:
    """Heavily indented economic/function/MDA fragments between title wraps."""
    if _line_indent(line) < _PROJECT_COL_MAX_INDENT:
        return False
    if ADMIN_ON_LINE_RE.search(line) or LOCATION_ON_LINE_RE.search(line):
        return False
    if _is_mda_name_wrap_line(line) or _is_fragment_line(line, profile):
        return True
    stripped = line.strip()
    if FRAGMENT_LEFT_RE.match(stripped):
        return True
    if ECONOMIC_ON_LINE_RE.match(stripped) or FUNCTION_ON_LINE_RE.match(stripped):
        return True
    # e.g. "Government … AND FITTINGS"
    if "AND FITTINGS" in stripped.upper() or "OFFICE BUILDINGS" in stripped.upper():
        return True
    return True  # unknown high-indent → skip during peek, don't abort


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
    cut_at = None
    for rx in (ADMIN_ON_LINE_RE, ECONOMIC_ON_LINE_RE, FUNCTION_ON_LINE_RE, LOCATION_ON_LINE_RE):
        m = rx.search(line)
        if m and m.start() > 0:
            cut_at = m.start() if cut_at is None else min(cut_at, m.start())
    text = line[:cut_at].rstrip() if cut_at is not None else line.rstrip()
    # Drop right-column MDA name fragments ("… (AGILE) … Education")
    gap = re.search(r"^(.*?)(?:\s{8,})(\S.*)$", text)
    if gap:
        right = gap.group(2).strip()
        if _MDA_NAME_WRAP_RE.match(right) or (
            len(right) <= 24 and right[0].isupper() and " " not in right.strip()
            and not NEW_PROJECT_START_RE.match(right)
        ):
            text = gap.group(1)
    return text.strip()


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


def _join_desc(parts: List[str]) -> str:
    cleaned = []
    for p in parts:
        s = (p or "").strip()
        if not s or FRAGMENT_LEFT_RE.match(s) or _MDA_NAME_WRAP_RE.match(s):
            continue
        # Drop leaked function / economic code tails
        s = re.sub(r"\s+7\d{4}\s*-.*$", "", s)
        s = re.sub(r"\s+2[123]\d{6}\s*-.*$", "", s)
        s = re.sub(
            r"^(?:OFFICE BUILDINGS|RESIDENTIAL BUILDING|HOUSING|BUILDINGS|SET|"
            r"AND FITTINGS|ORGANS|EQUIPMENT|Location Code and|2025 Performance|"
            r"Description January to September|WATER-WAYS|ROADS)\s+",
            "",
            s,
            flags=re.I,
        )
        s = re.sub(
            r"\s+(?:AND FITTINGS|ORGANS|OFFICE BUILDINGS|EQUIPMENT|WATER-WAYS|"
            r"GENERAL PERSONNEL SERVICES|BASIC RESEARCH)\b.*$",
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


def _pending_belongs_with_anchor(pending: List[str], anchor_left: str) -> bool:
    """True when pending title lines are wraps of this anchor, not a prior project."""
    if not pending:
        return True
    p0 = pending[0].strip()
    a0 = (anchor_left or "").strip()
    if not a0:
        # Title lives entirely in pending (admin/loc line has no left text)
        return True
    # Two distinct project-verb heads → do not merge
    if NEW_PROJECT_START_RE.match(p0) and NEW_PROJECT_START_RE.match(a0):
        return False
    # Verb-headed pending + non-verb left = title wrap / spec continuation
    # (e.g. "Purchase … Seed" + "Planters, 8HP…", or "Purchase…" + "1.5 Hp…")
    if NEW_PROJECT_START_RE.match(p0) and not NEW_PROJECT_START_RE.match(a0):
        return True
    # Wrap-tails / orphan fragments must not glue onto a new verb-headed project
    if NEW_PROJECT_START_RE.match(a0) and not NEW_PROJECT_START_RE.match(p0):
        if _CONTINUATION_START_RE.match(p0) or (p0 and p0[0].islower()):
            return False
        if (
            p0
            and p0[0].isupper()
            and len(p0) >= 12
            and p0.lower() not in a0.lower()
            and a0.lower() not in p0.lower()
        ):
            return False
    return True


def _attach_orphan_pending_to_last(rows: List[dict], pending: List[str]) -> None:
    """Glue a wrap-tail that arrived after emit onto the previous project row."""
    if not rows or not pending:
        return
    merged = _join_desc([rows[-1].get("description") or ""] + pending)
    if merged:
        rows[-1]["description"] = merged
        rows[-1]["project_name"] = merged


def description_merge_issues(description: str) -> List[str]:
    """Parse-quality signals for likely merged/split project descriptions."""
    issues: List[str] = []
    desc = (description or "").strip()
    if not desc:
        return issues

    # Mid-phrase start: wrap conjunctions / lowercase tails — not list markers.
    if re.match(
        r"^(?:and|or|of|for|to|in|at|with|within|from)\s+",
        desc,
        re.I,
    ):
        issues.append("mid_phrase_start")
    elif (
        desc[0].islower()
        and not NEW_PROJECT_START_RE.match(desc)
        and not re.match(r"^[ivx]+\.", desc, re.I)
    ):
        issues.append("mid_phrase_start")

    # Second project-title head: Capitalized "Verb of …" / "Erosion Control …"
    # after a substantial first title. Optional list marker (i. / ii.) allowed.
    second = re.search(
        r".{20,}?\s+(?:(?:[ivx]+|[0-9]+)\.(?!\d)\s*)?"
        r"(?:"
        r"(?:Construction|Construcion|Supply|Development|Procurement|Procuring|"
        r"Renovation|Remodelling|Remodeling|Provision|Purchase|Extension|"
        r"Compensation|Payment|Rehabilitation|Reconstruction|"
        r"Establishment|Completion|Drilling|Installation|Upgrading|Upgrade|"
        r"Equipping|Building|Fencing|Repairs)\s+of"
        r"|Erosion\s+Control"
        r")\b",
        desc,
    )
    if second:
        # Ignore object-of-preposition noun uses: "for Development of", "the Production of"
        start = second.start()
        window = desc[max(0, start - 12) : start + 1].lower()
        if not re.search(r"\b(?:for|of|the|and|to|in|by)\s+$", window):
            issues.append("multiple_project_verbs")
    return issues


def assess_parse_quality(rows: List[dict]) -> Dict[str, Any]:
    """Summarize merge/split description issues across parsed rows."""
    flagged = []
    for i, row in enumerate(rows):
        issues = description_merge_issues(str(row.get("description") or ""))
        if issues:
            flagged.append(
                {
                    "index": i,
                    "issues": issues,
                    "description": (row.get("description") or "")[:160],
                    "amount": row.get("amount"),
                }
            )
    return {
        "rows_checked": len(rows),
        "suspect_rows": len(flagged),
        "examples": flagged[:20],
    }


def parse_capital_section(text: str, profile: dict) -> Tuple[List[dict], dict]:
    """Parse Capital Expenditure by Project lines from pdftotext -layout output.

    Row boundary rules:
      - Emit when location + amounts are present with an admin code on the same
        line OR carried forward from a nearby prior admin-only line.
      - Never fold a subsequent project title into the current row; stop peek
        when a new left-column project title appears.
      - Title wraps start with lowercase / and|of|within… or sit under the
        project column without a new verb head.
    """
    starts = (profile.get("section_markers") or {}).get("start") or [
        "Capital Expenditure by Project"
    ]
    stops = (profile.get("section_markers") or {}).get("stop") or []

    lines = text.splitlines()
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

    header_blob = "\n".join(lines[start_idx : start_idx + 15])
    columns_detected = _detect_columns_present(header_blob, profile)
    amount_col = profile.get("amount_column") or "approved_2026"

    rows: List[dict] = []
    pending_desc: List[str] = []
    # Admin carried from a prior line that had 12-digit code but no location yet
    pending_admin: Optional[Dict[str, Any]] = None

    def _emit(desc_parts, mda_code, mda_name, loc_code, loc_name, yoy, eco_code):
        description = _join_desc(desc_parts)
        if not description or len(description) < 5:
            return
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
                "expenditure_code": eco_code,
                "economic_code": eco_code,
                "function_code": None,
                "location_code": loc_code,
                "actuals_2024": yoy["actuals_2024"],
                "budget_2025": yoy["budget_2025"],
                "performance_2025": yoy["performance_2025"],
                "budget_2026": yoy["budget_2026"],
            }
        )

    i = start_idx
    while i < len(lines):
        line = lines[i]
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

        # Patch last row / pending admin with indented MDA name wraps
        if _is_mda_name_wrap_line(line):
            wrap = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0].strip()
            if pending_admin and pending_admin.get("name"):
                if wrap.lower() not in pending_admin["name"].lower():
                    pending_admin["name"] = f"{pending_admin['name']} {wrap}".strip()
            elif rows and rows[-1].get("mda_name"):
                name = rows[-1]["mda_name"]
                if wrap.lower() not in name.lower():
                    rows[-1]["mda_name"] = f"{name} {wrap}".strip()
                    rows[-1]["ministry"] = rows[-1]["mda_name"]
            i += 1
            continue

        if has_admin:
            code, name = _parse_admin(line)
            pending_admin = {
                "code": code,
                "name": name or "",
                "line": i,
            }

        loc_code, loc_name = _parse_location(line) if has_loc else (None, None)
        yoy = _parse_yoy_amounts(line) if has_loc else None

        # Effective admin: same-line, or carried forward from a nearby admin-only line
        mda_code = mda_name = None
        if has_admin and has_loc:
            mda_code, mda_name = _parse_admin(line)
        elif has_loc and pending_admin and (i - pending_admin["line"]) <= 4:
            mda_code = pending_admin.get("code")
            mda_name = pending_admin.get("name") or None

        is_anchor = bool(mda_code and has_loc)

        if is_anchor:
            left = _left_text_before_admin(line)
            desc_parts: List[str] = []
            consume_pending = False
            if pending_desc and _pending_belongs_with_anchor(pending_desc, left or ""):
                desc_parts.extend(pending_desc)
                consume_pending = True
            elif pending_desc:
                p0 = pending_desc[0].strip()
                # Only glue wrap-tails onto the previous row; hold real titles
                if _CONTINUATION_START_RE.match(p0) or (p0[:1].islower() if p0 else False):
                    _attach_orphan_pending_to_last(rows, pending_desc)
                    consume_pending = True
            if consume_pending:
                pending_desc = []
            if left and not _MDA_NAME_WRAP_RE.match(left):
                desc_parts.append(left)

            eco_m = ECONOMIC_ON_LINE_RE.search(line)
            eco_code = eco_m.group(1) if eco_m else None
            yoy = yoy or {
                "actuals_2024": None,
                "budget_2025": None,
                "performance_2025": None,
                "budget_2026": None,
                "amount": None,
            }

            # Peek ONLY true wraps — stop at next project title / next structural row
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if _is_skip_line(nxt, profile):
                    break
                if any(s in nxt for s in starts) or any(s in nxt for s in stops):
                    break
                nxt_admin = bool(ADMIN_ON_LINE_RE.search(nxt))
                nxt_loc = bool(LOCATION_ON_LINE_RE.search(nxt))
                if nxt_admin and nxt_loc:
                    break
                if nxt_loc and not nxt_admin:
                    # Next location row = next project (admin may be carried)
                    break
                if nxt_admin and not nxt_loc:
                    # Admin-only prelude of the next project
                    break
                if _is_mda_name_wrap_line(nxt):
                    wrap = nxt.strip()
                    wrap_head = re.split(r"\s{2,}", wrap, maxsplit=1)[0].strip()
                    if mda_name and wrap_head.lower() not in mda_name.lower():
                        mda_name = f"{mda_name} {wrap_head}".strip()
                    j += 1
                    continue
                if _is_right_column_noise(nxt, profile):
                    j += 1
                    continue
                if _is_fragment_line(nxt, profile):
                    j += 1
                    continue
                if _looks_like_new_project_title(nxt):
                    break
                # Capitalized orphan titles are a new project unless the current
                # title clearly continues mid-phrase onto the next line.
                if _looks_like_orphan_project_title(nxt) and not _title_looks_incomplete(
                    " ".join(desc_parts)
                ):
                    break
                if _looks_like_desc_continuation(nxt):
                    cont = _left_text_before_admin(nxt)
                    if (
                        cont
                        and not ECONOMIC_ON_LINE_RE.search(cont)
                        and not FUNCTION_ON_LINE_RE.search(cont)
                        and not FRAGMENT_LEFT_RE.match(cont)
                        and not _MDA_NAME_WRAP_RE.match(cont)
                    ):
                        desc_parts.append(cont)
                    j += 1
                    continue
                break

            _emit(desc_parts, mda_code, mda_name, loc_code, loc_name, yoy, eco_code)
            # Keep admin context for immediately following title wraps of same MDA
            if pending_admin and pending_admin.get("code") == mda_code:
                pending_admin["name"] = mda_name or pending_admin.get("name")
            i = j
            continue

        # Non-anchor: accumulate project-title lines until the structural row
        if _is_fragment_line(line, profile):
            i += 1
            continue
        if _is_right_column_noise(line, profile) and not has_admin:
            i += 1
            continue
        left = _left_text_before_admin(line)
        if left and not FRAGMENT_LEFT_RE.match(left) and not ECONOMIC_ON_LINE_RE.match(left.strip()):
            if _looks_like_orphan_project_title(line) or _looks_like_new_project_title(line):
                # If we still have an unused wrap-tail, attach it to the last row first
                if pending_desc and rows and (
                    _CONTINUATION_START_RE.match(pending_desc[0].strip())
                    or pending_desc[0].strip()[:1].islower()
                ):
                    _attach_orphan_pending_to_last(rows, pending_desc)
                pending_desc = [left]
            elif _looks_like_desc_continuation(line) or (
                _line_indent(line) < _PROJECT_COL_MAX_INDENT
                and not ADMIN_ON_LINE_RE.search(line)
                and len(left) >= 5
            ):
                pending_desc.append(left)
        i += 1

    # Trailing wrap after last emit
    if pending_desc:
        _attach_orphan_pending_to_last(rows, pending_desc)

    meta = {
        "section_found": True,
        "columns_detected": columns_detected,
        "amount_column_used": amount_col,
        "amount_column_label": "2026 Approved Budget",
        "rows_parsed": len(rows),
        "profile_id": profile.get("id"),
        "jurisdiction": profile.get("jurisdiction"),
    }
    quality = assess_parse_quality(rows)
    meta["parse_quality"] = quality
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
