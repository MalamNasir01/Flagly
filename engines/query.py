"""Natural language query helpers and narrative generation for Flagly scans."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional


def safe_float(val) -> float:
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except Exception:
        return 0.0


def generate_narratives(results: List[Dict]) -> List[Dict]:
    """Deterministic beat reporter style cluster summaries."""
    clusters: Dict[str, List[Dict]] = {}
    for r in results:
        mda = (r.get("mda_name") or r.get("ministry") or "Unknown MDA").strip()
        for f in r.get("flags") or []:
            key = f"{mda}||{f.get('flag_type')}"
            clusters.setdefault(key, []).append(r)

    narratives = []
    for key, items in clusters.items():
        mda, flag_type = key.split("||", 1)
        uniq = {id(i): i for i in items}
        items = list(uniq.values())
        total = sum(safe_float(i.get("amount")) for i in items)
        sample = items[0]
        flag = next((f for f in (sample.get("flags") or []) if f.get("flag_type") == flag_type), None)
        title = (flag or {}).get("title") or flag_type.replace("_", " ").title()
        n = len(items)
        exposure = f"NGN {total:,.0f}"
        body = (
            f"{mda} has {n} line item{'s' if n != 1 else ''} flagged for {title.lower()}. "
            f"Combined exposure is {exposure}. "
            f"{(flag or {}).get('explanation') or 'The scanner marked a repeating pattern in this cluster.'} "
            f"Ask the ministry which contract covers each site and request the bill of quantities. "
            f"Ask for geo tagged completion evidence or an Open Treasury payment trail for the same codes."
        )
        body = body.replace("—", ". ").replace("–", ", ").replace(" - ", ", ")
        narratives.append({
            "cluster_key": key,
            "mda": mda,
            "flag_type": flag_type,
            "item_count": n,
            "total_amount": total,
            "summary": body,
            "journalist_questions": [
                f"Which contract and contractor cover each of the {n} items under {mda}?",
                f"Can {mda} produce geo tagged completion evidence for the {exposure} exposure?",
            ],
        })

    narratives.sort(key=lambda n: n.get("total_amount") or 0, reverse=True)
    return narratives[:40]


def _parse_ngn_threshold(question: str) -> Optional[float]:
    q = question.lower().replace(",", "")
    m = re.search(r"₦?\s*([\d.]+)\s*(billion|bn|million|m|trillion|tn)?", q)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("billion", "bn"):
        return val * 1_000_000_000
    if unit in ("million", "m"):
        return val * 1_000_000
    if unit in ("trillion", "tn"):
        return val * 1_000_000_000_000
    if val >= 1000:
        return val
    return None


def answer_question(question: str, results: List[Dict]) -> Dict[str, Any]:
    """Rule based NL query over scan results. Returns answer + filtered_results."""
    q = question.lower()
    filtered = list(results)
    answer_parts = []
    threshold = _parse_ngn_threshold(question)

    if "no location" in q or "missing location" in q or "without location" in q:
        filtered = [
            r for r in filtered
            if not (r.get("location") and str(r.get("location")).strip() not in ("", "n/a", "-", "None", "nan"))
            or any(f.get("flag_type") in ("MISSING_LOCATION", "VAGUE_LOCATION") for f in (r.get("flags") or []))
        ]
        answer_parts.append(f"Found {len(filtered)} items with missing or vague location.")

    if threshold is not None and ("above" in q or "over" in q or ">" in q):
        filtered = [r for r in filtered if safe_float(r.get("amount")) >= threshold]
        answer_parts.append(f"Restricted to amounts at or above NGN {threshold:,.0f}.")

    if "high risk" in q or "high-risk" in q:
        if "most" in q or "ministry" in q or "mda" in q:
            counts: Dict[str, int] = {}
            for r in results:
                if r.get("risk_level") != "HIGH":
                    continue
                mda = r.get("mda_name") or r.get("ministry") or "Unknown"
                counts[mda] = counts.get(mda, 0) + 1
            if counts:
                top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]
                lines = ", ".join(f"{m} ({c})" for m, c in top)
                answer_parts.append(f"MDAs with the most HIGH risk items: {lines}.")
                top_mda = top[0][0]
                filtered = [
                    r for r in results
                    if (r.get("mda_name") or r.get("ministry")) == top_mda and r.get("risk_level") == "HIGH"
                ]
            else:
                answer_parts.append("No HIGH risk items in the current result set.")
                filtered = []
        else:
            filtered = [r for r in filtered if r.get("risk_level") == "HIGH"]
            answer_parts.append(f"{len(filtered)} HIGH risk items match.")

    if "duplicate" in q:
        filtered = [
            r for r in filtered
            if any(f.get("flag_type") in ("DUPLICATE_CLUSTER", "COMPOSITE_DUPLICATE") for f in (r.get("flags") or []))
        ]
        if threshold is not None:
            kept = []
            for r in filtered:
                total = safe_float(r.get("amount"))
                for f in r.get("flags") or []:
                    if f.get("flag_type") == "DUPLICATE_CLUSTER":
                        cs = f.get("cluster_size") or 1
                        total = max(total, safe_float(r.get("amount")) * cs)
                if total >= threshold:
                    kept.append(r)
            filtered = kept
        answer_parts.append(f"{len(filtered)} duplicate cluster items match.")

    if not answer_parts:
        tokens = [t for t in re.split(r"\W+", q) if len(t) > 3]
        if tokens:
            filtered = [
                r for r in results
                if any(
                    t in (r.get("description") or "").lower()
                    or t in (r.get("mda_name") or "").lower()
                    or t in (r.get("location") or "").lower()
                    for t in tokens
                )
            ]
            answer_parts.append(f"Matched {len(filtered)} items against keywords in your question.")
        else:
            filtered = results[:25]
            answer_parts.append("Showing the top flagged items from the current scan.")

    answer = " ".join(answer_parts).replace("—", ". ").replace("–", ", ")
    return {
        "answer": answer,
        "filtered_results": filtered[:100],
        "match_count": len(filtered),
    }


def visuals_payload(results: List[Dict]) -> Dict[str, Any]:
    """Aggregate series for the Visuals tab (risk donut, MDA bars, unlocated, by year)."""
    risk = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    by_mda: Dict[str, float] = {}
    by_year: Dict[str, float] = {}
    unlocated = 0.0
    for r in results:
        lvl = r.get("risk_level") or "LOW"
        if lvl in risk:
            risk[lvl] += 1
        mda = r.get("mda_name") or r.get("ministry") or "Unknown"
        by_mda[mda] = by_mda.get(mda, 0) + safe_float(r.get("amount"))
        yr = str(r.get("budget_year") or "")
        if yr:
            by_year[yr] = by_year.get(yr, 0) + safe_float(r.get("amount"))
        loc = r.get("location")
        if loc is None or (isinstance(loc, float) and math.isnan(loc)):
            loc = ""
        else:
            loc = str(loc).strip()
        if not loc or loc.lower() in ("n/a", "none", "nan", "-"):
            unlocated += safe_float(r.get("amount"))

    top_mda = sorted(by_mda.items(), key=lambda x: x[1], reverse=True)[:15]
    return {
        "risk_counts": risk,
        "mda_bars": [{"mda": m, "amount": a} for m, a in top_mda],
        "unlocated_amount": unlocated,
        "by_year": [{"year": y, "amount": by_year[y]} for y in sorted(by_year.keys())],
        "colors": {"HIGH": "#E63946", "MEDIUM": "#D97706", "LOW": "#1E4272"},
    }
