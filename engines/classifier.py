"""
classifier.py — Deterministic project category classifier for Flagly.

Tie break (documented in data/category_keywords.json _meta.tie_break):
  1. Phrase overrides (forced category or hard-block) apply first.
  2. Domain categories beat catch-all admin categories (recurrent_admin).
  3. Longest matching keyword wins (most specific).
  4. If equal length, earlier category in mda_mandates._meta.category_vocabulary wins.

Disambiguation: category_exclusions suppress a category when a context phrase is present
(e.g. labour_programs blocked by "direct labour").
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Set, Tuple


# Generic admin catch-alls — deprioritized so "installation of solar lights"
# maps to renewable_energy via "solar lights", not recurrent_admin via "installation of".
CATCHALL_CATEGORIES: Set[str] = {'recurrent_admin'}


def _data_path(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', name)


def _load_json(name: str, default):
    try:
        with open(_data_path(name), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'[classifier] could not load {name}: {e}')
        return default


_KEYWORDS_DOC = _load_json('category_keywords.json', {})
_MANDATES_DOC = _load_json('mda_mandates.json', {})
_FLAG_CONFIG = _load_json('flag_config.json', {})

VOCABULARY: List[str] = list(
    (_MANDATES_DOC.get('_meta') or {}).get('category_vocabulary')
    or list((_KEYWORDS_DOC.get('categories') or {}).keys())
)

CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    k: [kw.lower() for kw in (v or [])]
    for k, v in (_KEYWORDS_DOC.get('categories') or {}).items()
}

CATEGORY_EXCLUSIONS: Dict[str, List[str]] = {
    k: [p.lower() for p in (v or [])]
    for k, v in (_KEYWORDS_DOC.get('category_exclusions') or {}).items()
}

# (phrase, category_or_None) sorted longest-first
PHRASE_OVERRIDES: List[Tuple[str, Optional[str]]] = sorted(
    [
        (str(item.get('phrase', '')).lower(), item.get('category'))
        for item in (_KEYWORDS_DOC.get('phrase_overrides') or [])
        if item.get('phrase')
    ],
    key=lambda x: -len(x[0]),
)

_VOCAB_INDEX = {cat: i for i, cat in enumerate(VOCABULARY)}


def _sorted_keyword_index() -> List[Tuple[str, str, int, int]]:
    """Return list of (category, keyword, -len(keyword), vocab_index)."""
    rows = []
    for cat, kws in CATEGORY_KEYWORDS.items():
        vi = _VOCAB_INDEX.get(cat, 10_000)
        for kw in kws:
            rows.append((cat, kw, -len(kw), vi))
    rows.sort(key=lambda r: (r[2], r[3], r[1]))
    return rows


_KEYWORD_INDEX = _sorted_keyword_index()


def _keyword_in_text(text: str, kw: str) -> bool:
    """Case-folded substring match with word-boundary protection for short tokens.

    Short alphanumeric keywords (len <= 4) such as 'hiv' / 'adr' must not match
    inside longer words ('archived', 'address'). Keywords that already include
    spacing or punctuation keep plain substring matching.
    """
    if not kw:
        return False
    if len(kw) <= 4 and kw.replace(' ', '').isalnum() and ' ' not in kw:
        import re
        return re.search(rf'(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])', text) is not None
    return kw in text


def _category_blocked(category: str, text: str) -> bool:
    for phrase in CATEGORY_EXCLUSIONS.get(category) or []:
        if phrase and phrase in text:
            return True
    return False


def classify_with_match(description: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (category, matched_keyword_or_override_phrase) for evidence/logging."""
    if not description:
        return None, None
    text = description.lower()

    # 1) Phrase overrides — forced category wins immediately.
    for phrase, forced in PHRASE_OVERRIDES:
        if phrase and phrase in text and forced is not None:
            return forced, phrase

    best = None
    best_kw = None
    for cat, kw, neg_len, vi in _KEYWORD_INDEX:
        if not _keyword_in_text(text, kw):
            continue
        if _category_blocked(cat, text):
            continue
        # Sort key: domain first, then longest keyword, then vocabulary order.
        is_catchall = 1 if cat in CATCHALL_CATEGORIES else 0
        candidate = (is_catchall, neg_len, vi, cat)
        if best is None or candidate < best:
            best = candidate
            best_kw = kw
    return (best[3], best_kw) if best else (None, None)


