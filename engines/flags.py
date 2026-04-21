"""
flags.py — Red-flag detection engine for UDEME Budget Scanner
Flag types:
  INFLATED_AMOUNT, CONTEXT_MISMATCH, MISSING_LOCATION,
  DUPLICATE_CLUSTER, GHOST_PROJECT,
  VAGUE_LOCATION, BUDGET_SPLITTING, MANDATE_MISMATCH, OVERHEAD_DOMINANCE
"""

import re
import math
from typing import List, Dict, Optional
from rapidfuzz import fuzz


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_null_amount(val) -> bool:
    if val is None:
        return True
    try:
        f = float(val)
        return math.isnan(f) or math.isinf(f)
    except (TypeError, ValueError):
        return True


def _fmt_amount(val) -> str:
    if _is_null_amount(val):
        return 'an unspecified amount'
    return f'NGN {float(val):,.0f}'


# ─── Category benchmarks ──────────────────────────────────────────────────────

CATEGORY_BENCHMARKS = [
    (['road', 'highway', 'carriageway'], 3_000_000_000),
    (['bridge'],                          3_000_000_000),
    (['school', 'classroom'],              600_000_000),
    (['hospital', 'health centre'],      3_000_000_000),
    (['clinic', 'primary health'],         600_000_000),
    (['borehole', 'water supply'],          60_000_000),
    (['toilet', 'sanitation'],              30_000_000),
    (['renovation', 'remodel'],            300_000_000),
    (['furniture'],                        150_000_000),
    (['vehicle', 'equipment'],             150_000_000),
    (['training', 'capacity'],              60_000_000),
    (['printing', 'publication'],           30_000_000),
    (['consultancy', 'study'],             300_000_000),
    (['empowerment', 'grant'],             300_000_000),
]

FALLBACK_THRESHOLD = 1_000_000_000

PHYSICAL_PROJECT_KEYWORDS = [
    'construction', 'rehabilitation', 'renovation', 'procurement', 'supply',
    'establishment', 'provision', 'installation', 'repair',
]


def _match_category(description: str):
    if not description:
        return None
    desc_lower = description.lower()
    for keywords, threshold in CATEGORY_BENCHMARKS:
        for kw in keywords:
            if kw in desc_lower:
                return (keywords[0], threshold)
    return None


def _has_physical_project_keyword(description: str) -> bool:
    desc_lower = (description or '').lower()
    return any(kw in desc_lower for kw in PHYSICAL_PROJECT_KEYWORDS)


# ─── Flag 1: INFLATED_AMOUNT ──────────────────────────────────────────────────

def flag_inflated_amount(row: Dict) -> Optional[Dict]:
    if row.get('is_mda_level'):
        return None
    amount = row.get('amount')
    description = row.get('description', '') or ''
    if _is_null_amount(amount):
        return None
    amount = float(amount)

    cat = _match_category(description)
    if cat:
        label, threshold = cat
        if amount > threshold:
            severity = 'HIGH' if amount > 1_000_000_000 else 'MEDIUM'
            return {
                'flag_type': 'INFLATED_AMOUNT',
                'severity': severity,
                'title': 'Inflated Amount',
                'explanation': (
                    f'Amount of {_fmt_amount(amount)} exceeds typical benchmark of '
                    f'NGN {threshold:,.0f} for {label} projects. '
                    f'Verify this is not split funding or inflated.'
                ),
            }
    else:
        if amount > FALLBACK_THRESHOLD:
            return {
                'flag_type': 'INFLATED_AMOUNT',
                'severity': 'HIGH',
                'title': 'Inflated Amount',
                'explanation': (
                    f'Amount of {_fmt_amount(amount)} exceeds ₦1,000,000,000 with no '
                    f'recognised category match. Verify this is not split funding or inflated.'
                ),
            }
    return None


# ─── Flag 2: CONTEXT_MISMATCH ─────────────────────────────────────────────────

