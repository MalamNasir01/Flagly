"""
scorer.py — Risk scoring engine for Flagly
Assigns numeric risk score 0–100 and risk level HIGH/MEDIUM/LOW.
"""

from typing import List, Dict


def score_item(item: Dict) -> Dict:
    """Score a single flagged item."""
    flags = item.get('flags', [])
    amount = item.get('amount') or 0
    location = item.get('location')

    score = 0

    has_high = any(f.get('severity') == 'HIGH' for f in flags)
    has_medium = any(f.get('severity') == 'MEDIUM' for f in flags)

    if has_high:
        score += 40
    if has_medium:
        score += 20

    # Additional flag types beyond the first
    flag_types = set(f.get('flag_type') for f in flags)
    extra = max(0, len(flag_types) - 1)
    score += min(extra * 15, 30)

    if amount > 1_000_000_000:
        score += 10

    loc_str = str(location).strip() if location else ''
    if not loc_str or len(loc_str) <= 3:
        score += 5

    score = min(score, 100)

    if score >= 70:
        risk_level = 'HIGH'
    elif score >= 40:
        risk_level = 'MEDIUM'
    else:
        risk_level = 'LOW'

    item['risk_score'] = score
    item['risk_level'] = risk_level
    return item


def score_items(items: List[Dict]) -> List[Dict]:
    """Score all flagged items. Exclude LOW-risk items with score < 40."""
    scored = [score_item(item) for item in items]
    # Exclude items with only LOW severity flags and score < 40
    filtered = []
    for item in scored:
        flags = item.get('flags', [])
        all_low = all(f.get('severity') == 'LOW' for f in flags)
        if all_low and item.get('risk_score', 0) < 40:
            continue
        filtered.append(item)
    return filtered