def classify_project(description: str) -> Optional[str]:
    """Return a vocabulary category or None if unclassified."""
    cat, _ = classify_with_match(description)
    return cat


def get_flag_config(reload: bool = False) -> dict:
    """Return flag_config.json (cached). Pass reload=True after editing the file."""
    global _FLAG_CONFIG
    if reload or not _FLAG_CONFIG:
        _FLAG_CONFIG = _load_json('flag_config.json', _FLAG_CONFIG or {})
    return _FLAG_CONFIG


def reload_classifier_data() -> None:
    """Reload keyword maps, vocabulary, and flag config (for tests / hot config)."""
    global _KEYWORDS_DOC, _MANDATES_DOC, VOCABULARY, CATEGORY_KEYWORDS
    global CATEGORY_EXCLUSIONS, PHRASE_OVERRIDES, _VOCAB_INDEX, _KEYWORD_INDEX, _FLAG_CONFIG
    _KEYWORDS_DOC = _load_json('category_keywords.json', {})
    _MANDATES_DOC = _load_json('mda_mandates.json', {})
    _FLAG_CONFIG = _load_json('flag_config.json', {})
    VOCABULARY = list(
        (_MANDATES_DOC.get('_meta') or {}).get('category_vocabulary')
        or list((_KEYWORDS_DOC.get('categories') or {}).keys())
    )
    CATEGORY_KEYWORDS = {
        k: [kw.lower() for kw in (v or [])]
        for k, v in (_KEYWORDS_DOC.get('categories') or {}).items()
    }
    CATEGORY_EXCLUSIONS = {
        k: [p.lower() for p in (v or [])]
        for k, v in (_KEYWORDS_DOC.get('category_exclusions') or {}).items()
    }
    PHRASE_OVERRIDES = sorted(
        [
            (str(item.get('phrase', '')).lower(), item.get('category'))
            for item in (_KEYWORDS_DOC.get('phrase_overrides') or [])
            if item.get('phrase')
        ],
        key=lambda x: -len(x[0]),
    )
    _VOCAB_INDEX = {cat: i for i, cat in enumerate(VOCABULARY)}
    _KEYWORD_INDEX = _sorted_keyword_index()


def get_inflated_benchmark(category: str, description: Optional[str] = None) -> Optional[float]:
    meta = get_inflated_benchmark_meta(category, description)
    return meta.get('benchmark')


def get_inflated_benchmark_meta(
    category: str,
    description: Optional[str] = None,
    jurisdiction: str = "federal",
) -> dict:
    """Return benchmark + which tier was selected (for evidence).

    jurisdiction='state*' uses inflated.benchmarks_ngn_state when present.
    """
    cfg = get_flag_config()
    inflated = cfg.get('inflated') or {}
    use_state = str(jurisdiction or "").lower().startswith("state")
    if use_state and inflated.get("benchmarks_ngn_state"):
        benchmarks = inflated.get("benchmarks_ngn_state") or {}
        tiers_root = inflated.get("benchmark_tiers_state") or inflated.get("benchmark_tiers") or {}
    else:
        benchmarks = inflated.get("benchmarks_ngn") or {}
        tiers_root = inflated.get("benchmark_tiers") or {}
    base = benchmarks.get(category)
    if base is None:
        return {'benchmark': None, 'tier': None, 'matched_tier_keyword': None, 'jurisdiction': jurisdiction}
    tiers = tiers_root.get(category) or {}
    large_kws = [str(k).lower() for k in (tiers.get('large_keywords') or [])]
    large_bench = tiers.get('large_benchmark_ngn')
    if description and large_kws and large_bench is not None:
        text = description.lower()
        for kw in sorted(large_kws, key=len, reverse=True):
            if kw and kw in text:
                return {
                    'benchmark': float(large_bench),
                    'tier': 'large',
                    'matched_tier_keyword': kw,
                    'jurisdiction': jurisdiction,
                }
    return {
        'benchmark': float(base),
        'tier': 'default',
        'matched_tier_keyword': None,
        'jurisdiction': jurisdiction,
    }


def get_inflated_multiplier() -> float:
    return float((get_flag_config().get('inflated') or {}).get('multiplier', 3.0))


def get_inflated_high_multiplier() -> float:
    return float((get_flag_config().get('inflated') or {}).get('high_multiplier', 5.0))