def flag_context_mismatch(row: Dict) -> Optional[Dict]:
    if row.get('is_mda_level'):
        return None
    amount = row.get('amount')
    description = row.get('description', '') or ''
    if _is_null_amount(amount):
        return None
    amount = float(amount)
    if amount >= 1_000_000_000:
        return None

    cat = _match_category(description)
    if not cat:
        return None

    label, threshold = cat
    if amount > threshold * 3:
        return {
            'flag_type': 'CONTEXT_MISMATCH',
            'severity': 'MEDIUM',
            'title': 'Context Mismatch',
            'explanation': (
                f'Amount is disproportionate for the item category even though it falls '
                f'below the ₦1B threshold. {_fmt_amount(amount)} for a {label} project is unusual.'
            ),
        }
    return None


# ─── Flag 3: MISSING_LOCATION ─────────────────────────────────────────────────

BROAD_VALID_LOCATION_RE = re.compile(
    r'\b(STATE\s+WIDE|NATIONWIDE|ACROSS\s+THE\s+STATE)\b',
    re.IGNORECASE,
)


def flag_missing_location(row: Dict) -> Optional[Dict]:
    if row.get('is_mda_level'):
        return None
    description = row.get('description', '') or ''
    if not _has_physical_project_keyword(description):
        return None

    location = row.get('location')
    amount = row.get('amount')
    if _is_null_amount(amount) or float(amount) <= 5_000_000:
        return None
    amount = float(amount)

    loc_str = str(location).strip() if location else ''
    loc_lower = loc_str.lower()

    if BROAD_VALID_LOCATION_RE.search(loc_str) or BROAD_VALID_LOCATION_RE.search(description):
        return None

    if loc_str and len(loc_str) > 3:
        return None

    severity = 'HIGH' if amount > 100_000_000 else 'MEDIUM'
    return {
        'flag_type': 'MISSING_LOCATION',
        'severity': severity,
        'title': 'Missing Location',
        'explanation': (
            f'No state, LGA, ward or constituency is attached to this item worth '
            f'{_fmt_amount(amount)}. Without a location there is no way to verify '
            f'delivery or hold anyone accountable.'
        ),
    }


# ─── Flag 4: DUPLICATE_CLUSTER ────────────────────────────────────────────────

DUPLICATE_ACTION_VERBS = {
    'construction', 'rehabilitation', 'renovation', 'procurement', 'supply',
    'provision', 'installation', 'repair', 'purchase', 'training',
    'establishment', 'development', 'remodelling', 'equipping', 'furnishing',
    'completion',
}


def _has_action_verb(description: str) -> bool:
    desc_lower = description.lower()
    return any(v in desc_lower for v in DUPLICATE_ACTION_VERBS)


