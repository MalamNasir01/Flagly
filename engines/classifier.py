"""
classifier.py — Deterministic project category classifier for Flagly.

Tie break (documented in data/category_keywords.json _meta.tie_break):
  1. Longest matching keyword wins (most specific).
  2. If equal length, earlier category in mda_mandates._meta.category_vocabulary wins.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple


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

# Precompute (category, keyword) pairs sorted by keyword length desc, then vocab order.
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


def classify_project(description: str) -> Optional[str]:
    """Return a vocabulary category or None if unclassified.

    Matching is case insensitive substring search. Tie break: longest keyword,
    then earlier vocabulary order.
    """
    if not description:
        return None
    text = description.lower()
    best = None  # (neg_len, vocab_index, category)
    for cat, kw, neg_len, vi in _KEYWORD_INDEX:
        if kw and kw in text:
            candidate = (neg_len, vi, cat)
            if best is None or candidate < best:
                best = candidate
    return best[2] if best else None


def classify_with_match(description: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (category, matched_keyword) for evidence/logging."""
    if not description:
        return None, None
    text = description.lower()
    best = None
    best_kw = None
    for cat, kw, neg_len, vi in _KEYWORD_INDEX:
        if kw and kw in text:
            candidate = (neg_len, vi, cat)
            if best is None or candidate < best:
                best = candidate
                best_kw = kw
    return (best[2], best_kw) if best else (None, None)


def get_flag_config(reload: bool = False) -> dict:
    """Return flag_config.json (cached). Pass reload=True after editing the file."""
    global _FLAG_CONFIG
    if reload or not _FLAG_CONFIG:
        _FLAG_CONFIG = _load_json('flag_config.json', _FLAG_CONFIG or {})
    return _FLAG_CONFIG


def reload_classifier_data() -> None:
    """Reload keyword maps, vocabulary, and flag config (for tests / hot config)."""
    global _KEYWORDS_DOC, _MANDATES_DOC, VOCABULARY, CATEGORY_KEYWORDS, _VOCAB_INDEX, _KEYWORD_INDEX, _FLAG_CONFIG
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
    _VOCAB_INDEX = {cat: i for i, cat in enumerate(VOCABULARY)}
    _KEYWORD_INDEX = _sorted_keyword_index()


def get_inflated_benchmark(category: str, description: Optional[str] = None) -> Optional[float]:
    """Return the Naira benchmark for a category.

    If ``benchmark_tiers`` defines large_keywords for the category and the
    description matches any of them, use ``large_benchmark_ngn`` (national-scale
    facilities). Otherwise use the flat ``benchmarks_ngn`` value (local/small).
    """
    meta = get_inflated_benchmark_meta(category, description)
    return meta.get('benchmark')


def get_inflated_benchmark_meta(category: str, description: Optional[str] = None) -> dict:
    """Return benchmark + which tier was selected (for evidence)."""
    cfg = get_flag_config()
    inflated = cfg.get('inflated') or {}
    benchmarks = inflated.get('benchmarks_ngn') or {}
    base = benchmarks.get(category)
    if base is None:
        return {'benchmark': None, 'tier': None, 'matched_tier_keyword': None}
    tiers = (inflated.get('benchmark_tiers') or {}).get(category) or {}
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
                }
    return {
        'benchmark': float(base),
        'tier': 'default',
        'matched_tier_keyword': None,
    }


def get_inflated_multiplier() -> float:
    return float((_FLAG_CONFIG.get('inflated') or {}).get('multiplier', 3.0))


def get_inflated_high_multiplier() -> float:
    return float((_FLAG_CONFIG.get('inflated') or {}).get('high_multiplier', 5.0))
