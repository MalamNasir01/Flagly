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
            'mda_code': item.get('mda_code'),
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
STATE_MDA_MANDATES = _normalize_mda_mandates(_load_json('mda_mandates_states.json', {}))
# Active mandate table — swapped per run_all_flags(jurisdiction=...)
ACTIVE_MDA_MANDATES = MDA_MANDATES
NIGERIA_GEO = _load_json('nigeria_states_lgas.json', {'states': []})
print(
    f"[flags] loaded {len(MDA_MANDATES)} federal MDA mandates, "
    f"{len(STATE_MDA_MANDATES)} state MDA mandates, "
    f"{len(VAGUE_LOCATION_PHRASES)} vague phrases"
)

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


# ─── Category benchmarks (legacy sector cues kept for context mismatch only) ─

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

PHYSICAL_PROJECT_KEYWORDS = [
    'construction', 'rehabilitation', 'renovation', 'procurement', 'supply',
    'establishment', 'provision', 'installation', 'repair',
]

# Run statistics for unclassified lines (unknown category path).
LAST_RUN_STATS: Dict = {
    'total_items': 0,
    'unclassified_count': 0,
    'flagged_items': 0,
}


def get_last_run_stats() -> Dict:
    return dict(LAST_RUN_STATS)


def _match_category(description: str):
    """Legacy helper used only by context mismatch. Prefer classify_project()."""
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


# ─── Flag 1: INFLATED_AMOUNT (relative to category benchmark) ─────────────────

# Aggregate / nationwide programme signals — exempt from unit-price inflation.
_DEFAULT_AGGREGATE_SIGNALS = [
    'nationwide', 'nation wide', 'nation-wide', 'across the nation', 'across the country',
    'across all', 'across the 6', 'across the six', 'geo-political zone', 'geopolitical zone',
    'geopolitical zones', 'geo political zones', 'all geopolitical', 'programmes across',
    'programs across', 'programme across', 'program across', 'multiple lots',
    'selected locations across', 'across nigerian army', 'across all nigerian',
]

_AGGREGATE_RE = re.compile(
    r'\b(nationwide|nation[\s\-]?wide|across\s+the\s+(?:nation|country)|'
    r'across\s+all\b|across\s+the\s+(?:6|six)\b|'
    r'(?:geo[\s\-]?political)\s+zones?|programmes?\s+across|programs?\s+across|'
    r'multiple\s+lots|selected\s+locations\s+across)\b',
    re.IGNORECASE,
)


def _aggregate_signal(description: str, location: Optional[str] = None) -> Optional[str]:
    """Return matched aggregate phrase if line is a programme/nationwide aggregate.

    Also checks location labels such as "State Wide" so state unit-price inflation
    does not treat statewide aggregates as single projects.
    """
    blobs = [description or "", location or ""]
    from engines.classifier import get_flag_config
    cfg = get_flag_config().get('inflated') or {}
    if 'aggregate_exempt_signals' in cfg:
        phrases = cfg.get('aggregate_exempt_signals') or []
    else:
        phrases = _DEFAULT_AGGREGATE_SIGNALS
    phrases_sorted = sorted((str(p).lower() for p in phrases), key=len, reverse=True)
    for blob in blobs:
        text = blob.lower()
        if not text:
            continue
        for phrase in phrases_sorted:
            if phrase and phrase in text:
                return phrase
    if 'aggregate_exempt_signals' not in cfg and description:
        m = _AGGREGATE_RE.search(description)
        return m.group(0).lower() if m else None
    return None


