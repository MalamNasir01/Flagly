"""
flags.py — Red-flag detection engine for UDEME Budget Scanner
Five flag types: INFLATED_AMOUNT, CONTEXT_MISMATCH, MISSING_LOCATION,
                  DUPLICATE_CLUSTER, GHOST_PROJECT
"""

import re
from typing import List, Dict, Optional
from rapidfuzz import fuzz


# ─── Category benchmarks ────────────────────────────────────────────────────

CATEGORY_BENCHMARKS = [
    (['road', 'highway', 'carriageway'], 3_000_000_000),
    (['bridge'], 3_000_000_000),
    (['school', 'classroom'], 600_000_000),
    (['hospital', 'health centre'], 3_000_000_000),
    (['clinic', 'primary health'], 600_000_000),
    (['borehole', 'water supply'], 60_000_000),
    (['toilet', 'sanitation'], 30_000_000),
    (['renovation', 'remodel'], 300_000_000),
    (['furniture'], 150_000_000),
    (['vehicle', 'equipment'], 150_000_000),
    (['training', 'capacity'], 60_000_000),
    (['printing', 'publication'], 30_000_000),
    (['consultancy', 'study'], 300_000_000),
    (['empowerment', 'grant'], 300_000_000),
]

FALLBACK_THRESHOLD = 1_000_000_000


def _match_category(description: str):
    """Return (keywords_label, threshold) or None if no match."""
    if not description:
        return None
    desc_lower = description.lower()
    for keywords, threshold in CATEGORY_BENCHMARKS:
        for kw in keywords:
            if kw in desc_lower:
                return (keywords[0], threshold)
    return None


# ─── Flag 1: INFLATED_AMOUNT ─────────────────────────────────────────────────

def flag_inflated_amount(row: Dict) -> Optional[Dict]:
    amount = row.get('amount')
    description = row.get('description', '') or ''
    if amount is None:
        return None

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
                    f'Amount of NGN {amount:,.0f} exceeds typical benchmark of NGN {threshold:,.0f} '
                    f'for {label} projects. Verify this is not split funding or inflated.'
                ),
            }
    else:
        # No category match
        if amount > FALLBACK_THRESHOLD:
            return {
                'flag_type': 'INFLATED_AMOUNT',
                'severity': 'HIGH',
                'title': 'Inflated Amount',
                'explanation': (
                    f'Amount of NGN {amount:,.0f} exceeds ₦1,000,000,000 with no recognised category match. '
                    f'Verify this is not split funding or inflated.'
                ),
            }
    return None


# ─── Flag 2: CONTEXT_MISMATCH ────────────────────────────────────────────────

def flag_context_mismatch(row: Dict) -> Optional[Dict]:
    amount = row.get('amount')
    description = row.get('description', '') or ''
    if amount is None or amount >= 1_000_000_000:
        return None  # INFLATED_AMOUNT handles ≥1B

    cat = _match_category(description)
    if not cat:
        return None

    label, threshold = cat
    # Fire at 3x threshold but only when amount < 1B
    if amount > threshold * 3:
        return {
            'flag_type': 'CONTEXT_MISMATCH',
            'severity': 'MEDIUM',
            'title': 'Context Mismatch',
            'explanation': (
                f'Amount is disproportionate for the item category even though it falls below the ₦1B threshold. '
                f'NGN {amount:,.0f} for a {label} project is unusual.'
            ),
        }
    return None


# ─── Flag 3: MISSING_LOCATION ────────────────────────────────────────────────

SKIP_LOCATIONS = {'state wide', 'various', 'national', 'nationwide', 'federal'}


def flag_missing_location(row: Dict) -> Optional[Dict]:
    if row.get('is_mda_level'):
        return None

    location = row.get('location')
    amount = row.get('amount')
    if amount is None or amount <= 5_000_000:
        return None

    loc_str = str(location).strip() if location else ''
    loc_lower = loc_str.lower()

    if loc_str and len(loc_str) > 3 and loc_lower not in SKIP_LOCATIONS:
        return None  # Has valid location

    severity = 'HIGH' if amount > 100_000_000 else 'MEDIUM'
    return {
        'flag_type': 'MISSING_LOCATION',
        'severity': severity,
        'title': 'Missing Location',
        'explanation': (
            f'No state, LGA, ward or constituency is attached to this item worth NGN {amount:,.0f}. '
            f'Without a location there is no way to verify delivery or hold anyone accountable.'
        ),
    }


