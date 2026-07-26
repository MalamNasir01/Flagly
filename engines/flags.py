"""
flags.py — Red-flag detection engine for Flagly
Flag types:
  INFLATED_AMOUNT, CONTEXT_MISMATCH, MISSING_LOCATION,
  DUPLICATE_CLUSTER, GHOST_PROJECT,
  VAGUE_LOCATION, BUDGET_SPLITTING, MANDATE_MISMATCH, OVERHEAD_DOMINANCE
"""

import re
import math
import json
import os
from collections import defaultdict
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


def _str_cell(val) -> str:
    """Safely coerce a DataFrame cell to str, returning '' for None/NaN/Inf."""
    if val is None:
        return ''
    try:
        if math.isnan(float(val)):
            return ''
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return s if s.lower() != 'nan' else ''


def _fmt_amount(val) -> str:
    if _is_null_amount(val):
        return 'an unspecified amount'
    return f'NGN {float(val):,.0f}'


def _data_path(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', name)


def _load_json(name: str, default):
    try:
        with open(_data_path(name), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'[flags] could not load {name}: {e}')
        return default


def _normalize_vague_phrases(raw) -> list:
    """Accept plain string lists or {_meta, phrases:[{phrase, severity}]} docs."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get('phrases') or raw.get('vague_location_phrases') or []
    else:
        items = []
    out = []
    for item in items:
        if isinstance(item, str):
            out.append({'phrase': item.lower().strip(), 'severity': None, 'category': None})
        elif isinstance(item, dict) and item.get('phrase'):
            out.append({
                'phrase': str(item['phrase']).lower().strip(),
                'severity': (item.get('severity') or '').lower() or None,
                'category': item.get('category'),
            })
    # Longest phrases first so "selected locations" beats "selected"
    out.sort(key=lambda x: len(x['phrase']), reverse=True)
    return out


def _normalize_mda_mandates(raw) -> dict:
    """Accept legacy name->meta maps or {_meta, mdas:[{name, aliases, scope, excluded}]}."""
    if isinstance(raw, dict) and 'mdas' in raw:
        items = raw.get('mdas') or []
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        # Legacy flat map
        return {
            k: v for k, v in raw.items()
            if isinstance(v, dict) and k != '_meta'
        }
    else:
        items = []

    out = {}
    for item in items:
        if not isinstance(item, dict) or not item.get('name'):
            continue
        name = str(item['name']).strip()
        out[name.upper()] = {
            'name': name,
            'aliases': item.get('aliases') or [],
            'scope': item.get('scope') or [],
            'excluded': item.get('excluded') or [],
        }
    return out


VAGUE_LOCATION_PHRASE_RECORDS = _normalize_vague_phrases(_load_json('vague_location_phrases.json', [
    'selected locations', 'multiple lots', 'various states', 'nationwide',
    'geopolitical zone', 'senatorial zone', 'selected states', 'selected lgas',
    'various locations', 'across the country',
]))
VAGUE_LOCATION_PHRASES = [r['phrase'] for r in VAGUE_LOCATION_PHRASE_RECORDS]
VAGUE_PHRASE_SEVERITY = {r['phrase']: r.get('severity') for r in VAGUE_LOCATION_PHRASE_RECORDS}

MDA_MANDATES = _normalize_mda_mandates(_load_json('mda_mandates.json', {}))
NIGERIA_GEO = _load_json('nigeria_states_lgas.json', {'states': []})
print(f"[flags] loaded {len(MDA_MANDATES)} MDA mandates, {len(VAGUE_LOCATION_PHRASES)} vague phrases")

_STATE_NAMES = [s['name'] for s in NIGERIA_GEO.get('states', [])]
_LGA_NAMES = [lga for s in NIGERIA_GEO.get('states', []) for lga in s.get('lgas', [])]
_GEO_RE = re.compile(
    r'\b(' + '|'.join(re.escape(n) for n in sorted(_STATE_NAMES + _LGA_NAMES, key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
) if (_STATE_NAMES or _LGA_NAMES) else None


def _has_specific_geo(text: str) -> bool:
    if not text:
        return False
    if _GEO_RE and _GEO_RE.search(text):
        return True
    return False


# Map description cues onto excluded/scope tokens used in mda_mandates.json
_EXCLUDED_MARKERS = [
    ('primary_schools', ['primary school', 'classroom', 'basic education']),
    ('secondary_schools', ['secondary school', 'secondary education']),
    ('tertiary_education', ['university', 'polytechnic', 'college of', 'nursing school', 'nursing']),
    ('health_facilities', ['hospital', 'clinic', 'primary health', 'health centre', 'health center']),
    ('sports_facilities', ['stadium', 'sports complex', 'sports centre', 'sports center']),
    ('markets', ['market stall', 'market construction', 'modern market']),
    ('housing', ['housing estate', 'residential housing', 'staff quarters']),
    ('water_supply', ['borehole', 'water supply', 'water scheme']),
    ('electricity_generation', ['power plant', 'electricity generation', 'solar farm']),
    ('agriculture_inputs', ['fertilizer', 'seedling', 'farm input', 'tractor']),
    ('military_equipment', ['armoured', 'ammunition', 'military equipment']),
]


# ─── Category benchmarks ──────────────────────────────────────────────────────

CATEGORY_BENCHMARKS = [
    (['road', 'highway', 'carriageway', 'feeder'], 3_000_000_000),
    (['bridge'],                          3_000_000_000),
    (['school', 'classroom', 'nursing school'], 600_000_000),
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
    (['streetlight', 'street light'],      100_000_000),
    (['stadium', 'sports'],                500_000_000),
]

FALLBACK_THRESHOLD = 1_000_000_000
HARD_CEILING = 1_000_000_000

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
    desc_lower = _str_cell(description).lower()
    return any(kw in desc_lower for kw in PHYSICAL_PROJECT_KEYWORDS)


def _category_label(description: str) -> str:
    cat = _match_category(description)
    return cat[0] if cat else 'uncategorised'


# ─── Flag 1: INFLATED_AMOUNT ──────────────────────────────────────────────────

def flag_inflated_amount(row: Dict) -> Optional[Dict]:
    """Hard ceiling and category benchmark checks. IQR outliers are added in batch."""
    if row.get('is_mda_level'):
        return None
    amount = row.get('amount')
    description = _str_cell(row.get('description') or row.get('project_name'))
    if _is_null_amount(amount):
        return None
    amount = float(amount)

    # 3a. Hard ceiling: any item at or above ₦1B is HIGH
    if amount >= HARD_CEILING:
        cat = _match_category(description)
        label = cat[0] if cat else 'this category'
        return {
            'flag_type': 'INFLATED_AMOUNT',
            'severity': 'HIGH',
            'title': 'Hard Ceiling Inflated Amount',
            'explanation': (
                f'This line item is priced at {_fmt_amount(amount)}. '
                f'Any single allocation at or above NGN 1,000,000,000 triggers an automatic high risk flag. '
                f'Cross reference the amount against BPP Price Intelligence benchmarks for {label} before drawing any conclusion.'
            ),
            'evidence': {
                'rule': 'hard_ceiling',
                'threshold': HARD_CEILING,
                'category': label,
                'amount': amount,
            },
        }

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
                    f'This line item is priced at {_fmt_amount(amount)} for a {label} project. '
                    f'The category benchmark used by the scanner is NGN {threshold:,.0f}. '
                    f'The amount exceeds that benchmark. Cross reference against BPP Price Intelligence before publication.'
                ),
                'evidence': {
                    'rule': 'category_benchmark',
                    'category': label,
                    'threshold': threshold,
                    'amount': amount,
                },
            }
    return None


def flag_iqr_outliers(rows: List[Dict]) -> List[Dict]:
    """3b. Category relative IQR outlier detection."""
    by_cat: Dict[str, List[Dict]] = defaultdict(list)
    for row in rows:
        if row.get('is_mda_level') or row.get('_exclude'):
            continue
        if _is_null_amount(row.get('amount')):
            continue
        label = _category_label(_str_cell(row.get('description') or row.get('project_name')))
        if label == 'uncategorised':
            continue
        by_cat[label].append(row)

    for label, members in by_cat.items():
        if len(members) < 4:
            continue
        amounts = sorted(float(r['amount']) for r in members)
        n = len(amounts)
        q1 = amounts[n // 4]
        q3 = amounts[(3 * n) // 4]
        iqr = q3 - q1
        if iqr <= 0:
            continue
        median = amounts[n // 2]
        high_bound = q3 + 3 * iqr
        med_bound = q3 + 1.5 * iqr

        for row in members:
            amt = float(row['amount'])
            if amt <= med_bound:
                continue
            # Skip if a hard ceiling / benchmark flag already covers this as HIGH with higher rule priority
            existing = [f for f in row.get('_flags', []) if f.get('flag_type') == 'INFLATED_AMOUNT']
            if any(f.get('evidence', {}).get('rule') == 'hard_ceiling' for f in existing):
                continue
            severity = 'HIGH' if amt > high_bound else 'MEDIUM'
            flag = {
                'flag_type': 'INFLATED_AMOUNT',
                'severity': severity,
                'title': 'IQR Outlier Inflated Amount',
                'explanation': (
                    f'This line item is priced at {_fmt_amount(amt)} within the {label} category of this budget. '
                    f'The category median is {_fmt_amount(median)}. '
                    f'The statistical upper bound used by the scanner is {_fmt_amount(high_bound if severity == "HIGH" else med_bound)}. '
                    f'This item sits above that bound. Cross reference the amount against BPP Price Intelligence benchmarks before drawing any conclusion.'
                ),
                'evidence': {
                    'rule': 'iqr_outlier',
                    'category': label,
                    'median': median,
                    'q1': q1,
                    'q3': q3,
                    'iqr': iqr,
                    'medium_bound': med_bound,
                    'high_bound': high_bound,
                    'amount': amt,
                },
            }
            # Replace weaker inflated flag or append
            if existing:
                row['_flags'] = [f for f in row['_flags'] if f.get('flag_type') != 'INFLATED_AMOUNT'] + [flag]
            else:
                row.setdefault('_flags', []).append(flag)

    return rows


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
    Duplicate matching with RapidFuzz token_set_ratio.
    Similarity 95 to 100 is HIGH. Similarity 85 to 94 is MEDIUM.
    Both paired items are flagged and carry matched counterpart evidence.
    """
    candidates = [
        r for r in rows
        if _str_cell(r.get('description') or r.get('project_name'))
        and len(_str_cell(r.get('description') or r.get('project_name'))) >= 40
        and not _str_cell(r.get('description') or r.get('project_name'))[0].isdigit()
        and _has_action_verb(_str_cell(r.get('description') or r.get('project_name')))
    ]

    # Track best pair per row
    best_pair: Dict[int, Dict] = {}

    for i, row_a in enumerate(candidates):
        desc_a = _str_cell(row_a.get('description') or row_a.get('project_name'))
        for j in range(i + 1, len(candidates)):
            row_b = candidates[j]
            desc_b = _str_cell(row_b.get('description') or row_b.get('project_name'))
            score = fuzz.token_set_ratio(desc_a, desc_b)
            if score < 85:
                continue
            severity = 'HIGH' if score >= 95 else 'MEDIUM'
            pair_info_a = {
                'score': score,
                'severity': severity,
                'counterpart_row_id': row_b.get('row_id'),
                'counterpart_description': desc_b[:120],
                'counterpart_amount': row_b.get('amount'),
                'counterpart_code': row_b.get('project_code'),
            }
            pair_info_b = {
                'score': score,
                'severity': severity,
                'counterpart_row_id': row_a.get('row_id'),
                'counterpart_description': desc_a[:120],
                'counterpart_amount': row_a.get('amount'),
                'counterpart_code': row_a.get('project_code'),
            }
            for row, info in ((row_a, pair_info_a), (row_b, pair_info_b)):
                rid = id(row)
                prev = best_pair.get(rid)
                if prev is None or info['score'] > prev['score']:
                    best_pair[rid] = info
                    best_pair[rid]['_row'] = row

    # Group into clusters of mutual high matches for cluster_size reporting
    cluster_ids: Dict[int, set] = defaultdict(set)
    for rid, info in best_pair.items():
        row = info['_row']
        cluster_ids[rid].add(row.get('row_id'))
        cluster_ids[rid].add(info['counterpart_row_id'])

    for rid, info in best_pair.items():
        row = info['_row']
        matched = sorted(x for x in cluster_ids[rid] if x is not None)
        n = max(2, len(matched))
        severity = info['severity']
        if n > 5:
            severity = 'HIGH'
        flag = {
            'flag_type': 'DUPLICATE_CLUSTER',
            'cluster_size': n,
            'matched_rows': matched,
            'severity': severity,
            'title': f'Duplicate Cluster ({n}x)',
            'explanation': (
                f'This project description closely matches another line item at {info["score"]}% similarity. '
                f'The matched counterpart is "{info["counterpart_description"]}" '
                f'({_fmt_amount(info["counterpart_amount"])}). '
                f'Verify these are genuinely separate projects at different locations and not the same allocation duplicated. '
                f'File a Freedom of Information request for award history if the sites cannot be distinguished.'
            ),
            'evidence': {
                'similarity': info['score'],
                'matched_row_id': info['counterpart_row_id'],
                'matched_description': info['counterpart_description'],
                'matched_amount': info['counterpart_amount'],
                'matched_code': info['counterpart_code'],
                'matched_rows': matched,
            },
        }
        # Avoid duplicate DUPLICATE_CLUSTER flags
        existing = [f for f in row.get('_flags', []) if f.get('flag_type') == 'DUPLICATE_CLUSTER']
        if not existing:
            row.setdefault('_flags', []).append(flag)
            row['cluster_size'] = n

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

    description = _str_cell(row.get('description') or row.get('project_name'))
    years_in_desc = [int(m) for m in YEAR_RE.findall(description)]
    stale_years = [y for y in years_in_desc if by - y >= 2]
    if not stale_years:
        return None

    for other_desc in all_descriptions:
        if other_desc == description:
            continue
        if fuzz.token_set_ratio(description, other_desc) >= 95:
            other_years = [int(m) for m in YEAR_RE.findall(other_desc)]
            if any(by - y >= 2 for y in other_years):
                return {
                    'flag_type': 'GHOST_PROJECT',
                    'severity': 'MEDIUM',
                    'title': 'Ghost Project',
                    'explanation': (
                        f'This description closely matches a project from '
                        f'{min(stale_years)}, which is 2 or more years before the {budget_year} budget. '
                        f'Search Open Treasury and tracka.ng for completion evidence before treating this as a new allocation.'
                    ),
                    'evidence': {'stale_years': stale_years, 'budget_year': by},
                }

    return {
        'flag_type': 'GHOST_PROJECT',
        'severity': 'MEDIUM',
        'title': 'Ghost Project',
        'explanation': (
            f'This description references {min(stale_years)}, which is 2 or more years before the '
            f'{budget_year} budget. Search Open Treasury and tracka.ng for completion evidence.'
        ),
        'evidence': {'stale_years': stale_years, 'budget_year': by},
    }


def flag_ghost_projects_multiyear(year_frames: Dict[str, List[Dict]]) -> List[Dict]:
    """Cross year ghost detection when two or more budget years are uploaded.

    Recurrence across three or more consecutive years with ONGOING is HIGH.
    Two year recurrence is MEDIUM.
    """
    if len(year_frames) < 2:
        return []

    years = sorted(int(y) for y in year_frames.keys())
    # Flatten candidates by year
    indexed = []
    for y in years:
        for row in year_frames[str(y)]:
            desc = _str_cell(row.get('description') or row.get('project_name'))
            if len(desc) < 20:
                continue
            indexed.append((y, desc, row))

    # For each row in the newest year, find matches in earlier years
    results_touched = []
    newest = years[-1]
    for y, desc, row in indexed:
        if y != newest:
            continue
        appearances = {y: {'amount': row.get('amount'), 'status': _str_cell(row.get('project_status')), 'row_id': row.get('row_id')}}
        for y2, desc2, row2 in indexed:
            if y2 >= y:
                continue
            if fuzz.token_set_ratio(desc, desc2) < 90:
                continue
            appearances[y2] = {
                'amount': row2.get('amount'),
                'status': _str_cell(row2.get('project_status')),
                'row_id': row2.get('row_id'),
            }
        if len(appearances) < 2:
            continue
        yrs = sorted(appearances.keys())
        # consecutive check
        consecutive = all(yrs[i + 1] - yrs[i] == 1 for i in range(len(yrs) - 1))
        ongoing_streak = all(
            (appearances[yy].get('status') or '').upper() == 'ONGOING' for yy in yrs
        )
        if len(yrs) >= 3 and consecutive and ongoing_streak:
            severity = 'HIGH'
        elif len(yrs) >= 3 and consecutive:
            severity = 'HIGH'
        else:
            severity = 'MEDIUM'

        year_lines = ', '.join(
            f"{yy} ({_fmt_amount(appearances[yy]['amount'])})" for yy in yrs
        )
        flag = {
            'flag_type': 'GHOST_PROJECT',
            'severity': severity,
            'title': 'Multi Year Ghost Project',
            'explanation': (
                f'This project name recurs across budget years {year_lines}. '
                f'Search Open Treasury and tracka.ng for completion evidence before accepting another rollover.'
            ),
            'evidence': {
                'years': appearances,
                'year_list': yrs,
            },
        }
        row.setdefault('_flags', []).append(flag)
        results_touched.append(row)

    return results_touched


# ─── Flag A: VAGUE_LOCATION ───────────────────────────────────────────────────

def flag_vague_location(row: Dict) -> Optional[Dict]:
    if row.get('is_mda_level'):
        return None
    description = _str_cell(row.get('description') or row.get('project_name'))
    location = _str_cell(row.get('location'))
    combined = f'{description} {location}'.strip().lower()
    amount = row.get('amount')

    matched = next((p for p in VAGUE_LOCATION_PHRASES if p in combined), None)
    if not matched:
        return None

    # Spec: phrase present and no specific state or LGA extracted
    if _has_specific_geo(location) or _has_specific_geo(description):
        return None

    phrase_sev = (VAGUE_PHRASE_SEVERITY.get(matched) or '').upper()
    if phrase_sev in ('HIGH', 'MEDIUM', 'LOW'):
        severity = phrase_sev
    else:
        severity = 'MEDIUM'
    if not _is_null_amount(amount) and float(amount) >= 5_000_000:
        severity = 'HIGH'

    return {
        'flag_type': 'VAGUE_LOCATION',
        'severity': severity,
        'title': 'Non Traceable Location',
        'explanation': (
            f'The project description uses vague location language ("{matched}"). '
            f'Without a specific state or LGA, implementation cannot be tracked. '
            f'Request geo tagged evidence from the responsible MDA or check tracka.ng for site reports.'
        ),
        'evidence': {
            'phrase': matched,
            'location': location or None,
        },
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
        ministry = _str_cell(row.get('ministry'))
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
    'road':        ['road', 'highway', 'bridge', 'transport', 'works', 'infrastructure', 'carriageway', 'feeder'],
    'health':      ['health', 'hospital', 'medical', 'clinic', 'pharmaceutical', 'nursing'],
    'education':   ['education', 'school', 'university', 'college', 'training', 'classroom'],
    'water':       ['water', 'irrigation', 'dam', 'sanitation', 'borehole'],
    'agriculture': ['agriculture', 'farm', 'livestock', 'fishery', 'food'],
    'power':       ['power', 'electricity', 'streetlight', 'street light', 'grid'],
    'sports':      ['stadium', 'sports', 'recreation'],
}


def _classify_sector(text: str) -> Optional[str]:
    if not text:
        return None
    text_lower = text.lower()
    for sector, keywords in MANDATE_MAP.items():
        if any(kw in text_lower for kw in keywords):
            return sector
    return None


def _lookup_mda_scope(mda_name: str) -> Optional[Dict]:
    if not mda_name or not MDA_MANDATES:
        return None
    key = mda_name.strip().upper()
    if key in MDA_MANDATES:
        return MDA_MANDATES[key]
    for name, meta in MDA_MANDATES.items():
        aliases = [a.upper() for a in meta.get('aliases', [])]
        canon = (meta.get('name') or name).upper()
        if key == name.upper() or key == canon or key in aliases or any(a in key or key in a for a in aliases + [canon]):
            return meta
    return None


def _match_excluded_category(description: str, excluded: list) -> Optional[str]:
    desc_lower = description.lower()
    excluded_set = {e.lower() for e in excluded}
    for token, cues in _EXCLUDED_MARKERS:
        if token not in excluded_set:
            continue
        if any(cue in desc_lower for cue in cues):
            return token
    # Also allow direct underscore token fragments in the description
    for token in excluded_set:
        readable = token.replace('_', ' ')
        if readable in desc_lower:
            return token
    return None


def flag_mandate_mismatch(row: Dict) -> Optional[Dict]:
    if row.get('is_mda_level'):
        return None
    ministry = _str_cell(row.get('mda_name') or row.get('ministry'))
    description = _str_cell(row.get('description') or row.get('project_name'))
    if not ministry or not description:
        return None

    meta = _lookup_mda_scope(ministry)
    if meta:
        scope = [s.lower() for s in meta.get('scope', [])]
        excluded = [s.lower() for s in meta.get('excluded', [])]
        desc_lower = description.lower()

        # In scope: skip
        if any(sk.replace('_', ' ') in desc_lower or sk in desc_lower for sk in scope):
            return None

        hit = _match_excluded_category(description, excluded)
        if hit:
            severity = 'HIGH'
            reason = f'listed in the MDA excluded categories ({hit})'
        else:
            # Not in scope and not explicitly excluded: MEDIUM pending review
            proj_sector = _classify_sector(description)
            if not proj_sector:
                return None
            if any(proj_sector[:4] in s or s in proj_sector for s in scope):
                return None
            hit = proj_sector
            severity = 'MEDIUM'
            reason = 'outside the published MDA scope and pending review'

        return {
            'flag_type': 'MANDATE_MISMATCH',
            'severity': severity,
            'title': 'Possible Mandate Violation',
            'explanation': (
                f'{ministry} is scoped to {", ".join(scope[:5]) or "its statutory functions"}, '
                f'but this project describes {hit} work. That is {reason}. '
                f'Request the enabling instrument or procurement plan from the MDA.'
            ),
            'evidence': {
                'mda': ministry,
                'scope': scope,
                'excluded': excluded,
                'project_marker': hit,
            },
        }

    # Fallback sector classifier
    mda_sector  = _classify_sector(ministry)
    proj_sector = _classify_sector(description)
    if not mda_sector or not proj_sector or mda_sector == proj_sector:
        return None

    return {
        'flag_type': 'MANDATE_MISMATCH',
        'severity':  'HIGH',
        'title':     'Possible Mandate Violation',
        'explanation': (
            f'{ministry} is a {mda_sector} agency but this project appears to be a '
            f'{proj_sector} project. Request the enabling instrument or procurement plan from the MDA.'
        ),
        'evidence': {
            'mda_sector': mda_sector,
            'project_sector': proj_sector,
        },
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


# ─── Flag 6: COMPOSITE_DUPLICATE (Format B exact key matching) ────────────────

def flag_duplicates_composite(rows: List[Dict]) -> List[Dict]:
    """
    Format B: exact composite key match on mda_code + economic_code + location_code.
    Same MDA + Same Economic Code + Same Location = definitive duplicate.
    All group members are flagged (no exclusion — every instance is suspect).
    """
    from collections import defaultdict

    def _valid_composite_key(row):
        """Return composite key string only when all three codes are well-formed digits."""
        mda = _str_cell(row.get('mda_code'))
        eco = _str_cell(row.get('economic_code'))
        loc = _str_cell(row.get('location_code'))
        if (len(mda) == 12 and mda.isdigit() and
                len(eco) == 8 and eco.isdigit() and
                len(loc) == 8 and loc.isdigit()):
            return f'{mda}|{eco}|{loc}'
        return None  # any code missing or malformed — no key assigned

    key_groups: Dict[str, List[Dict]] = defaultdict(list)
    for row in rows:
        key = _valid_composite_key(row)
        if key is None:
            continue
        key_groups[key].append(row)

    for key, group in key_groups.items():
        if len(group) < 2:
            continue
        n = len(group)
        amounts = [float(r['amount']) for r in group if not _is_null_amount(r.get('amount'))]
        total_amount = sum(amounts)
        mda_part, eco_part, loc_part = key.split('|', 2)

        severity = 'HIGH' if (n >= 3 or total_amount > 100_000_000) else 'MEDIUM'
        explanation = (
            f'MDA {mda_part} allocated economic code {eco_part} spending at location '
            f'{loc_part} {n} times. Total double-allocated amount: {_fmt_amount(total_amount)}.'
        )
        cluster_row_ids = [r.get('row_id') for r in group]
        flag = {
            'flag_type':    'COMPOSITE_DUPLICATE',
            'severity':     severity,
            'title':        f'Composite Duplicate ({n}×)',
            'explanation':  explanation,
            'cluster_size': n,
            'matched_rows': cluster_row_ids,
        }
        for member in group:
            member.setdefault('_flags', []).append(flag)
            member['cluster_size'] = n

    return rows


# ─── Flag 7: INFLATED_PROJECTION ──────────────────────────────────────────────

def flag_inflated_projection(row: Dict) -> Optional[Dict]:
    """Budget doubled YoY with zero prior-year implementation — Format B only."""
    budget_2026 = row.get('budget_2026')
    budget_2025 = row.get('budget_2025')
    perf_2025   = row.get('performance_2025')
    if _is_null_amount(budget_2026) or _is_null_amount(budget_2025) or _is_null_amount(perf_2025):
        return None
    b26 = float(budget_2026)
    b25 = float(budget_2025)
    p25 = float(perf_2025)
    if b25 <= 0 or b26 <= 0:
        return None
    if not (b26 > b25 * 2 and p25 == 0):
        return None
    ratio = b26 / b25
    severity = 'HIGH' if b26 > 100_000_000 else 'MEDIUM'
    return {
        'flag_type':   'INFLATED_PROJECTION',
        'severity':    severity,
        'title':       'Inflated Projection',
        'explanation': (
            f'2026 allocation of {_fmt_amount(b26)} is {ratio:.1f}× the 2025 budget of '
            f'{_fmt_amount(b25)}, but 2025 performance was zero. This project was not '
            f'implemented last year yet received a larger allocation.'
        ),
    }


# ─── Flag 8: PHANTOM_SPENDING ─────────────────────────────────────────────────

_PHANTOM_KEYWORDS = ['construction', 'renovation', 'supply', 'purchase',
                     'procurement', 'rehabilitation']


def flag_phantom_spending(row: Dict) -> Optional[Dict]:
    """Economic code R&D (23050101) but description indicates physical project."""
    economic_code = _str_cell(row.get('economic_code'))
    if not economic_code.startswith('23050101'):
        return None
    description = _str_cell(row.get('description')).lower()
    matched = next((kw for kw in _PHANTOM_KEYWORDS if kw in description), None)
    if not matched:
        return None
    return {
        'flag_type':   'PHANTOM_SPENDING',
        'severity':    'MEDIUM',
        'title':       'Economic Code Mismatch',
        'explanation': (
            f'This item is coded as Research & Development (23050101) but the description '
            f'suggests it is a {matched} project. Miscoding may be used to obscure the '
            f'nature of spending.'
        ),
    }


# ─── Flag 9: VAGUE_HIGH_VALUE_SPEND ───────────────────────────────────────────

def flag_vague_high_value_spend(row: Dict) -> Optional[Dict]:
    """Capital expenditure allocated statewide (location 12642600) > ₦500M."""
    if row.get('is_mda_level'):
        return None
    location_code = str(row.get('location_code') or '')
    if location_code != '12642600':
        return None
    economic_code = str(row.get('economic_code') or '')
    if not economic_code.startswith('23'):
        return None
    amount = row.get('amount')
    if _is_null_amount(amount) or float(amount) <= 500_000_000:
        return None
    return {
        'flag_type':   'VAGUE_HIGH_VALUE_SPEND',
        'severity':    'MEDIUM',
        'title':       'Vague High-Value Capital Spend',
        'explanation': (
            f'Capital expenditure of {_fmt_amount(float(amount))} is allocated statewide '
            f'(location code 12642600) with no specific delivery location. High-value '
            f'capital projects should have traceable implementation sites.'
        ),
    }


# ─── Flag 10: ZERO_ROLLOVER ───────────────────────────────────────────────────

def flag_zero_implementation_rollover(row: Dict) -> Optional[Dict]:
    """Zero prior-year implementation despite significant budget — Format B only."""
    if row.get('is_mda_level'):
        return None
    budget_2025 = row.get('budget_2025')
    perf_2025   = row.get('performance_2025')
    budget_2026 = row.get('budget_2026')
    if _is_null_amount(budget_2025) or _is_null_amount(perf_2025) or _is_null_amount(budget_2026):
        return None
    b25 = float(budget_2025)
    p25 = float(perf_2025)
    b26 = float(budget_2026)
    if not (p25 == 0 and b25 > 10_000_000 and b26 > 10_000_000):
        return None
    return {
        'flag_type':   'ZERO_ROLLOVER',
        'severity':    'MEDIUM',
        'title':       'Zero Implementation Rollover',
        'explanation': (
            f'This project had {_fmt_amount(b25)} budgeted in 2025 but recorded zero '
            f'implementation. The allocation has been rolled over to 2026 without evidence '
            f'of delivery.'
        ),
    }


# ─── Main runner ──────────────────────────────────────────────────────────────

def run_all_flags(df, budget_year: Optional[str] = None) -> List[Dict]:
    """Run all flag checks and return list of flagged item dicts."""
    rows = df.to_dict('records')

    for row in rows:
        row['_flags']   = []
        row['_exclude'] = False

    # Detect Format B (Niger State): has mda_code values
    is_format_b = (
        'mda_code' in df.columns
        and df['mda_code'].notna().any()
    )

    all_descriptions = [r.get('description', '') or '' for r in rows]

    # Per-row flags — universal
    for row in rows:
        f1 = flag_inflated_amount(row)
        if f1: row['_flags'].append(f1)

        f2 = flag_context_mismatch(row)
        if f2: row['_flags'].append(f2)

        f3 = flag_missing_location(row)
        if f3: row['_flags'].append(f3)

        f5 = flag_ghost_project(row, all_descriptions, budget_year)
        if f5: row['_flags'].append(f5)

        fa = flag_vague_location(row)
        if fa: row['_flags'].append(fa)

        fc = flag_mandate_mismatch(row)
        if fc: row['_flags'].append(fc)

        fe = flag_overhead_dominance(row)
        if fe: row['_flags'].append(fe)

        # Per-row flags — Format B only
        if is_format_b:
            f6 = flag_inflated_projection(row)
            if f6: row['_flags'].append(f6)

            f7 = flag_phantom_spending(row)
            if f7: row['_flags'].append(f7)

            f8 = flag_vague_high_value_spend(row)
            if f8: row['_flags'].append(f8)

            f9 = flag_zero_implementation_rollover(row)
            if f9: row['_flags'].append(f9)

    # Batch flags (modify rows in-place)
    rows = flag_iqr_outliers(rows)

    if is_format_b:
        # Exact composite key matching replaces fuzzy duplicate detection for Format B
        rows = flag_duplicates_composite(rows)
    else:
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
            'row_id':          row.get('row_id'),
            'description':     row.get('description') or row.get('project_name'),
            'amount':          row.get('amount'),
            'location':        row.get('location'),
            'ministry':        row.get('mda_name') or row.get('ministry'),
            'project_code':    row.get('project_code'),
            'is_mda_level':    row.get('is_mda_level'),
            'cluster_size':    row.get('cluster_size'),
            'flags':           flags,
            # Seven extracted parameters
            'mda_code':        row.get('mda_code'),
            'mda_name':        row.get('mda_name') or row.get('ministry'),
            'project_name':    row.get('project_name') or row.get('description'),
            'project_status':  row.get('project_status'),
            'expenditure_code': row.get('expenditure_code') or row.get('economic_code'),
            'data_quality_notes': row.get('data_quality_notes'),
            # Format B passthrough fields
            'economic_code':   row.get('economic_code'),
            'function_code':   row.get('function_code'),
            'location_code':   row.get('location_code'),
            'actuals_2024':    row.get('actuals_2024'),
            'budget_2025':     row.get('budget_2025'),
            'performance_2025': row.get('performance_2025'),
            'budget_2026':     row.get('budget_2026'),
        })

    return results
