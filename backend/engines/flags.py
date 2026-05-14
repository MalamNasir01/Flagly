"""
flags.py — Flagly Budget Scanner
Four red-flag engines that run on a standardised budget DataFrame.

Each engine returns a list of dicts:
    {
        "row_id": int,
        "flag_type": str,
        "severity": "HIGH" | "MEDIUM" | "LOW",
        "score": float (0–100),
        "reason": str,
        "detail": str
    }
"""

import re
import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process
from typing import List, Dict, Any


# ─────────────────────────────────────────────
# 1. DUPLICATE DETECTOR
# ─────────────────────────────────────────────

def flag_duplicates(df: pd.DataFrame, threshold: int = 85) -> List[Dict]:
    """
    Find near-identical line item descriptions using fuzzy matching.
    Flags pairs where similarity >= threshold%.
    Also catches exact duplicates (100% match).
    """
    flags = []
    descriptions = df["description"].tolist()
    seen_pairs = set()

    for i, desc_a in enumerate(descriptions):
        if len(desc_a) < 10:
            continue
        # rapidfuzz: compare against all subsequent items
        for j in range(i + 1, len(descriptions)):
            desc_b = descriptions[j]
            if len(desc_b) < 10:
                continue
            pair_key = (min(i, j), max(i, j))
            if pair_key in seen_pairs:
                continue

            score = fuzz.token_sort_ratio(desc_a, desc_b)
            if score >= threshold:
                seen_pairs.add(pair_key)
                severity = "HIGH" if score >= 95 else "MEDIUM"
                amt_a = df.at[i, "amount"]
                amt_b = df.at[j, "amount"]
                amt_note = ""
                if pd.notna(amt_a) and pd.notna(amt_b):
                    combined = amt_a + amt_b
                    amt_note = (f" Combined exposure: ₦{combined:,.0f}.")

                for row_id in [i, j]:
                    flags.append({
                        "row_id": int(row_id),
                        "flag_type": "DUPLICATE",
                        "severity": severity,
                        "score": float(score),
                        "reason": f"{score:.0f}% match with row {j if row_id == i else i}",
                        "detail": (
                            f'Near-identical to: "{descriptions[j if row_id == i else i][:80]}...". '
                            f"Similarity: {score:.0f}%.{amt_note}"
                        )
                    })

    return flags


# ─────────────────────────────────────────────
# 2. AMOUNT ANOMALY ENGINE
# ─────────────────────────────────────────────

# Nigerian budget categories with realistic typical ranges (in Naira)
CATEGORY_KEYWORDS: Dict[str, tuple] = {
    "road":             (50_000_000,   1_000_000_000),
    "bridge":           (100_000_000,  1_000_000_000),
    "school":           (5_000_000,    200_000_000),
    "classroom":        (2_000_000,    50_000_000),
    "hospital":         (50_000_000,   1_000_000_000),
    "clinic":           (10_000_000,   200_000_000),
    "borehole":         (1_000_000,    20_000_000),
    "toilet":           (500_000,      10_000_000),
    "renovation":       (1_000_000,    100_000_000),
    "rehabilitation":   (5_000_000,    500_000_000),
    "furniture":        (500_000,      50_000_000),
    "vehicle":          (5_000_000,    50_000_000),
    "training":         (500_000,      20_000_000),
    "workshop":         (500_000,      10_000_000),
    "stationery":       (100_000,      5_000_000),
    "printing":         (200_000,      10_000_000),
    "consultancy":      (1_000_000,    100_000_000),
    "study":            (500_000,      20_000_000),
    "empowerment":      (1_000_000,    100_000_000),
    "overhead":         (1_000_000,    500_000_000),
}

BILLION = 1_000_000_000

def _get_category(description: str) -> str:
    desc_l = description.lower()
    for kw in CATEGORY_KEYWORDS:
        if kw in desc_l:
            return kw
    return "general"