def flag_inflated_amount(row: Dict) -> Optional[Dict]:
    """Flag only when amount exceeds category benchmark * configured multiplier.

    Unknown category: skip (no benchmark = no flag). No flat ₦1B fallback.
    Uses large-facility tier when description matches tier keywords.
    Aggregate / nationwide programme lines are exempt — vague/missing-location
    already covers them; unit-price inflation does not apply.
    """
    from engines.classifier import (
        classify_with_match,
        get_flag_config,
        get_inflated_benchmark_meta,
        get_inflated_multiplier,
        get_inflated_high_multiplier,
    )

    if row.get('is_mda_level'):
        return None
    amount = row.get('amount')
    description = _str_cell(row.get('description') or row.get('project_name'))
    if _is_null_amount(amount) or not description:
        return None
    amount = float(amount)

    category, matched_kw = classify_with_match(description)
    row['_project_category'] = category
    row['_category_keyword'] = matched_kw
    if not category:
        return None

    # Aggregate / programme-wide lines: exempt from unit-price inflation
    loc = _str_cell(row.get('location'))
    agg_hit = _aggregate_signal(description, loc)
    if agg_hit:
        row['_inflation_exempt'] = agg_hit
        return None

    jurisdiction = row.get('_jurisdiction') or 'federal'
    meta = get_inflated_benchmark_meta(category, description, jurisdiction=jurisdiction)
    benchmark = meta.get('benchmark')
    if benchmark is None or benchmark <= 0:
        return None

    multiplier = get_inflated_multiplier()
    high_mult = get_inflated_high_multiplier()
    threshold = benchmark * multiplier
    high_threshold = benchmark * high_mult

    if amount <= threshold:
        return None

    severity = 'HIGH' if amount > high_threshold else 'MEDIUM'
    tier = meta.get('tier') or 'default'
    tier_note = (
        f' (large-facility tier via "{meta.get("matched_tier_keyword")}")'
        if tier == 'large' and meta.get('matched_tier_keyword')
        else ' (default tier)'
    )
    return {
        'flag_type': 'INFLATED_AMOUNT',
        'severity': severity,
        'title': 'Inflated Amount',
        'explanation': (
            f'This line item is priced at {_fmt_amount(amount)} for a {category} project'
            f'{tier_note}. '
            f'The category benchmark is {_fmt_amount(benchmark)} and the scanner flags amounts '
            f'above {multiplier:g} times that benchmark ({_fmt_amount(threshold)}). '
            f'Cross reference against BPP Price Intelligence before drawing any conclusion.'
        ),
        'evidence': {
            'rule': 'relative_benchmark',
            'category': category,
            'matched_keyword': matched_kw,
            'benchmark': benchmark,
            'benchmark_tier': tier,
            'matched_tier_keyword': meta.get('matched_tier_keyword'),
            'multiplier': multiplier,
            'threshold': threshold,
            'amount': amount,
        },
    }


def flag_iqr_outliers(rows: List[Dict]) -> List[Dict]:
    """IQR outlier pass disabled by default — relative benchmarks own this signal."""
    return rows


# ─── Flag 2: CONTEXT_MISMATCH ─────────────────────────────────────────────────

def flag_context_mismatch(row: Dict) -> Optional[Dict]:
    """Disproportionate amount under ₦1B relative to legacy category cues."""
    from engines.classifier import get_flag_config

    if row.get('is_mda_level'):
        return None
    cfg = (get_flag_config().get('context_mismatch') or {})
    if cfg.get('enabled') is False:
        return None

    amount = row.get('amount')
    description = _str_cell(row.get('description') or row.get('project_name'))
    if _is_null_amount(amount) or not description:
        return None
    amount = float(amount)
    under = float(cfg.get('under_amount_ngn', 1_000_000_000))
    if amount >= under:
        return None

    cat = _match_category(description)
    if not cat:
        return None

    label, threshold = cat
    mult = float(cfg.get('multiplier', 3.0))
    if amount > threshold * mult:
        return {
            'flag_type': 'CONTEXT_MISMATCH',
            'severity': 'MEDIUM',
            'title': 'Context Mismatch',
            'explanation': (
                f'Amount is disproportionate for the item category even though it falls '
                f'below the NGN {under:,.0f} threshold. {_fmt_amount(amount)} for a {label} project is unusual.'
            ),
        }
    return None


# ─── Flag 3: MISSING_LOCATION ─────────────────────────────────────────────────

BROAD_VALID_LOCATION_RE = re.compile(
    r'\b(STATE\s+WIDE|NATIONWIDE|ACROSS\s+THE\s+STATE)\b',
    re.IGNORECASE,
)