# ─── Flag 4: DUPLICATE_CLUSTER ───────────────────────────────────────────────

def flag_duplicates(rows: List[Dict]) -> List[Dict]:
    """
    Group rows by description similarity ≥95% into clusters.
    Only compare descriptions > 20 chars that don't start with a digit.
    Returns modified rows list with DUPLICATE_CLUSTER flags added.
    """
    candidates = [
        r for r in rows
        if r.get('description')
        and len(r['description']) > 20
        and not r['description'][0].isdigit()
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
            score = fuzz.ratio(row_a['description'], row_b['description'])
            if score >= 95:
                cluster.append(row_b)
                visited.add(id(row_b))
        if len(cluster) >= 2:
            clusters.append(cluster)

    # For each cluster, pick representative (highest amount), flag it
    cluster_rows = set()
    for cluster in clusters:
        sorted_cluster = sorted(
            cluster,
            key=lambda r: r.get('amount') or 0,
            reverse=True,
        )
        representative = sorted_cluster[0]
        cluster_row_ids = [r.get('row_id') for r in cluster]
        n = len(cluster)

        flag = {
            'flag_type': 'DUPLICATE_CLUSTER',
            'cluster_size': n,
            'matched_rows': cluster_row_ids,
            'severity': 'HIGH' if n > 5 else 'MEDIUM',
            'title': f'Duplicate Cluster ({n}x)',
            'explanation': (
                f'This project description appears {n} times across the budget. '
                f'Verify these are genuinely separate projects at different locations '
                f'and not the same allocation duplicated.'
            ),
        }

        representative.setdefault('_flags', []).append(flag)
        representative['cluster_size'] = n
        cluster_rows.add(id(representative))

        # Mark non-representative members for exclusion
        for member in sorted_cluster[1:]:
            member['_exclude'] = True

    return rows


# ─── Flag 5: GHOST_PROJECT ────────────────────────────────────────────────────

YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')


def flag_ghost_project(row: Dict, all_descriptions: List[str], budget_year: Optional[str]) -> Optional[Dict]:
    if not budget_year:
        return None
    try:
        by = int(budget_year)
    except ValueError:
        return None

    description = row.get('description', '') or ''
    years_in_desc = [int(m) for m in YEAR_RE.findall(description)]

    stale_years = [y for y in years_in_desc if by - y >= 2]
    if not stale_years:
        return None

    # Also check if same description appears in other rows with old years
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

    if stale_years:
        return {
            'flag_type': 'GHOST_PROJECT',
            'severity': 'MEDIUM',
            'title': 'Ghost Project',
            'explanation': (
                f'This description references {min(stale_years)} — 2+ years before the {budget_year} budget. '
                f'Verify this is not a recycled or ghost allocation.'
            ),
        }
    return None


# ─── Main runner ─────────────────────────────────────────────────────────────

def run_all_flags(df, budget_year: Optional[str] = None) -> List[Dict]:
    """
    Run all flag checks and return list of flagged item dicts.
    """
    rows = df.to_dict('records')

    # Attach empty flags list and init fields
    for row in rows:
        row['_flags'] = []
        row['_exclude'] = False

    # Flags 1, 2, 3, 5 — per-item
    all_descriptions = [r.get('description', '') or '' for r in rows]
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

    # Flag 4 — cluster-based (modifies rows in-place)
    rows = flag_duplicates(rows)

    # Build final result: only flagged, non-excluded rows
    results = []
    for row in rows:
        if row.get('_exclude'):
            continue
        flags = row.get('_flags', [])
        if not flags:
            continue

        result = {
            'row_id': row.get('row_id'),
            'description': row.get('description'),
            'amount': row.get('amount'),
            'location': row.get('location'),
            'ministry': row.get('ministry'),
            'project_code': row.get('project_code'),
            'is_mda_level': row.get('is_mda_level'),
            'cluster_size': row.get('cluster_size'),
            'flags': flags,
        }
        results.append(result)

    return results