def flag_amount_anomalies(df: pd.DataFrame) -> List[Dict]:
    """
    Flags items with:
    a) Amount > 3× the IQR-based upper fence for its peer group
    b) Amount outside known typical range for the project category
    c) Items above ₦10bn with no specific location (near-guaranteed flag)
    """
    flags = []
    amount_df = df[df["amount"].notna() & (df["amount"] > 0)].copy()
    if amount_df.empty:
        return flags

    # 1-billion naira hard ceiling — always flag regardless of category
    for _, row in amount_df.iterrows():
        amt = row["amount"]
        if amt >= BILLION:
            flags.append({
                "row_id": int(row["row_id"]),
                "flag_type": "AMOUNT_ANOMALY",
                "severity": "HIGH",
                "score": 95.0,
                "reason": f"₦{amt:,.0f} exceeds ₦1 billion threshold",
                "detail": (
                    f"Any single line item above ₦1,000,000,000 requires scrutiny. "
                    f"This item is ₦{amt/BILLION:.2f} billion. "
                    f"Verify this is not split funding or a duplicate rolled into one line."
                )
            })

    # Category-aware range check (for items under 1B)
    for _, row in amount_df.iterrows():
        amt = row["amount"]
        if amt >= BILLION:
            continue  # already flagged above
        desc = str(row["description"])
        cat = _get_category(desc)

        if cat in CATEGORY_KEYWORDS:
            low, high = CATEGORY_KEYWORDS[cat]
            if amt > high * 10:
                flags.append({
                    "row_id": int(row["row_id"]),
                    "flag_type": "AMOUNT_ANOMALY",
                    "severity": "HIGH",
                    "score": 90.0,
                    "reason": f"₦{amt:,.0f} is far above typical range for '{cat}' projects",
                    "detail": (
                        f"Typical '{cat}' projects cost ₦{low:,.0f}–₦{high:,.0f}. "
                        f"This item is {amt/high:.0f}× the upper typical bound."
                    )
                })
            elif amt > high * 3:
                flags.append({
                    "row_id": int(row["row_id"]),
                    "flag_type": "AMOUNT_ANOMALY",
                    "severity": "MEDIUM",
                    "score": 65.0,
                    "reason": f"₦{amt:,.0f} is unusually high for a '{cat}' project",
                    "detail": (
                        f"Typical '{cat}' projects cost ₦{low:,.0f}–₦{high:,.0f}. "
                        f"This item is {amt/high:.1f}× the typical upper bound."
                    )
                })

    # Statistical outlier within the full dataset
    amounts = amount_df["amount"].values
    if len(amounts) >= 10:
        q1, q3 = np.percentile(amounts, 25), np.percentile(amounts, 75)
        iqr = q3 - q1
        upper_fence = q3 + 3.0 * iqr

        for _, row in amount_df.iterrows():
            amt = row["amount"]
            if amt > upper_fence:
                already_flagged = any(
                    f["row_id"] == int(row["row_id"]) and f["flag_type"] == "AMOUNT_ANOMALY"
                    for f in flags
                )
                if not already_flagged:
                    flags.append({
                        "row_id": int(row["row_id"]),
                        "flag_type": "AMOUNT_ANOMALY",
                        "severity": "MEDIUM",
                        "score": 60.0,
                        "reason": f"₦{amt:,.0f} is a statistical outlier (above IQR upper fence of ₦{upper_fence:,.0f})",
                        "detail": (
                            f"Dataset median: ₦{np.median(amounts):,.0f}. "
                            f"This item is {amt / np.median(amounts):.1f}× the median allocation."
                        )
                    })

    return flags


# ─────────────────────────────────────────────
# 3. LOCATION CHECKER
# ─────────────────────────────────────────────

KNOWN_STATES = [
    "abia", "adamawa", "akwa ibom", "anambra", "bauchi", "bayelsa", "benue",
    "borno", "cross river", "delta", "ebonyi", "edo", "ekiti", "enugu",
    "gombe", "imo", "jigawa", "kaduna", "kano", "katsina", "kebbi", "kogi",
    "kwara", "lagos", "nasarawa", "niger", "ogun", "ondo", "osun", "oyo",
    "plateau", "rivers", "sokoto", "taraba", "yobe", "zamfara", "fct", "abuja",
    "federal capital territory"
]

VAGUE_LOCATION_PATTERNS = [
    r"^various\b", r"^nationwide\b", r"^national\b",
    r"^all states\b", r"^federal\b", r"^tbd\b", r"^n/a\b",
    r"^nil\b", r"^none\b"
]

def _has_real_location(location_val) -> bool:
    if pd.isna(location_val) or str(location_val).strip() in ("", "nan", "None", "-", "N/A"):
        return False
    loc_l = str(location_val).lower().strip()
    for pattern in VAGUE_LOCATION_PATTERNS:
        if re.match(pattern, loc_l):
            return False
    return True


def _description_has_location(desc: str) -> bool:
    """Check if description itself contains a recognisable Nigerian place name."""
    desc_l = desc.lower()
    return any(state in desc_l for state in KNOWN_STATES)


