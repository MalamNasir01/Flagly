"""
format_detect.py — Classify uploaded budget PDFs by structural signals.

Returns one of:
  federal_fgn  — Federal Appropriation Bill (Format C project-level or Format A MDA summary)
  state_niger  — Niger State approved budget (profile-driven)
  unknown      — not supported yet

Detection uses document text / layout signals only — never the filename.
"""

from __future__ import annotations

import io
import json
import os
import re
from typing import Optional, Tuple

FORMAT_FEDERAL = "federal_fgn"
FORMAT_STATE_NIGER = "state_niger"
FORMAT_UNKNOWN = "unknown"

SUPPORTED_FORMATS = (FORMAT_FEDERAL, FORMAT_STATE_NIGER)


class UnsupportedBudgetFormat(ValueError):
    """Raised when the uploaded file does not match a supported budget layout."""

    def __init__(self, message: Optional[str] = None):
        super().__init__(
            message
            or (
                "This budget format is not supported yet. Flagly currently supports "
                "Federal FGN appropriation bills and Niger State approved budgets."
            )
        )


def _profiles_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "state_formats", "profiles")


def _load_niger_profile() -> dict:
    path = os.path.join(_profiles_dir(), "niger_v1.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _peek_pdf_text(contents: bytes, max_pages: int = 25) -> str:
    """Best-effort text from early pages (pdfplumber), else empty."""
    try:
        import pdfplumber
    except ImportError:
        return ""
    chunks = []
    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            for page in pdf.pages[:max_pages]:
                chunks.append(page.extract_text() or "")
    except Exception:
        return ""
    return "\n".join(chunks)


def _detect_federal_c(contents: bytes) -> bool:
    """Reuse the federal Format C detector without altering it."""
    try:
        import pdfplumber
        from engines.parser import _detect_format_c
    except Exception:
        return False
    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            return bool(_detect_format_c(pdf))
    except Exception:
        return False


def _detect_federal_a_signals(text: str) -> bool:
    """Positive Format A / federal title signals (structural, not filename)."""
    if not text:
        return False
    upper = text.upper()
    if "FEDERAL GOVERNMENT OF NIGERIA" in upper and "APPROPRIATION" in upper:
        return True
    if "APPROPRIATION BILL" in upper and re.search(r"\bERGP\d{8,}", upper):
        return True
    # Classic MDA-summary header row fragments
    if re.search(r"\bMDA\b", upper) and re.search(r"\bPERSONNEL\b", upper) and re.search(
        r"\bCAPITAL\b", upper
    ):
        if "NIGER STATE GOVERNMENT" not in upper:
            return True
    return False


def _detect_state_niger(text: str, profile: Optional[dict] = None) -> bool:
    profile = profile or _load_niger_profile()
    det = profile.get("detection") or {}
    required = det.get("required_phrases_any") or []
    supporting = det.get("supporting_phrases_any") or []
    if not any(p in text for p in required):
        return False
    if supporting and not any(p in text for p in supporting):
        return False
    loc_re = re.compile(det.get("location_code_re") or r"\b12[0-9]{6}\s*-\s*[A-Z]{2,}")
    admin_re = re.compile(det.get("admin_code_re") or r"\b\d{12}\b")
    min_hits = int(det.get("min_location_hits_on_page") or 3)
    if len(loc_re.findall(text)) >= min_hits:
        return True
    # Strong title + capital project section
    if "Capital Expenditure by Project" in text:
        return True
    # Early summary pages: Niger title + many 12-digit administrative codes
    if len(admin_re.findall(text)) >= 8:
        return True
    return False


def detect_budget_format(contents: bytes) -> str:
    """Return federal_fgn | state_niger | unknown."""
    if _detect_federal_c(contents):
        return FORMAT_FEDERAL

    profile = _load_niger_profile()
    sample_pages = int((profile.get("detection") or {}).get("sample_pages") or 25)
    text = _peek_pdf_text(contents, max_pages=sample_pages)

    if _detect_state_niger(text, profile):
        return FORMAT_STATE_NIGER

    if _detect_federal_a_signals(text):
        return FORMAT_FEDERAL

    return FORMAT_UNKNOWN


def detect_budget_format_with_reason(contents: bytes) -> Tuple[str, str]:
    """Same as detect_budget_format, plus a short reason string for logs/API."""
    fmt = detect_budget_format(contents)
    reasons = {
        FORMAT_FEDERAL: "Matched federal Appropriation Bill structure (Format C or A).",
        FORMAT_STATE_NIGER: "Matched Niger State approved-budget structure.",
        FORMAT_UNKNOWN: "No supported federal or Niger State layout signals found.",
    }
    return fmt, reasons[fmt]