def _should_suppress_missing_location(description: str, category: Optional[str]) -> bool:
    from engines.classifier import get_flag_config
    cfg = get_flag_config().get('missing_location') or {}
    suppress_cats = {c.lower() for c in (cfg.get('suppress_categories') or [])}
    if category and category.lower() in suppress_cats:
        return True
    desc = description.lower()
    for kw in (cfg.get('suppress_description_keywords') or []):
        if kw.lower() in desc:
            return True
    return False


def flag_missing_location(row: Dict) -> Optional[Dict]:
    from engines.classifier import classify_project, get_flag_config

    if row.get('is_mda_level'):
        return None
    description = _str_cell(row.get('description') or row.get('project_name'))
    if not _has_physical_project_keyword(description):
        return None

    category = row.get('_project_category')
    if category is None:
        category = classify_project(description)
        row['_project_category'] = category

    if _should_suppress_missing_location(description, category):
        return None

    cfg = get_flag_config().get('missing_location') or {}
    min_amt = float(cfg.get('min_amount_ngn', 5_000_000))
    high_amt = float(cfg.get('high_amount_ngn', 100_000_000))

    location = row.get('location')
    amount = row.get('amount')
    if _is_null_amount(amount) or float(amount) <= min_amt:
        return None
    amount = float(amount)

    loc_str = _str_cell(location)
    if BROAD_VALID_LOCATION_RE.search(loc_str) or BROAD_VALID_LOCATION_RE.search(description):
        return None
    if loc_str and len(loc_str) > 3:
        return None
    if _has_specific_geo(loc_str) or _has_specific_geo(description):
        return None

    severity = 'HIGH' if amount > high_amt else 'MEDIUM'
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


def _mda_key(row: Dict) -> str:
    return _str_cell(row.get('mda_name') or row.get('ministry') or row.get('mda_code')).upper()


def _amounts_within_tolerance(a, b, tol_pct: float) -> bool:
    if _is_null_amount(a) or _is_null_amount(b):
        return False
    a, b = float(a), float(b)
    if a == b:
        return True
    if tol_pct <= 0:
        return False
    base = max(abs(a), abs(b), 1.0)
    return abs(a - b) / base <= (tol_pct / 100.0)


def flag_duplicates(rows: List[Dict]) -> List[Dict]:
    """Duplicates require same MDA, amount within tolerance, and high similarity."""
    from engines.classifier import get_flag_config

    cfg = get_flag_config().get('duplicates') or {}
    high_sim = int(cfg.get('high_similarity', 98))
    med_sim = int(cfg.get('medium_similarity', 95))
    tol = float(cfg.get('amount_tolerance_pct', 0.0))
    require_mda = bool(cfg.get('require_same_mda', True))
    min_len = int(cfg.get('min_description_length', 40))

    candidates = [
        r for r in rows
        if _str_cell(r.get('description') or r.get('project_name'))
        and len(_str_cell(r.get('description') or r.get('project_name'))) >= min_len
        and not _str_cell(r.get('description') or r.get('project_name'))[0].isdigit()
        and _has_action_verb(_str_cell(r.get('description') or r.get('project_name')))
    ]

    best_pair: Dict[int, Dict] = {}

    for i, row_a in enumerate(candidates):
        desc_a = _str_cell(row_a.get('description') or row_a.get('project_name'))
        mda_a = _mda_key(row_a)
        if require_mda and not mda_a:
            continue
        for j in range(i + 1, len(candidates)):
            row_b = candidates[j]
            if require_mda and _mda_key(row_b) != mda_a:
                continue
            if not _amounts_within_tolerance(row_a.get('amount'), row_b.get('amount'), tol):
                continue
            desc_b = _str_cell(row_b.get('description') or row_b.get('project_name'))
            score = fuzz.token_set_ratio(desc_a, desc_b)
            if score < med_sim:
                continue
            severity = 'HIGH' if score >= high_sim else 'MEDIUM'
            pair_a = {
                'score': score, 'severity': severity,
                'counterpart_row_id': row_b.get('row_id'),
                'counterpart_description': desc_b,
                'counterpart_amount': row_b.get('amount'),
                'counterpart_code': row_b.get('project_code'),
                '_row': row_a,
            }
            pair_b = {
                'score': score, 'severity': severity,
                'counterpart_row_id': row_a.get('row_id'),
                'counterpart_description': desc_a,
                'counterpart_amount': row_a.get('amount'),
                'counterpart_code': row_a.get('project_code'),
                '_row': row_b,
            }
            for info in (pair_a, pair_b):
                rid = id(info['_row'])
                prev = best_pair.get(rid)
                if prev is None or info['score'] > prev['score']:
                    best_pair[rid] = info

    for rid, info in best_pair.items():
        row = info['_row']
        matched = [row.get('row_id'), info['counterpart_row_id']]
        matched = [x for x in matched if x is not None]
        flag = {
            'flag_type': 'DUPLICATE_CLUSTER',
            'cluster_size': max(2, len(set(matched))),
            'matched_rows': matched,
            'severity': info['severity'],
            'title': f'Duplicate Cluster ({max(2, len(set(matched)))}x)',
            'explanation': (
                f'This project description matches another line under the same MDA at {info["score"]}% similarity '
                f'with the same amount ({_fmt_amount(info["counterpart_amount"])}). '
                f'The matched counterpart is "{info["counterpart_description"]}". '
                f'Verify these are genuinely separate projects at different locations.'
            ),
            'evidence': {
                'similarity': info['score'],
                'matched_row_id': info['counterpart_row_id'],
                'matched_description': info['counterpart_description'],
                'matched_amount': info['counterpart_amount'],
                'matched_code': info['counterpart_code'],
                'same_mda': True,
                'amount_tolerance_pct': tol,
            },
        }
        if not any(f.get('flag_type') == 'DUPLICATE_CLUSTER' for f in row.get('_flags', [])):
            row.setdefault('_flags', []).append(flag)
            row['cluster_size'] = flag['cluster_size']

    return rows