def flag_missing_locations(df: pd.DataFrame) -> List[Dict]:
    """
    Flags items where:
    - No location column value AND description contains no place name
    - Only flags items above ₦5m to avoid noise on petty items
    """
    flags = []
    MIN_AMOUNT = 5_000_000  # Only flag if ≥ ₦5m

    for _, row in df.iterrows():
        amt = row.get("amount")
        if pd.notna(amt) and amt < MIN_AMOUNT:
            continue

        has_loc_col = _has_real_location(row.get("location"))
        has_loc_desc = _description_has_location(str(row["description"]))

        if not has_loc_col and not has_loc_desc:
            amt_str = f"₦{amt:,.0f}" if pd.notna(amt) else "unknown amount"
            flags.append({
                "row_id": int(row["row_id"]),
                "flag_type": "MISSING_LOCATION",
                "severity": "MEDIUM",
                "score": 55.0,
                "reason": "No traceable location or constituency identified",
                "detail": (
                    f"Item ({amt_str}) has no state, LGA, ward, or constituency. "
                    "Without a location, there is no way to verify delivery or hold anyone accountable."
                )
            })

    return flags


# ─────────────────────────────────────────────
# 4. GHOST PROJECT / RECURRENCE DETECTOR
# (Runs across multiple uploaded files / years)
# ─────────────────────────────────────────────

def flag_ghost_projects(
    dfs_by_year: Dict[str, pd.DataFrame],
    similarity_threshold: int = 82
) -> List[Dict]:
    """
    Compares budget line items across multiple years.
    Flags items that reappear year-on-year without evidence of completion.

    dfs_by_year: {"2022": df_2022, "2023": df_2023, ...}
    """
    flags = []
    years = sorted(dfs_by_year.keys())
    if len(years) < 2:
        return flags  # Need at least 2 years to detect recurrence

    # Build a flat list: (year, row_id, description, amount)
    all_items = []
    for year, df in dfs_by_year.items():
        for _, row in df.iterrows():
            all_items.append({
                "year": year,
                "row_id": int(row["row_id"]),
                "description": str(row["description"]),
                "amount": row.get("amount"),
            })

    # For each item in year N, search for a match in year N+1, N+2...
    for i, item_a in enumerate(all_items):
        for item_b in all_items:
            if item_b["year"] <= item_a["year"]:
                continue
            score = fuzz.token_sort_ratio(item_a["description"], item_b["description"])
            if score >= similarity_threshold:
                years_apart = int(item_b["year"]) - int(item_a["year"])
                severity = "HIGH" if years_apart >= 2 else "MEDIUM"
                flags.append({
                    "row_id": item_a["row_id"],
                    "year": item_a["year"],
                    "flag_type": "GHOST_PROJECT",
                    "severity": severity,
                    "score": float(score),
                    "reason": (
                        f"Same project re-appropriated in {item_b['year']} "
                        f"({years_apart} year(s) later)"
                    ),
                    "detail": (
                        f'"{item_a["description"][:70]}..." appeared in {item_a["year"]} '
                        f'and again in {item_b["year"]} with {score:.0f}% similarity. '
                        f"If not completed, this is double-spending. "
                        f"If completed, re-appropriation needs justification."
                    )
                })

    return flags


# ─────────────────────────────────────────────
# COMPOSITE RISK SCORER
# ─────────────────────────────────────────────

SEVERITY_WEIGHTS = {"HIGH": 40, "MEDIUM": 20, "LOW": 10}
FLAG_TYPE_WEIGHTS = {
    "DUPLICATE": 1.2,
    "AMOUNT_ANOMALY": 1.3,
    "MISSING_LOCATION": 0.8,
    "GHOST_PROJECT": 1.5,
}

def compute_risk_scores(
    df: pd.DataFrame,
    all_flags: List[Dict]
) -> pd.DataFrame:
    """
    Attaches a composite risk score (0–100) and flag list to each row.
    Returns the original df with extra columns: risk_score, flags, risk_level.
    """
    scores = {i: 0.0 for i in df["row_id"].tolist()}
    flag_map = {i: [] for i in df["row_id"].tolist()}

    for f in all_flags:
        rid = f["row_id"]
        if rid not in scores:
            continue
        base = SEVERITY_WEIGHTS.get(f["severity"], 10)
        weight = FLAG_TYPE_WEIGHTS.get(f["flag_type"], 1.0)
        scores[rid] = min(100, scores[rid] + base * weight)
        flag_map[rid].append(f)

    df = df.copy()
    df["risk_score"] = df["row_id"].map(scores).round(1)
    df["flags"] = df["row_id"].map(flag_map)
    df["risk_level"] = df["risk_score"].apply(
        lambda s: "HIGH" if s >= 60 else ("MEDIUM" if s >= 25 else "LOW")
    )
    df["flag_count"] = df["flags"].apply(len)

    return df.sort_values("risk_score", ascending=False)
