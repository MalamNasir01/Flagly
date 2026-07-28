"""Profile registry — discover and load state budget format profiles.

Adding a state = drop a JSON under profiles/ + optional mandates file.
No parser fork required.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional


def profiles_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "profiles")


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_profile_files() -> List[str]:
    d = profiles_dir()
    if not os.path.isdir(d):
        return []
    return sorted(
        f for f in os.listdir(d) if f.endswith(".json") and not f.startswith(".")
    )


def list_profiles() -> List[str]:
    """Return stable profile ids (the JSON `id` field), e.g. state_niger."""
    ids = []
    for fname in list_profile_files():
        try:
            data = _read_json(os.path.join(profiles_dir(), fname))
            pid = data.get("id") or fname.replace(".json", "")
            ids.append(pid)
        except Exception:
            continue
    return ids


def load_profile(profile_id: str) -> dict:
    """Load a profile by id (`state_niger`) or filename stem (`niger_v1`)."""
    mapping_aliases = {
        "niger_v1": "niger_v1.json",
        "state_niger": "niger_v1.json",
        "kaduna_v1": "kaduna_v1.json",
        "state_kaduna": "kaduna_v1.json",
    }
    # Prefer explicit filename map, then scan for matching id
    fname = mapping_aliases.get(profile_id)
    if fname:
        path = os.path.join(profiles_dir(), fname)
        if os.path.isfile(path):
            return _read_json(path)

    direct = os.path.join(profiles_dir(), f"{profile_id}.json")
    if os.path.isfile(direct):
        return _read_json(direct)

    for fname in list_profile_files():
        data = _read_json(os.path.join(profiles_dir(), fname))
        if data.get("id") == profile_id:
            return data
        stem = fname.replace(".json", "")
        if stem == profile_id:
            return data

    raise FileNotFoundError(f"No state format profile for {profile_id!r}")


def profile_summary(profile: dict) -> Dict[str, Any]:
    return {
        "id": profile.get("id"),
        "label": profile.get("label"),
        "jurisdiction": profile.get("jurisdiction"),
        "version": profile.get("version"),
        "amount_column": profile.get("amount_column"),
    }


def supported_format_catalog() -> List[Dict[str, str]]:
    """Human-facing list of supported budget formats (federal + state profiles)."""
    catalog = [
        {
            "id": "federal_fgn",
            "label": "Federal FGN Appropriation Bill",
            "kind": "federal",
        }
    ]
    for pid in list_profiles():
        try:
            p = load_profile(pid)
        except Exception:
            continue
        catalog.append(
            {
                "id": pid,
                "label": p.get("label") or pid,
                "kind": "state",
                "jurisdiction": p.get("jurisdiction") or "",
            }
        )
    return catalog


def mandates_path_for_jurisdiction(jurisdiction: str) -> Optional[str]:
    """Resolve data/mda_mandates_states_<slug>.json (or legacy niger file)."""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "data")
    data_dir = os.path.normpath(data_dir)
    slug = (jurisdiction or "").replace("_state", "").replace("state_", "").strip("_")
    candidates = [
        f"mda_mandates_states_{slug}.json",
        f"mda_mandates_states_{jurisdiction}.json",
    ]
    # Legacy Niger file
    if "niger" in (jurisdiction or "").lower() or slug == "niger":
        candidates.append("mda_mandates_states.json")
    for name in candidates:
        path = os.path.join(data_dir, name)
        if os.path.isfile(path):
            return path
    return None
