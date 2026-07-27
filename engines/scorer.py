"""
scorer.py — Composite risk scoring engine for Flagly

Score 0 to 100 combining:
  - Distinct signal groups (overlapping flags in one group count once)
  - Severity of the strongest flag in each group (HIGH weight 3, MEDIUM weight 1)
  - Amount weighting (scaled log of naira amount)

Bands: above 60 HIGH, above 25 MEDIUM, below 25 LOW.

Results are ranked (rank=1 is highest score). A shortlist of top N is available
via score_items(..., top_n=...) / get_ranked_shortlist.
"""

import math
from typing import List, Dict, Optional, Tuple

# Default overlap groups — location / price stacks must not triple-count.
_DEFAULT_OVERLAP_GROUPS = {
    'location_quality': ['MISSING_LOCATION', 'VAGUE_LOCATION', 'VAGUE_HIGH_VALUE', 'VAGUE_HIGH_VALUE_SPEND'],
    'price_anomaly': ['INFLATED_AMOUNT', 'INFLATED_PROJECTION', 'CONTEXT_MISMATCH'],
    'duplication': ['DUPLICATE_CLUSTER', 'COMPOSITE_DUPLICATE', 'BUDGET_SPLITTING'],
    'mandate': ['MANDATE_MISMATCH'],
    'ghost': ['GHOST_PROJECT', 'PHANTOM_SPENDING', 'ZERO_ROLLOVER'],
    'overhead': ['OVERHEAD_DOMINANCE'],
    'approved_amount': ['BLANK_APPROVED_AMOUNT'],
}

_SEV_WEIGHT = {'HIGH': 3, 'MEDIUM': 1, 'LOW': 0}


def _safe_amount(val) -> float:
    try:
        f = float(val or 0)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return max(0.0, f)
    except (TypeError, ValueError):
        return 0.0


def _scoring_config() -> dict:
    try:
        from engines.classifier import get_flag_config
        return (get_flag_config().get('scoring') or {})
    except Exception:
        return {}


def _overlap_groups() -> Dict[str, List[str]]:
    cfg = _scoring_config().get('overlap_groups') or _DEFAULT_OVERLAP_GROUPS
    return {k: list(v) for k, v in cfg.items()}


def _flag_type_to_group(flag_type: str, groups: Dict[str, List[str]]) -> str:
    ft = (flag_type or '').upper()
    for group_name, types in groups.items():
        if ft in {t.upper() for t in types}:
            return group_name
    # Unknown flag types each form their own singleton group
    return f'solo:{ft}' if ft else 'solo:unknown'


def collapse_flags_for_scoring(flags: List[Dict]) -> List[Dict]:
    """Keep the highest-severity flag per overlap group for scoring only.

    All original flags remain on the item for display/evidence; this list is
    used solely to compute risk_score so INFLATED+MISSING+VAGUE do not triple-count.
    """
    groups = _overlap_groups()
    best: Dict[str, Tuple[int, Dict]] = {}
    for f in flags or []:
        ft = (f.get('flag_type') or '').upper()
        sev = (f.get('severity') or '').upper()
        weight = _SEV_WEIGHT.get(sev, 0)
        group = _flag_type_to_group(ft, groups)
        prev = best.get(group)
        if prev is None or weight > prev[0]:
            best[group] = (weight, f)
        elif weight == prev[0]:
            # Stable tie-break: keep first seen
            pass
    return [pair[1] for pair in best.values()]


def score_item(item: Dict) -> Dict:
    """Score a single flagged item with the composite formula (deduped signals)."""
    all_flags = item.get('flags', []) or []
    scoring_flags = collapse_flags_for_scoring(all_flags)
    amount = _safe_amount(item.get('amount'))

    severity_points = 0
    for f in scoring_flags:
        sev = (f.get('severity') or '').upper()
        severity_points += _SEV_WEIGHT.get(sev, 0)

    signal_count = len(scoring_flags)
    # Severity block: up to 55 points — based on distinct signals, not raw flag count
    severity_score = min(55, severity_points * 8 + max(0, signal_count - 1) * 5)

    # Amount weighting: log10 scale, up to 35 points
    if amount <= 0:
        amount_score = 0
    else:
        amount_score = min(35, max(0, (math.log10(amount) - 5) * 7))

    base = 10 if scoring_flags else 0
    score = int(round(min(100, base + severity_score + amount_score)))

    if score > 60:
        risk_level = 'HIGH'
    elif score > 25:
        risk_level = 'MEDIUM'
    else:
        risk_level = 'LOW'

    item['risk_score'] = score
    item['risk_level'] = risk_level
    item['scoring_signal_count'] = signal_count
    item['raw_flag_count'] = len(all_flags)
    return item


def score_items(items: List[Dict], top_n: Optional[int] = None) -> List[Dict]:
    """Score all flagged items, sort by score descending, and assign ranks.

    ``top_n`` defaults to flag_config.scoring.top_n (50). The full ranked list is
    returned; each item gets ``rank`` (1 = highest). Items beyond top_n get
    ``on_shortlist: False``.
    """
    scored = [score_item(item) for item in items]
    scored = [i for i in scored if i.get('flags')]
    scored.sort(
        key=lambda r: (
            r.get('risk_score') or 0,
            _safe_amount(r.get('amount')),
            str(r.get('description') or ''),
        ),
        reverse=True,
    )
    cfg_n = _scoring_config().get('top_n', 50)
    n = int(top_n if top_n is not None else cfg_n)
    for idx, item in enumerate(scored, start=1):
        item['rank'] = idx
        item['on_shortlist'] = idx <= n
    return scored


def get_ranked_shortlist(items: List[Dict], top_n: Optional[int] = None) -> List[Dict]:
    """Return only the top-N shortlist from an already-scored (or unscored) list."""
    scored = score_items(items, top_n=top_n)
    return [i for i in scored if i.get('on_shortlist')]
