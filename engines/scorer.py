"""
scorer.py — Composite risk scoring engine for Flagly

Score 0 to 100 combining:
  - Number of flags triggered
  - Severity of each flag (HIGH weight 3, MEDIUM weight 1)
  - Amount weighting (scaled log of naira amount)

Bands: above 60 HIGH, above 25 MEDIUM, below 25 LOW.
"""

import math
from typing import List, Dict


def _safe_amount(val) -> float:
    try:
        f = float(val or 0)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return max(0.0, f)
    except (TypeError, ValueError):
        return 0.0


def score_item(item: Dict) -> Dict:
    """Score a single flagged item with the composite formula."""
    flags = item.get('flags', []) or []
    amount = _safe_amount(item.get('amount'))

    severity_points = 0
    for f in flags:
        sev = (f.get('severity') or '').upper()
        if sev == 'HIGH':
            severity_points += 3
        elif sev == 'MEDIUM':
            severity_points += 1

    flag_count = len(flags)
    # Severity block: up to 55 points
    severity_score = min(55, severity_points * 8 + max(0, flag_count - 1) * 5)

    # Amount weighting: log10 scale, up to 35 points
    if amount <= 0:
        amount_score = 0
    else:
        # 1e6 → ~3, 1e9 → ~9, 1e10 → ~10
        amount_score = min(35, max(0, (math.log10(amount) - 5) * 7))

    # Base presence of any flag
    base = 10 if flags else 0

    score = int(round(min(100, base + severity_score + amount_score)))

    if score > 60:
        risk_level = 'HIGH'
    elif score > 25:
        risk_level = 'MEDIUM'
    else:
        risk_level = 'LOW'

    item['risk_score'] = score
    item['risk_level'] = risk_level
    return item


def score_items(items: List[Dict]) -> List[Dict]:
    """Score all flagged items and sort by score descending."""
    scored = [score_item(item) for item in items]
    # Keep LOW items so the UI toggle can reveal them; filter only empty flag sets
    scored = [i for i in scored if i.get('flags')]
    scored.sort(key=lambda r: (r.get('risk_score') or 0), reverse=True)
    return scored