def flag_duplicates(rows: List[Dict]) -> List[Dict]:
    """
    Group rows by description similarity ≥95% into clusters.
    Only compare descriptions ≥40 chars with at least one action verb.
    Flag D enhancement: if cluster members have amounts within 10% of each other
    AND all under the same MDA, upgrade severity to HIGH and note systematic splitting.
    """
    candidates = [
        r for r in rows
        if r.get('description')
        and len(r['description']) >= 40
        and not r['description'][0].isdigit()
        and _has_action_verb(r['description'])
    ]

    visited = set()
    clusters = []

    for i, row_a in enumerate(candidates):
        if id(row_a) in visited:
            continue
        cluster = [row_a]
        visited.add(id(row_a))
        for j, row_b in enumerate(candidates):
            if i == j or id(row_b) in visited:
                continue
            if fuzz.ratio(row_a['description'], row_b['description']) >= 95:
                cluster.append(row_b)
                visited.add(id(row_b))
        if len(cluster) >= 2:
            clusters.append(cluster)

    for cluster in clusters:
        n = len(cluster)
        cluster_row_ids = [r.get('row_id') for r in cluster]

        # Flag D: detect systematic splitting
        amounts_raw = [r.get('amount') for r in cluster]
        all_have_amount = all(not _is_null_amount(a) for a in amounts_raw)
        amounts = [float(a) for a in amounts_raw if not _is_null_amount(a)]
        mdas = {(r.get('ministry') or '').strip() for r in cluster}
        all_same_mda = len(mdas) == 1 and any(mdas)

        amounts_close = False
        if all_have_amount and len(amounts) >= 2:
            mean_amt = sum(amounts) / len(amounts)
            if mean_amt > 0:
                max_diff = max(abs(a - mean_amt) / mean_amt for a in amounts)
                amounts_close = max_diff <= 0.10

        is_systematic = amounts_close and all_same_mda and n >= 3

        explanation = f'This project description appears {n} times across the budget. '
        if is_systematic:
            explanation += (
                'Identical amounts suggest systematic splitting rather than coincidental '
                'duplication. '
            )
        explanation += (
            'Verify these are genuinely separate projects at different locations '
            'and not the same allocation duplicated.'
        )

        members_with_amount = [r for r in cluster if not _is_null_amount(r.get('amount'))]
        if members_with_amount:
            representative = max(members_with_amount, key=lambda r: float(r['amount']))
        else:
            representative = max(cluster, key=lambda r: len(r.get('description') or ''))

        flag = {
            'flag_type': 'DUPLICATE_CLUSTER',
            'cluster_size': n,
            'matched_rows': cluster_row_ids,
            'severity': 'HIGH' if (is_systematic or n > 5) else 'MEDIUM',
            'title': f'Duplicate Cluster ({n}x)',
            'explanation': explanation,
        }

        representative.setdefault('_flags', []).append(flag)
        representative['cluster_size'] = n

        for member in cluster:
            if member is not representative:
                member['_exclude'] = True

    return rows


# ─── Flag 5: GHOST_PROJECT ────────────────────────────────────────────────────

YEAR_RE = re.compile(
    r'(?<!\d)(201[0-9]|202[0-4])(?!\d)'
    r'(?=\s*[\)\-]|\s+(?:BUDGET|APPROPRIATION|FY|FISCAL|BATCH|EDITION|PHASE|TRANCHE|CONTRACT))',
    re.IGNORECASE,
)


def flag_ghost_project(row: Dict, all_descriptions: List[str], budget_year: Optional[str]) -> Optional[Dict]:
    if not budget_year:
        return None
    try:
        by = int(budget_year)
    except ValueError:
        return None

    amount = row.get('amount')
    if not _is_null_amount(amount) and float(amount) < 1_000_000:
        return None

    description = row.get('description', '') or ''
    years_in_desc = [int(m) for m in YEAR_RE.findall(description)]
    stale_years = [y for y in years_in_desc if by - y >= 2]
    if not stale_years:
        return None

    for other_desc in all_descriptions:
        if other_desc == description:
            continue
        if fuzz.ratio(description, other_desc) >= 95:
            other_years = [int(m) for m in YEAR_RE.findall(other_desc)]
            if any(by - y >= 2 for y in other_years):
                return {
                    'flag_type': 'GHOST_PROJECT',
                    'severity': 'MEDIUM',
                    'title': 'Ghost Project',
                    'explanation': (
                        f'This description closely matches a project from '
                        f'{min(stale_years)} — 2+ years before the {budget_year} budget. '
                        f'Verify this is not a recycled or ghost allocation.'
                    ),
                }

    return {
        'flag_type': 'GHOST_PROJECT',
        'severity': 'MEDIUM',
        'title': 'Ghost Project',
        'explanation': (
            f'This description references {min(stale_years)} — 2+ years before the '
            f'{budget_year} budget. Verify this is not a recycled or ghost allocation.'
        ),
    }


# ─── Flag A: VAGUE_LOCATION ───────────────────────────────────────────────────