# ─── Flag 5: GHOST_PROJECT ────────────────────────────────────────────────────

YEAR_RE = re.compile(
    r'(?<!\d)(201[0-9]|202[0-4])(?!\d)'
    r'(?=\s*[\)\-]|\s+(?:BUDGET|APPROPRIATION|FY|FISCAL|BATCH|EDITION|PHASE|TRANCHE|CONTRACT))',
    re.IGNORECASE,
)

# Year ranges like 2023-2027 / 2023–2027 (en/em dash). If end >= budget year, start is not stale.
YEAR_RANGE_RE = re.compile(
    r'(?<!\d)((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})(?!\d)'
)


def _stale_years_in_description(description: str, budget_year: int) -> List[int]:
    """Years that look stale vs budget_year, excluding start years of still-active ranges."""
    protected = set()
    for m in YEAR_RANGE_RE.finditer(description or ''):
        start_y, end_y = int(m.group(1)), int(m.group(2))
        if end_y < start_y:
            start_y, end_y = end_y, start_y
        if end_y >= budget_year:
            # Plan/strategy still covers the budget year — do not treat range start as ghost.
            protected.add(start_y)
            protected.add(end_y)
    years = [int(m) for m in YEAR_RE.findall(description or '')]
    return [y for y in years if budget_year - y >= 2 and y not in protected]


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
    stale_years = _stale_years_in_description(description, by)
    if not stale_years:
        return None

    for other_desc in all_descriptions:
        if other_desc == description:
            continue
        if fuzz.token_set_ratio(description, other_desc) >= 95:
            other_stale = _stale_years_in_description(other_desc, by)
            if other_stale:
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
    """Phrase match on description only when no state/LGA was extracted."""
    from engines.classifier import get_flag_config

    if row.get('is_mda_level'):
        return None
    description = _str_cell(row.get('description') or row.get('project_name'))
    location = _str_cell(row.get('location'))
    amount = row.get('amount')
    if not description:
        return None

    desc_lower = description.lower()
    matched = next((p for p in VAGUE_LOCATION_PHRASES if p in desc_lower), None)
    if not matched:
        return None

    # If a real location is present, do not flag even if a vague phrase appears
    if _has_specific_geo(location) or _has_specific_geo(description):
        return None

    phrase_sev = (VAGUE_PHRASE_SEVERITY.get(matched) or 'medium').upper()
    if phrase_sev not in ('HIGH', 'MEDIUM', 'LOW'):
        phrase_sev = 'MEDIUM'
    severity = phrase_sev

    elevate_at = float(
        (get_flag_config().get('vague_location') or {}).get('elevate_medium_to_high_amount_ngn', 5_000_000)
    )
    if severity == 'MEDIUM' and not _is_null_amount(amount) and float(amount) >= elevate_at:
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
            'phrase_severity': phrase_sev,
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


