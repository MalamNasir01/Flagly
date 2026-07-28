"""Draft MDA mandates generator for newly onboarded state profiles."""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from engines.state_formats.registry import mandates_path_for_jurisdiction


# Shared category vocabulary (kept in sync with federal / niger mandate files)
CATEGORY_VOCABULARY = [
    "roads", "road_maintenance", "bridges", "rail", "airports", "ports",
    "water_transport", "housing", "urban_infrastructure", "health_facilities",
    "medical_equipment", "pharmaceuticals", "disease_control", "primary_schools",
    "secondary_schools", "tertiary_education", "vocational_training",
    "educational_materials", "agriculture_inputs", "irrigation", "livestock",
    "fisheries", "food_storage", "water_supply", "sanitation", "dams",
    "electricity_generation", "electricity_distribution", "renewable_energy",
    "oil_gas", "solid_minerals", "ict_infrastructure", "broadcasting",
    "postal_services", "financial_regulation", "taxation", "customs",
    "military_equipment", "policing", "immigration", "corrections",
    "fire_service", "disaster_response", "refugee_support", "social_welfare",
    "youth_programs", "sports_facilities", "cultural_facilities",
    "tourism_facilities", "markets", "industrial_parks", "trade_facilitation",
    "environmental_protection", "climate_action", "forestry", "gender_programs",
    "child_welfare", "labour_programs", "diplomacy", "justice_administration",
    "research_development", "population_data", "elections", "identity_management",
    "recurrent_admin",
]

# Name-token → likely scope categories (draft heuristics only)
_SCOPE_HINTS: List[Tuple[re.Pattern, List[str]]] = [
    (re.compile(r"education|school|subeb|university|college|polytechnic", re.I),
     ["primary_schools", "secondary_schools", "tertiary_education", "educational_materials", "recurrent_admin"]),
    (re.compile(r"health|hospital|phc|medical|nurs", re.I),
     ["health_facilities", "medical_equipment", "pharmaceuticals", "disease_control", "recurrent_admin"]),
    (re.compile(r"works|road|transport|infrastructure", re.I),
     ["roads", "road_maintenance", "bridges", "urban_infrastructure", "recurrent_admin"]),
    (re.compile(r"water|sanitation", re.I),
     ["water_supply", "sanitation", "dams", "recurrent_admin"]),
    (re.compile(r"agric|livestock|fisher", re.I),
     ["agriculture_inputs", "irrigation", "livestock", "fisheries", "food_storage", "recurrent_admin"]),
    (re.compile(r"environ|climate|forest", re.I),
     ["environmental_protection", "climate_action", "forestry", "recurrent_admin"]),
    (re.compile(r"housing|lands|survey|urban", re.I),
     ["housing", "urban_infrastructure", "recurrent_admin"]),
    (re.compile(r"power|electric|energy", re.I),
     ["electricity_generation", "electricity_distribution", "renewable_energy", "recurrent_admin"]),
    (re.compile(r"women|gender", re.I),
     ["gender_programs", "social_welfare", "recurrent_admin"]),
    (re.compile(r"youth|sport", re.I),
     ["youth_programs", "sports_facilities", "recurrent_admin"]),
    (re.compile(r"justice|court|attorney|judiciar", re.I),
     ["justice_administration", "recurrent_admin"]),
    (re.compile(r"secur|police|homeland|vigilance", re.I),
     ["policing", "disaster_response", "recurrent_admin"]),
    (re.compile(r"budget|planning|finance|account|auditor|revenue", re.I),
     ["financial_regulation", "taxation", "recurrent_admin"]),
    (re.compile(r"ict|digital|information", re.I),
     ["ict_infrastructure", "broadcasting", "recurrent_admin"]),
]


def _infer_scope(mda_name: str) -> List[str]:
    for pat, scope in _SCOPE_HINTS:
        if pat.search(mda_name or ""):
            return list(scope)
    return ["recurrent_admin"]


def _default_excluded(scope: List[str]) -> List[str]:
    # Soft exclusions for draft — keep narrow so unreviewed files only MEDIUM-fire anyway
    heavy = ["military_equipment", "oil_gas", "customs", "immigration"]
    return [c for c in heavy if c not in scope]


def extract_mda_list(rows: List[dict]) -> List[Dict[str, Any]]:
    """Unique MDAs from parsed capital rows."""
    seen = {}
    for row in rows:
        code = str(row.get("mda_code") or "").strip()
        name = str(row.get("mda_name") or row.get("ministry") or "").strip()
        if not name or name.lower() == "nan":
            continue
        key = code or name.upper()
        if key in seen:
            continue
        scope = _infer_scope(name)
        seen[key] = {
            "name": name,
            "aliases": [code, name] if code else [name],
            "mda_code": code or None,
            "scope": scope,
            "excluded": _default_excluded(scope),
        }
    return sorted(seen.values(), key=lambda m: m["name"].lower())


def build_draft_mandates(
    rows: List[dict],
    *,
    jurisdiction: str,
    profile_id: str,
    source_row_count: Optional[int] = None,
) -> dict:
    mdas = extract_mda_list(rows)
    return {
        "_meta": {
            "purpose": (
                f"Draft MDA scope map for {jurisdiction} mandate-mismatch. "
                "Auto-generated from parsed MDA names — scopes are heuristics only."
            ),
            "version": "0.1-draft",
            "jurisdiction": jurisdiction,
            "profile_id": profile_id,
            "reviewed": False,
            "last_updated": date.today().isoformat(),
            "notes": (
                "reviewed=false: mandate-mismatch may fire MEDIUM only (never HIGH / "
                "publishable-tier) until a human sets reviewed=true after scope review."
            ),
            "category_vocabulary": CATEGORY_VOCABULARY,
            "source_row_count": source_row_count if source_row_count is not None else len(rows),
            "mda_count": len(mdas),
        },
        "mdas": mdas,
    }


def write_draft_mandates(doc: dict, path: Optional[str] = None) -> str:
    jurisdiction = (doc.get("_meta") or {}).get("jurisdiction") or "state"
    if path is None:
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "..", "data"
        )
        data_dir = os.path.normpath(data_dir)
        slug = jurisdiction.replace("_state", "").replace("state_", "")
        path = os.path.join(data_dir, f"mda_mandates_states_{slug}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def mandates_reviewed(meta_or_path) -> bool:
    """True when a mandates file has been human-reviewed."""
    if isinstance(meta_or_path, dict):
        meta = meta_or_path.get("_meta") if "mdas" in meta_or_path else meta_or_path
        return bool((meta or {}).get("reviewed"))
    path = meta_or_path
    if not path or not os.path.isfile(path):
        # Legacy niger file without reviewed flag → treat as reviewed (shipped)
        return True
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    meta = doc.get("_meta") or {}
    if "reviewed" not in meta:
        # Backward compatible: existing niger file is considered reviewed
        return True
    return bool(meta.get("reviewed"))