VAGUE_LOCATION_PHRASES = [
    'selected locations', 'multiple locations', 'multiple lots',
    'various locations', 'selected states', 'various states',
    'nationwide', 'across the country', 'various places', 'multiple sites',
    'different locations', 'different states', 'selected areas',
]


def flag_vague_location(row: Dict) -> Optional[Dict]:
    if row.get('is_mda_level'):
        return None
    description = (row.get('description') or '').lower()
    amount = row.get('amount')

    matched = next((p for p in VAGUE_LOCATION_PHRASES if p in description), None)
    if not matched:
        return None

    severity = 'MEDIUM'
    if not _is_null_amount(amount) and float(amount) > 1_000_000_000:
        severity = 'HIGH'

    return {
        'flag_type': 'VAGUE_LOCATION',
        'severity': severity,
        'title': 'Non-Traceable Location',
        'explanation': (
            f'Project description uses vague location language ("{matched}"). '
            f'Without specific locations, implementation cannot be tracked or verified.'
        ),
    }


# ─── Flag B: BUDGET_SPLITTING ─────────────────────────────────────────────────

def flag_budget_splitting(rows: List[Dict]) -> List[Dict]:
    """
    Detect ≥3 items under the same MDA with ≥85% similar descriptions and
    amounts within 5% of each other. Flags ALL members (no exclusion).
    """
    by_mda: Dict[str, List[Dict]] = {}
    for row in rows:
        if row.get('is_mda_level') or row.get('_exclude'):
            continue
        ministry = (row.get('ministry') or '').strip()
        if not ministry:
            continue
        by_mda.setdefault(ministry, []).append(row)

    for ministry, mda_rows in by_mda.items():
        candidates = [
            r for r in mda_rows
            if not _is_null_amount(r.get('amount'))
            and len(r.get('description') or '') >= 20
        ]
        if len(candidates) < 3:
            continue

        visited_split: set = set()

        for i, seed in enumerate(candidates):
            if id(seed) in visited_split:
                continue
            amt_seed = float(seed['amount'])
            if amt_seed == 0:
                continue

            group = [seed]
            for j, other in enumerate(candidates):
                if i == j or id(other) in visited_split:
                    continue
                if fuzz.ratio(seed['description'], other['description']) < 85:
                    continue
                amt_other = float(other['amount'])
                if amt_other == 0:
                    continue
                if abs(amt_seed - amt_other) / max(amt_seed, amt_other) > 0.05:
                    continue
                group.append(other)

            if len(group) >= 3:
                for r in group:
                    visited_split.add(id(r))

                n = len(group)
                avg_amt = sum(float(r['amount']) for r in group) / n
                split_items = [
                    {
                        'code':        r.get('project_code') or '—',
                        'description': (r.get('description') or '')[:70],
                        'amount':      r.get('amount'),
                    }
                    for r in group
                ]
                flag = {
                    'flag_type':  'BUDGET_SPLITTING',
                    'severity':   'HIGH',
                    'title':      'Suspected Budget Splitting',
                    'explanation': (
                        f'Found {n} line items under {ministry} with near-identical descriptions '
                        f'and amounts ({_fmt_amount(avg_amt)} each). This pattern is consistent '
                        f'with project splitting to avoid oversight thresholds.'
                    ),
                    'split_items': split_items,
                }
                for r in group:
                    r.setdefault('_flags', []).append(flag)

    return rows


# ─── Flag C: MANDATE_MISMATCH ─────────────────────────────────────────────────

MANDATE_MAP = {
    'road':        ['road', 'highway', 'bridge', 'transport', 'works', 'infrastructure'],
    'health':      ['health', 'hospital', 'medical', 'clinic', 'pharmaceutical'],
    'education':   ['education', 'school', 'university', 'college', 'training'],
    'water':       ['water', 'irrigation', 'dam', 'sanitation'],
    'agriculture': ['agriculture', 'farm', 'livestock', 'fishery', 'food'],
}


