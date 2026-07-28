"""
format_detect.py — Classify uploaded budget files by structural signals.

Outcomes:
  federal_fgn              — Federal Appropriation Bill (Format C or A)
  state_<name>             — supported state profile (e.g. state_niger)
  scanned_pdf              — PDF with no usable text layer
  known_unsupported_state  — looks like a Nigerian state budget we don't support yet
  unknown                  — no supported layout signals

Detection uses document text / layout only — never the filename.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from engines.state_formats.registry import list_profiles, load_profile, supported_format_catalog

FORMAT_FEDERAL = "federal_fgn"
FORMAT_STATE_NIGER = "state_niger"  # kept for callers; also a live profile id
FORMAT_SCANNED = "scanned_pdf"
FORMAT_KNOWN_UNSUPPORTED = "known_unsupported_state"
FORMAT_UNKNOWN = "unknown"

# Nigerian state names for "recognized but unsupported" messaging
_NIGERIAN_STATES = [
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue",
    "Borno", "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu",
    "Gombe", "Imo", "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi",
    "Kwara", "Lagos", "Nasarawa", "Niger", "Ogun", "Ondo", "Osun", "Oyo",
    "Plateau", "Rivers", "Sokoto", "Taraba", "Yobe", "Zamfara", "FCT",
]


@dataclass
class DetectionResult:
    format_id: str
    reason: str
    supported: bool
    message: str = ""
    detected_state: Optional[str] = None
    refusal_code: Optional[str] = None  # scanned_pdf | known_unsupported | unknown
    supported_labels: List[str] = field(default_factory=list)


class UnsupportedBudgetFormat(ValueError):
    """Raised when the uploaded file cannot produce a Flagly report."""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        refusal_code: Optional[str] = None,
        detected_state: Optional[str] = None,
        detection: Optional[DetectionResult] = None,
    ):
        self.refusal_code = refusal_code or (detection.refusal_code if detection else None)
        self.detected_state = detected_state or (detection.detected_state if detection else None)
        self.detection = detection
        if message is None and detection is not None:
            message = detection.message
        super().__init__(
            message
            or (
                "This budget format is not supported yet. Flagly currently supports "
                + _supported_list_phrase()
                + "."
            )
        )


def _supported_list_phrase() -> str:
    labels = [c["label"] for c in supported_format_catalog()]
    if not labels:
        return "Federal FGN appropriation bills"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def supported_formats() -> Tuple[str, ...]:
    return tuple(["federal_fgn"] + list_profiles())


SUPPORTED_FORMATS = None  # refreshed lazily; prefer supported_formats()


def _peek_pdf_text(contents: bytes, max_pages: int = 25) -> str:
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


def _pdf_page_count(contents: bytes) -> int:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def _looks_scanned(contents: bytes, text: str) -> bool:
    """True when the PDF has pages but almost no extractable text."""
    pages = _pdf_page_count(contents)
    if pages <= 0:
        return False
    # Strip whitespace; require roughly < 40 chars/page on average from peek
    compact = re.sub(r"\s+", "", text or "")
    if pages >= 3 and len(compact) < max(80, pages * 15):
        return True
    if pages >= 1 and len(compact) < 40:
        return True
    return False


def _detect_federal_c(contents: bytes) -> bool:
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
    if not text:
        return False
    upper = text.upper()
    if "FEDERAL GOVERNMENT OF NIGERIA" in upper and "APPROPRIATION" in upper:
        return True
    if "APPROPRIATION BILL" in upper and re.search(r"\bERGP\d{8,}", upper):
        return True
    if re.search(r"\bMDA\b", upper) and re.search(r"\bPERSONNEL\b", upper) and re.search(
        r"\bCAPITAL\b", upper
    ):
        # Exclude obvious state titles
        if not re.search(r"\b[A-Z][A-Z\s]+\s+STATE\s+GOVERNMENT\b", upper):
            return True
    return False


def _detect_state_from_profile(text: str, profile: dict) -> bool:
    det = profile.get("detection") or {}
    required = det.get("required_phrases_any") or []
    supporting = det.get("supporting_phrases_any") or []
    if required and not any(p in text for p in required):
        return False
    if supporting and not any(p in text for p in supporting):
        return False
    loc_re = re.compile(det.get("location_code_re") or r"\b\d{8}\s*-\s*[A-Z]{2,}")
    admin_re = re.compile(det.get("admin_code_re") or r"\b\d{12}\b")
    min_hits = int(det.get("min_location_hits_on_page") or 3)
    if len(loc_re.findall(text)) >= min_hits:
        return True
    capital_markers = (profile.get("section_markers") or {}).get("start") or []
    if any(m in text for m in capital_markers):
        return True
    if "Capital Expenditure by Project" in text:
        return True
    if len(admin_re.findall(text)) >= 8:
        return True
    return False


def _guess_nigerian_state_name(text: str) -> Optional[str]:
    if not text:
        return None
    # Prefer "X State Government"
    m = re.search(
        r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s+State\s+Government\b",
        text,
        re.I,
    )
    if m:
        name = m.group(1).strip().title()
        # Normalize known aliases
        if name.upper() in {"AKWA IBOM", "CROSS RIVER"}:
            return name.title()
        return name
    upper = text.upper()
    for state in sorted(_NIGERIAN_STATES, key=len, reverse=True):
        if state.upper() == "FCT":
            continue
        if f"{state.upper()} STATE" in upper:
            return state
    return None


# Back-compat helpers used by tests
def _detect_state_niger(text: str, profile: Optional[dict] = None) -> bool:
    profile = profile or load_profile("state_niger")
    return _detect_state_from_profile(text, profile)


def _load_niger_profile() -> dict:
    return load_profile("state_niger")


def classify_budget_file(contents: bytes) -> DetectionResult:
    """Full classification with refusal messaging — preferred entry point."""
    labels = [c["label"] for c in supported_format_catalog()]
    supported_phrase = _supported_list_phrase()

    # Federal Format C first (uses pdfplumber project-code scan)
    if _detect_federal_c(contents):
        return DetectionResult(
            format_id=FORMAT_FEDERAL,
            reason="Matched federal Appropriation Bill structure (Format C).",
            supported=True,
            supported_labels=labels,
        )

    # Peek text for remaining checks
    sample_pages = 80
    text = _peek_pdf_text(contents, max_pages=sample_pages)

    if _looks_scanned(contents, text):
        msg = (
            "This looks like a scanned document; Flagly needs a text-based PDF "
            "or Excel file. OCR support is coming."
        )
        return DetectionResult(
            format_id=FORMAT_SCANNED,
            reason="PDF has pages but little/no extractable text.",
            supported=False,
            message=msg,
            refusal_code="scanned_pdf",
            supported_labels=labels,
        )

    # Try each registered state profile (longest required-phrase first is implicit via scan)
    for pid in list_profiles():
        try:
            profile = load_profile(pid)
        except Exception:
            continue
        det_pages = int((profile.get("detection") or {}).get("sample_pages") or sample_pages)
        peek = text if det_pages <= sample_pages else _peek_pdf_text(contents, max_pages=det_pages)
        if _detect_state_from_profile(peek or text, profile):
            return DetectionResult(
                format_id=pid,
                reason=f"Matched {profile.get('label') or pid} structure.",
                supported=True,
                supported_labels=labels,
                detected_state=(profile.get("jurisdiction") or pid),
            )

    if _detect_federal_a_signals(text):
        return DetectionResult(
            format_id=FORMAT_FEDERAL,
            reason="Matched federal Appropriation Bill structure (Format A).",
            supported=True,
            supported_labels=labels,
        )

    # Looks like a state budget we don't support yet?
    state_name = _guess_nigerian_state_name(text)
    state_ish = bool(state_name) or bool(
        re.search(r"\bSTATE\s+GOVERNMENT\b", text or "", re.I)
        and re.search(r"\b(APPROVED\s+BUDGET|APPROPRIATION)\b", text or "", re.I)
    )
    if state_ish:
        if state_name:
            msg = (
                f"This looks like a {state_name} State budget, which Flagly cannot "
                f"parse yet. Currently supported: {supported_phrase}."
            )
        else:
            msg = (
                "This looks like a Nigerian state budget Flagly cannot parse yet. "
                f"Currently supported: {supported_phrase}."
            )
        return DetectionResult(
            format_id=FORMAT_KNOWN_UNSUPPORTED,
            reason="Recognized state-budget signals for an unsupported jurisdiction.",
            supported=False,
            message=msg,
            refusal_code="known_unsupported",
            detected_state=state_name,
            supported_labels=labels,
        )

    msg = (
        "This budget format is not supported yet. "
        f"Flagly currently supports {supported_phrase}."
    )
    return DetectionResult(
        format_id=FORMAT_UNKNOWN,
        reason="No supported federal or state layout signals found.",
        supported=False,
        message=msg,
        refusal_code="unknown",
        supported_labels=labels,
    )


def detect_budget_format(contents: bytes) -> str:
    """Return format id string (including refusal ids)."""
    return classify_budget_file(contents).format_id


def detect_budget_format_with_reason(contents: bytes) -> Tuple[str, str]:
    result = classify_budget_file(contents)
    return result.format_id, result.reason
