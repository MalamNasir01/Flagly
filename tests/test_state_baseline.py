"""Frozen Niger State parse baseline — gate for row-boundary / merge regressions.

Golden numbers live in samples/state_niger_baseline.json.
The full PDF comparison runs only when the Niger Approved Budget PDF is present.
"""

from __future__ import annotations

import json
import os
import unittest

BASELINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "samples",
    "state_niger_baseline.json",
)

_PDF_CANDIDATES = [
    os.environ.get("FLAGLY_NIGER_PDF", ""),
    os.path.expanduser("~/Desktop/NIGER-STATE-APPROVED-2026-BUDGET.pdf"),
    "/Users/spade_nas1/Desktop/NIGER-STATE-APPROVED-2026-BUDGET.pdf",
]

_TOLERANCE = {
    "total_items": 30,
    "flagged_items": 40,
    "mandate_high": 5,
    "mandate_medium": 25,
    "parse_quality_suspects": 40,
}


def _load_baseline() -> dict:
    with open(BASELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _find_pdf() -> str | None:
    for path in _PDF_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


class StateNigerBaselineFileTests(unittest.TestCase):
    def test_baseline_file_exists_and_notes_demerge(self):
        self.assertTrue(os.path.isfile(BASELINE_PATH), "missing samples/state_niger_baseline.json")
        base = _load_baseline()
        self.assertGreater(base["total_items"], base["prior_buggy_item_count"])
        self.assertEqual(base["prior_buggy_item_count"], 1014)
        self.assertEqual(len(base.get("top10_by_amount") or []), 10)
        self.assertIn("parse_quality", base)
        # Top 10 must be single coherent projects (no merge-quality hits)
        for row in base["top10_by_amount"]:
            self.assertEqual(row.get("merge_issues") or [], [], row.get("description"))


@unittest.skipUnless(_find_pdf(), "Niger State Approved Budget PDF not available")
class StateNigerBaselineRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from engines.state_formats import parse_state_pdf
        from engines.state_formats.parser import description_merge_issues
        from engines.classifier import reload_classifier_data
        import engines.flags as flags
        from engines.flags import run_all_flags
        from engines.scorer import score_items
        from collections import Counter

        reload_classifier_data()
        flags.MDA_MANDATES = flags._normalize_mda_mandates(
            flags._load_json("mda_mandates.json", {})
        )
        state_m = flags._load_json("mda_mandates_states.json", {})
        flags.ACTIVE_MDA_MANDATES = (
            flags._normalize_mda_mandates(state_m) if state_m else flags.MDA_MANDATES
        )

        pdf_path = _find_pdf()
        df, meta = parse_state_pdf(open(pdf_path, "rb").read())
        scored = score_items(run_all_flags(df, budget_year="2026", jurisdiction="state"))
        types = Counter()
        mandate = Counter()
        for r in scored:
            for f in r.get("flags") or []:
                types[f["flag_type"]] += 1
                if f["flag_type"] == "MANDATE_MISMATCH":
                    mandate[(f.get("severity") or "").upper()] += 1

        top = df.sort_values("amount", ascending=False, na_position="last").head(10)
        cls.stats = {
            "total_items": len(df),
            "flagged_items": len(scored),
            "mandate_high": mandate.get("HIGH", 0),
            "mandate_medium": mandate.get("MEDIUM", 0),
            "parse_quality_suspects": (meta.get("parse_quality") or {}).get("suspect_rows", 0),
            "top10": [
                {
                    "description": str(r.description),
                    "amount": float(r.amount) if r.amount == r.amount else None,
                    "merge_issues": description_merge_issues(str(r.description)),
                }
                for _, r in top.iterrows()
            ],
        }
        cls.baseline = _load_baseline()
        cls.df = df

    def test_item_count_within_tolerance(self):
        delta = abs(self.stats["total_items"] - self.baseline["total_items"])
        self.assertLessEqual(
            delta,
            _TOLERANCE["total_items"],
            f"items {self.stats['total_items']} vs baseline {self.baseline['total_items']}",
        )

    def test_flagged_and_mandate_within_tolerance(self):
        for key in ("flagged_items", "mandate_high", "mandate_medium"):
            delta = abs(self.stats[key] - self.baseline[key])
            self.assertLessEqual(
                delta,
                _TOLERANCE[key],
                f"{key} {self.stats[key]} vs baseline {self.baseline[key]}",
            )

    def test_parse_quality_suspects_bounded(self):
        delta = abs(
            self.stats["parse_quality_suspects"]
            - self.baseline["parse_quality"]["suspect_rows"]
        )
        self.assertLessEqual(
            delta,
            _TOLERANCE["parse_quality_suspects"],
            f"suspects {self.stats['parse_quality_suspects']} vs "
            f"{self.baseline['parse_quality']['suspect_rows']}",
        )

    def test_top10_are_single_projects(self):
        for row in self.stats["top10"]:
            self.assertEqual(row["merge_issues"], [], row["description"])

    def test_evidence_pairs_demerged(self):
        descs = self.df["description"].astype(str)
        soft = descs[descs.str.contains("Software for MDAs", case=False, na=False, regex=False)]
        roads = descs[
            descs.str.contains(
                "10km Rural Roads and Markets)World Bank at Katcha",
                case=False,
                na=False,
                regex=False,
            )
        ]
        self.assertEqual(len(soft), 1)
        self.assertEqual(len(roads), 1)
        self.assertNotIn("Construction", soft.iloc[0])
        self.assertNotIn("Software", roads.iloc[0])

        surwash = descs[descs.str.contains("SURWASH)", case=False, na=False, regex=False)]
        lenfa = descs[descs.str.contains("Lenfa", case=False, na=False, regex=False)]
        self.assertGreaterEqual(len(surwash), 1)
        self.assertEqual(len(lenfa), 1)
        self.assertNotIn("Lenfa", surwash.iloc[0])
        self.assertNotIn("SURWASH", lenfa.iloc[0])

        mokwa = descs[descs.str.contains("ACReSAL) at Mokwa", case=False, na=False, regex=False)]
        fut = descs[descs.str.contains("Behind FUT", case=False, na=False, regex=False)]
        self.assertEqual(len(mokwa), 1)
        self.assertEqual(len(fut), 1)
        self.assertNotIn("FUT", mokwa.iloc[0])
        self.assertNotIn("Mokwa", fut.iloc[0])

        forestry = descs[descs.str.contains("Forestry Regeneration", case=False, na=False, regex=False)]
        self.assertEqual(len(forestry), 1)
        self.assertTrue(forestry.iloc[0].startswith("Suleja, Chanchaga Model Cities"))


if __name__ == "__main__":
    unittest.main()