def _classify_sector(text: str) -> Optional[str]:
    if not text:
        return None
    text_lower = text.lower()
    for sector, keywords in MANDATE_MAP.items():
        if any(kw in text_lower for kw in keywords):
            return sector
    return None


def flag_mandate_mismatch(row: Dict) -> Optional[Dict]:
    if row.get('is_mda_level'):
        return None
    ministry = (row.get('ministry') or '').strip()
    description = (row.get('description') or '').strip()
    if not ministry or not description:
        return None

    mda_sector  = _classify_sector(ministry)
    proj_sector = _classify_sector(description)

    if not mda_sector or not proj_sector or mda_sector == proj_sector:
        return None

    return {
        'flag_type': 'MANDATE_MISMATCH',
        'severity':  'MEDIUM',
        'title':     'Possible Mandate Violation',
        'explanation': (
            f'{ministry} is a {mda_sector} agency but this project appears to be a '
            f'{proj_sector} project. Verify this allocation is within the agency\'s mandate.'
        ),
    }


# ─── Flag E: OVERHEAD_DOMINANCE ───────────────────────────────────────────────

def flag_overhead_dominance(row: Dict) -> Optional[Dict]:
    """Only fires for Format A MDA-level rows that have overhead and capital extracted."""
    if not row.get('is_mda_level'):
        return None

    overhead = row.get('overhead_amount')
    capital  = row.get('capital_amount')
    amount   = row.get('amount')

    if _is_null_amount(overhead) or _is_null_amount(capital):
        return None
    if _is_null_amount(amount) or float(amount) <= 10_000_000_000:
        return None

    overhead = float(overhead)
    capital  = float(capital)

    if overhead <= capital:
        return None

    mda_name = row.get('description') or row.get('ministry') or 'This MDA'
    return {
        'flag_type': 'OVERHEAD_DOMINANCE',
        'severity':  'MEDIUM',
        'title':     'Overhead Exceeds Capital Spending',
        'explanation': (
            f'{mda_name} spent more on overhead ({_fmt_amount(overhead)}) than capital '
            f'projects ({_fmt_amount(capital)}). This may indicate administrative costs '
            f'consuming funds meant for project delivery.'
        ),
    }


# ─── Main runner ──────────────────────────────────────────────────────────────

def run_all_flags(df, budget_year: Optional[str] = None) -> List[Dict]:
    """Run all flag checks and return list of flagged item dicts."""
    rows = df.to_dict('records')

    for row in rows:
        row['_flags']   = []
        row['_exclude'] = False

    all_descriptions = [r.get('description', '') or '' for r in rows]

    # Per-row flags
    for row in rows:
        f1 = flag_inflated_amount(row)
        if f1:
            row['_flags'].append(f1)

        f2 = flag_context_mismatch(row)
        if f2:
            row['_flags'].append(f2)

        f3 = flag_missing_location(row)
        if f3:
            row['_flags'].append(f3)

        f5 = flag_ghost_project(row, all_descriptions, budget_year)
        if f5:
            row['_flags'].append(f5)

        fa = flag_vague_location(row)
        if fa:
            row['_flags'].append(fa)

        fc = flag_mandate_mismatch(row)
        if fc:
            row['_flags'].append(fc)

        fe = flag_overhead_dominance(row)
        if fe:
            row['_flags'].append(fe)

    # Batch flags (modify rows in-place)
    rows = flag_duplicates(rows)
    rows = flag_budget_splitting(rows)

    # Build final result: only flagged non-excluded rows
    results = []
    for row in rows:
        if row.get('_exclude'):
            continue
        flags = row.get('_flags', [])
        if not flags:
            continue

        results.append({
            'row_id':       row.get('row_id'),
            'description':  row.get('description'),
            'amount':       row.get('amount'),
            'location':     row.get('location'),
            'ministry':     row.get('ministry'),
            'project_code': row.get('project_code'),
            'is_mda_level': row.get('is_mda_level'),
            'cluster_size': row.get('cluster_size'),
            'flags':        flags,
        })

    return results