def _lookup_mda_scope(mda_name: str, mda_code: Optional[str] = None) -> Optional[Dict]:
    mandates = ACTIVE_MDA_MANDATES or MDA_MANDATES
    if not mandates:
        return None
    code = str(mda_code or "").strip()
    if code:
        for meta in mandates.values():
            aliases = [str(a).strip() for a in (meta.get('aliases') or [])]
            if code == str(meta.get('mda_code') or "").strip() or code in aliases:
                return meta
    if not mda_name:
        return None
    key = mda_name.strip().upper()
    if key in mandates:
        return mandates[key]
    for name, meta in mandates.items():
        aliases = [a.upper() for a in meta.get('aliases', [])]
        canon = (meta.get('name') or name).upper()
        if key == name.upper() or key == canon or key in aliases or any(
            a in key or key in a for a in aliases + [canon]
        ):
            return meta
    return None


def _match_excluded_category(category: Optional[str], excluded: list) -> Optional[str]:
    if not category:
        return None
    excluded_set = {e.lower() for e in excluded}
    if category.lower() in excluded_set:
        return category.lower()
    return None


def flag_mandate_mismatch(row: Dict) -> Optional[Dict]:
    """HIGH if category in MDA excluded; MEDIUM if neither scope nor excluded; skip if unclassified."""
    from engines.classifier import classify_with_match

    if row.get('is_mda_level'):
        return None
    ministry = _str_cell(row.get('mda_name') or row.get('ministry'))
    description = _str_cell(row.get('description') or row.get('project_name'))
    if not ministry or not description:
        return None

    category = row.get('_project_category')
    matched_kw = row.get('_category_keyword')
    if category is None and '_project_category' not in row:
        category, matched_kw = classify_with_match(description)
        row['_project_category'] = category
        row['_category_keyword'] = matched_kw

    # Unknown category path: do not fire mismatch
    if not category:
        return None

    meta = _lookup_mda_scope(ministry, row.get('mda_code'))
    if not meta:
        return None

    scope = [s.lower() for s in (meta.get('scope') or [])]
    excluded = [s.lower() for s in (meta.get('excluded') or [])]
    cat = category.lower()

    if cat in scope:
        return None

    if cat in excluded:
        severity = 'HIGH'
        mismatch_kind = 'excluded'
        reason = f'listed in the MDA excluded categories ({cat})'
    else:
        severity = 'MEDIUM'
        mismatch_kind = 'not_in_scope'
        reason = 'neither in the MDA scope nor its excluded list, pending review'

    return {
        'flag_type': 'MANDATE_MISMATCH',
        'severity': severity,
        'title': 'Possible Mandate Violation' if severity == 'HIGH' else 'Possible Mandate Drift',
        'explanation': (
            f'{ministry} is scoped to {", ".join(scope[:5]) or "its statutory functions"}, '
            f'but this project was classified as {cat}. That is {reason}. '
            f'Request the enabling instrument or procurement plan from the MDA.'
        ),
        'evidence': {
            'mda': meta.get('name') or ministry,
            'mda_code': row.get('mda_code'),
            'scope': scope,
            'excluded': excluded,
            'project_category': cat,
            'matched_keyword': matched_kw,
            'mismatch_kind': mismatch_kind,
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

def _composite_desc_fingerprint(desc: str) -> str:
    """Normalize description so only near-identical project lines share a fingerprint.

    Composite codes alone are too coarse on state sheets (many distinct projects share
    one economic + location code). Require the same fingerprint within the code key.
    """
    d = _str_cell(desc).lower()
    d = re.sub(r'\s+', ' ', d).strip()
    # Strip leading enumeration (i. / ii. / 1. / 12.)
    d = re.sub(r'^(?:[ivxlcdm]+|\d+)[.)]\s*', '', d)
    return d


def flag_duplicates_composite(rows: List[Dict]) -> List[Dict]:
    """
    Format B: duplicate when mda_code + economic_code + location_code match AND the
    normalized project description is the same (near-identical line, not merely the
    same procurement class — e.g. two different vehicle buys must not match).
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
        # Sub-cluster by description fingerprint — codes alone over-match on state sheets
        by_desc: Dict[str, List[Dict]] = defaultdict(list)
        for row in group:
            fp = _composite_desc_fingerprint(
                row.get('description') or row.get('project_name')
            )
            if len(fp) < 12:
                continue  # too short / empty — do not invent a duplicate
            by_desc[fp].append(row)

        mda_part, eco_part, loc_part = key.split('|', 2)
        for fp, members in by_desc.items():
            if len(members) < 2:
                continue
            n = len(members)
            amounts = [
                float(r['amount']) for r in members
                if not _is_null_amount(r.get('amount'))
            ]
            total_amount = sum(amounts)
            severity = 'HIGH' if (n >= 3 or total_amount > 100_000_000) else 'MEDIUM'
            explanation = (
                f'MDA {mda_part} repeated the same project line under economic code '
                f'{eco_part} at location {loc_part} {n} times '
                f'(identical description after normalizing enumeration). '
                f'Total double-allocated amount: {_fmt_amount(total_amount)}.'
            )
            cluster_row_ids = [r.get('row_id') for r in members]
            flag = {
                'flag_type':    'COMPOSITE_DUPLICATE',
                'severity':     severity,
                'title':        f'Composite Duplicate ({n}×)',
                'explanation':  explanation,
                'cluster_size': n,
                'matched_rows': cluster_row_ids,
                'evidence': {
                    'composite_key': key,
                    'description_fingerprint': fp[:120],
                },
            }
            for member in members:
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


# ─── Flag: BLANK_APPROVED_AMOUNT (state / Format B) ───────────────────────────

_BLANK_ONGOING_RE = re.compile(
    r'\b(?:'
    r'ongoing|continuation|continuing|roll[\s-]?over|carried?\s+over|'
    r'phase\s*[2-9]|phase\s*ii+|new\s+phase|still\s+under\s+construction|'
    r'completion\s+of'
    r')\b',
    re.I,
)
_BLANK_ANOMALY_PRIOR_NGN = 50_000_000  # large prior spend threshold for MEDIUM escalate


def _blank_amount_ongoing_signal(row: Dict) -> Optional[str]:
    """Return a short label when the line still looks active (not a routine wind-down)."""
    status = _str_cell(row.get('project_status')).upper()
    if status in ('ONGOING', 'NEW', 'CONTINUATION', 'ACTIVE'):
        return f'status={status}'
    desc = _str_cell(row.get('description') or row.get('project_name'))
    m = _BLANK_ONGOING_RE.search(desc)
    if m:
        return f'description:{m.group(0).lower()}'
    return None


def flag_blank_approved_amount(row: Dict) -> Optional[Dict]:
    """Surface blank 2026 approved amounts on state / Format B rows.

    Default LOW/informational: prior-year spend with no 2026 vote is usually a
    completed or discontinued line (normal wind-down), not a red flag.
    Escalate to MEDIUM only on a genuine anomaly — large prior-year spend AND
    an ongoing/new signal (project_status or description).
    Federal Format A/C rows are unaffected (gate on jurisdiction/format_b).
    """
    jurisdiction = str(row.get('_jurisdiction') or '')
    if not (jurisdiction.startswith('state') or row.get('_format_b')):
        return None
    if row.get('is_mda_level'):
        return None
    if not _is_null_amount(row.get('amount')):
        return None

    prior_fields = {}
    max_prior = 0.0
    for key, label in (
        ('actuals_2024', '2024 actuals'),
        ('budget_2025', '2025 revised budget'),
        ('performance_2025', '2025 performance'),
    ):
        val = row.get(key)
        if not _is_null_amount(val) and float(val) != 0:
            prior_fields[label] = float(val)
            max_prior = max(max_prior, float(val))

    ongoing = _blank_amount_ongoing_signal(row)
    anomaly = bool(prior_fields and max_prior >= _BLANK_ANOMALY_PRIOR_NGN and ongoing)

    if anomaly:
        severity = 'MEDIUM'
        title = 'Blank 2026 Approved Amount (Active Line)'
        explanation = (
            'This line has no 2026 approved amount despite large prior-year figures '
            f"({', '.join(f'{k}={_fmt_amount(v)}' for k, v in prior_fields.items())}) "
            f'and still looks active ({ongoing}). Ask whether funding was omitted in error.'
        )
    elif prior_fields:
        severity = 'LOW'
        title = 'Blank 2026 Approved Amount (Likely Wind-Down)'
        explanation = (
            'No 2026 approved amount with prior-year figures present '
            f"({', '.join(f'{k}={_fmt_amount(v)}' for k, v in prior_fields.items())}). "
            'Usually completed or discontinued — informational, not a red flag unless '
            'the project is still marked ongoing.'
        )
    else:
        severity = 'LOW'
        title = 'Blank 2026 Approved Amount'
        explanation = (
            'This capital line has a blank 2026 approved amount in the state budget table. '
            'Informational only; confirm whether funding is nil or omitted.'
        )

    return {
        'flag_type': 'BLANK_APPROVED_AMOUNT',
        'severity': severity,
        'title': title,
        'explanation': explanation,
        'evidence': {
            'amount_2026': None,
            'prior_year_fields': prior_fields,
            'budget_2026': row.get('budget_2026'),
            'max_prior': max_prior or None,
            'ongoing_signal': ongoing,
            'anomaly': anomaly,
            'informational': severity == 'LOW',
        },
    }


# ─── Main runner ──────────────────────────────────────────────────────────────

def run_all_flags(
    df,
    budget_year: Optional[str] = None,
    jurisdiction: str = 'federal',
) -> List[Dict]:
    """Run flag checks and return list of flagged item dicts.

    jurisdiction:
      'federal'     — federal MDA mandates + federal inflation benchmarks
      'state_niger' — state MDA mandates + state inflation benchmarks
                      (also enables Format-B YoY flags when columns present)
    """
    from engines.classifier import classify_with_match

    global LAST_RUN_STATS, ACTIVE_MDA_MANDATES

    is_state = str(jurisdiction or '').startswith('state')
    ACTIVE_MDA_MANDATES = STATE_MDA_MANDATES if is_state else MDA_MANDATES

    rows = df.to_dict('records')

    for row in rows:
        row['_flags'] = []
        row['_exclude'] = False
        row['_jurisdiction'] = jurisdiction
        desc = _str_cell(row.get('description') or row.get('project_name'))
        cat, kw = classify_with_match(desc)
        row['_project_category'] = cat
        row['_category_keyword'] = kw

    unclassified = sum(1 for r in rows if not r.get('_project_category') and not r.get('is_mda_level'))
    LAST_RUN_STATS = {
        'total_items': len(rows),
        'unclassified_count': unclassified,
        'flagged_items': 0,
        'jurisdiction': jurisdiction,
    }
    if unclassified:
        print(f"[flags] unclassified lines: {unclassified} of {len(rows)} "
              f"({100.0 * unclassified / max(len(rows), 1):.1f}%)")

    # Format B (state YoY sheets) carries multi-year amount columns / 8-digit economic codes.
    # Do NOT use mda_code presence — Format C also fills 10-digit MDA codes from section headers.
    is_format_b = bool(is_state)
    if not is_format_b:
        if 'actuals_2024' in df.columns and df['actuals_2024'].notna().any():
            is_format_b = True
        elif 'economic_code' in df.columns and df['economic_code'].notna().any():
            is_format_b = True
        elif 'mda_code' in df.columns:
            sample_codes = (
                df['mda_code'].dropna().astype(str).str.strip()
                .head(50)
            )
            if any(len(c) == 12 and c.isdigit() for c in sample_codes):
                is_format_b = True

    for row in rows:
        row['_format_b'] = is_format_b

    all_descriptions = [_str_cell(r.get('description') or r.get('project_name')) for r in rows]

    # Per-row flags — universal
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

        # Overhead dominance needs Format A MDA-level overhead/capital columns — skip on state
        if not is_state:
            fe = flag_overhead_dominance(row)
            if fe:
                row['_flags'].append(fe)

        # Per-row flags — Format B / state YoY
        if is_format_b:
            f6 = flag_inflated_projection(row)
            if f6:
                row['_flags'].append(f6)

            f7 = flag_phantom_spending(row)
            if f7:
                row['_flags'].append(f7)

            f8 = flag_vague_high_value_spend(row)
            if f8:
                row['_flags'].append(f8)

            f9 = flag_zero_implementation_rollover(row)
            if f9:
                row['_flags'].append(f9)

            fb = flag_blank_approved_amount(row)
            if fb:
                row['_flags'].append(fb)

    # Batch flags (modify rows in-place)
    rows = flag_iqr_outliers(rows)

    if is_format_b:
        rows = flag_duplicates_composite(rows)
    else:
        rows = flag_duplicates(rows)

    rows = flag_budget_splitting(rows)

    def _is_informational_blank_only(flags: List[Dict]) -> bool:
        """Routine blank-amount LOW notes are not primary red flags."""
        if len(flags) != 1:
            return False
        f = flags[0]
        if (f.get('flag_type') or '') != 'BLANK_APPROVED_AMOUNT':
            return False
        return (f.get('severity') or '').upper() == 'LOW'

    # Build final result: only flagged non-excluded rows
    results = []
    blank_low = blank_medium = blank_informational_only = 0
    for row in rows:
        if row.get('_exclude'):
            continue
        flags = row.get('_flags', [])
        if not flags:
            continue

        for f in flags:
            if (f.get('flag_type') or '') != 'BLANK_APPROVED_AMOUNT':
                continue
            if (f.get('severity') or '').upper() == 'MEDIUM':
                blank_medium += 1
            else:
                blank_low += 1

        if _is_informational_blank_only(flags):
            blank_informational_only += 1
            continue  # keep in blank stats; do not inflate flagged rate

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
            'project_category': row.get('_project_category'),
            'mda_code':        row.get('mda_code'),
            'mda_name':        row.get('mda_name') or row.get('ministry'),
            'project_name':    row.get('project_name') or row.get('description'),
            'project_status':  row.get('project_status'),
            'expenditure_code': row.get('expenditure_code') or row.get('economic_code'),
            'data_quality_notes': row.get('data_quality_notes'),
            'economic_code':   row.get('economic_code'),
            'function_code':   row.get('function_code'),
            'location_code':   row.get('location_code'),
            'actuals_2024':    row.get('actuals_2024'),
            'budget_2025':     row.get('budget_2025'),
            'performance_2025': row.get('performance_2025'),
            'budget_2026':     row.get('budget_2026'),
        })

    LAST_RUN_STATS['flagged_items'] = len(results)
    LAST_RUN_STATS['blank_approved_amount'] = {
        'total': blank_low + blank_medium,
        'low': blank_low,
        'medium': blank_medium,
        'informational_only_excluded': blank_informational_only,
    }
    if is_state:
        LAST_RUN_STATS['flags_not_checked'] = [
            {
                'flag_type': 'OVERHEAD_DOMINANCE',
                'reason': 'Not checked for this format — no overhead/capital MDA-level fields',
            },
            {
                'flag_type': 'DUPLICATE_CLUSTER',
                'reason': 'Not checked for this format — state sheets use COMPOSITE_DUPLICATE instead',
            },
            {
                'flag_type': 'GHOST_PROJECT',
                'reason': 'Multi-year ghost detection not checked for this format '
                          '(upload multiple federal years to enable)',
            },
        ]
    else:
        LAST_RUN_STATS['flags_not_checked'] = []
    return results
