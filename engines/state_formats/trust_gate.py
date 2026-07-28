"""Trust gate for onboarded state formats.

A format may publish results above MEDIUM only when:
  1. parse-quality merge guards are clean (or within baseline tolerance), AND
  2. a frozen baseline snapshot exists for the profile, AND
  3. (for mandate HIGH) the mandates file is reviewed=true.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional


def baselines_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "samples",
    )


def baseline_path_for_profile(profile_id: str) -> str:
    # state_niger → state_niger_baseline.json ; state_kaduna → state_kaduna_baseline.json
    slug = profile_id
    return os.path.join(baselines_dir(), f"{slug}_baseline.json")


def load_baseline(profile_id: str) -> Optional[dict]:
    path = baseline_path_for_profile(profile_id)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_quality_is_clean(
    parse_meta: dict,
    *,
    max_suspects: Optional[int] = None,
    baseline: Optional[dict] = None,
) -> bool:
    """True when merge/split suspects are acceptably low."""
    pq = (parse_meta or {}).get("parse_quality") or {}
    suspects = int(pq.get("suspect_rows") or 0)
    rows = int(pq.get("rows_checked") or (parse_meta or {}).get("rows_parsed") or 0)
    if baseline and baseline.get("parse_quality"):
        # Match locked baseline within a small absolute tolerance
        base_s = int(baseline["parse_quality"].get("suspect_rows") or 0)
        return abs(suspects - base_s) <= 40 and suspects <= max(base_s + 40, 60)
    if max_suspects is not None:
        return suspects <= max_suspects
    # Default: allow a few false positives, fail hard on widespread merges
    if rows <= 0:
        return False
    return suspects <= max(5, int(rows * 0.05))


def evaluate_trust_gate(
    *,
    profile_id: str,
    parse_meta: dict,
    mandates_reviewed: bool,
) -> Dict[str, Any]:
    """Return trust-gate status for UI / scoring caps."""
    baseline = load_baseline(profile_id)
    has_baseline = baseline is not None
    quality_clean = parse_quality_is_clean(parse_meta, baseline=baseline)
    publishable = bool(has_baseline and quality_clean)

    warnings: List[str] = []
    if not has_baseline:
        warnings.append(
            "No frozen baseline snapshot for this format yet — results are provisional."
        )
    if not quality_clean:
        suspects = ((parse_meta or {}).get("parse_quality") or {}).get("suspect_rows")
        warnings.append(
            f"Parse-quality check found {suspects} suspect row(s) "
            "(possible merged/split descriptions). Treat amounts and rankings as low-confidence."
        )
    if not mandates_reviewed:
        warnings.append(
            "MDA mandates are unreviewed — mandate mismatches are capped at MEDIUM "
            "and are not publishable-tier."
        )

    return {
        "publishable": publishable,
        "provisional": not publishable,
        "confidence": "high" if publishable else "low",
        "has_baseline": has_baseline,
        "parse_quality_clean": quality_clean,
        "mandates_reviewed": mandates_reviewed,
        "max_severity": "HIGH" if (publishable and mandates_reviewed) else "MEDIUM",
        "warnings": warnings,
    }


def apply_severity_cap(results: List[dict], max_severity: str) -> List[dict]:
    """Downgrade flag severities above the trust-gate cap (in place)."""
    rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    cap = rank.get((max_severity or "MEDIUM").upper(), 2)
    for row in results:
        for flag in row.get("flags") or []:
            sev = (flag.get("severity") or "").upper()
            if rank.get(sev, 0) > cap:
                flag["severity"] = max_severity.upper()
                flag["severity_capped"] = True
                evidence = flag.get("evidence")
                if isinstance(evidence, dict):
                    evidence["severity_capped_from"] = sev
                    evidence["trust_gate_cap"] = max_severity.upper()
    return results
