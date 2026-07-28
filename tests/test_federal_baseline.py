"""Frozen federal scan baseline — gate for future classifier/mandate changes.

Golden numbers live in samples/federal_baseline.json (regenerate with
scripts/refresh_federal_baseline.py or by re-running the scan helper).

The full PDF comparison runs only when the Appropriation Bill PDF is present
locally; road guardrails always run.
"""

from __future__ import annotations

import json
import os
import unittest

BASELINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "samples",
    "federal_baseline.json",
)

# Candidate paths for the 2026 Appropriation Bill Details PDF
_PDF_CANDIDATES = [
    os.environ.get("FLAGLY_FEDERAL_PDF", ""),
    os.path.expanduser("~/Desktop/2026 Appropriation Bill Details.pdf"),
    "/Users/spade_nas1/Desktop/2026 Appropriation Bill Details.pdf",
]

# Tight tolerances — any larger drift is a regression until baseline is intentionally refreshed
_TOLERANCE = {
    "flagged_items": 40,          # ~1.2% of ~3240
    "mandate_high": 15,
    "mandate_medium": 40,
    "INFLATED_AMOUNT": 15,
    "MISSING_LOCATION": 30,
    "MANDATE_MISMATCH": 40,
    "VAGUE_LOCATION": 20,
    "DUPLICATE_CLUSTER": 15,
    "BUDGET_SPLITTING": 15,
    "CONTEXT_MISMATCH": 15,
    "GHOST_PROJECT": 5,
}


def _load_baseline() -> dict:
    with open(BASELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _find_pdf() -> str | None:
    for path in _PDF_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


class FederalBaselineFileTests(unittest.TestCase):
    def test_baseline_file_exists_and_has_locked_totals(self):
        self.assertTrue(os.path.isfile(BASELINE_PATH), "missing samples/federal_baseline.json")
        base = _load_baseline()
        self.assertEqual(base["total_items"], 17873)
        self.assertIn("flagged_items", base)
        self.assertIn("mandate_high", base)
        self.assertIn("mandate_medium", base)
        self.assertIn("flag_counts", base)
        self.assertEqual(len(base.get("top10") or []), 10)


@unittest.skipUnless(_find_pdf(), "Federal Appropriation Bill PDF not available")
class FederalBaselineRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from engines.classifier import reload_classifier_data
        import engines.flags as flags

        reload_classifier_data()
        flags.MDA_MANDATES = flags._normalize_mda_mandates(
            flags._load_json("mda_mandates.json", {})
        )
        flags.ACTIVE_MDA_MANDATES = flags.MDA_MANDATES

        from engines.parser import parse_file
        from engines.flags import run_all_flags
        from engines.scorer import score_items
        from collections import Counter

        pdf_path = _find_pdf()
        df = parse_file(open(pdf_path, "rb").read(), "2026.pdf")
        scored = score_items(run_all_flags(df, budget_year="2026", jurisdiction="federal"))
        types = Counter()
        mandate = Counter()
        for r in scored:
            for f in r.get("flags") or []:
                types[f["flag_type"]] += 1
                if f["flag_type"] == "MANDATE_MISMATCH":
                    mandate[(f.get("severity") or "").upper()] += 1

        cls.stats = {
            "total_items": len(df),
            "flagged_items": len(scored),
            "mandate_high": mandate.get("HIGH", 0),
            "mandate_medium": mandate.get("MEDIUM", 0),
            "flag_counts": dict(types),
        }
        cls.baseline = _load_baseline()

    def test_total_items_locked(self):
        self.assertEqual(self.stats["total_items"], self.baseline["total_items"])

    def test_flagged_within_tolerance(self):
        delta = abs(self.stats["flagged_items"] - self.baseline["flagged_items"])
        self.assertLessEqual(
            delta,
            _TOLERANCE["flagged_items"],
            f"flagged {self.stats['flagged_items']} vs baseline {self.baseline['flagged_items']}",
        )

    def test_mandate_high_medium_within_tolerance(self):
        for key in ("mandate_high", "mandate_medium"):
            delta = abs(self.stats[key] - self.baseline[key])
            self.assertLessEqual(
                delta,
                _TOLERANCE[key],
                f"{key} {self.stats[key]} vs baseline {self.baseline[key]}",
            )

    def test_per_flag_counts_within_tolerance(self):
        base_counts = self.baseline["flag_counts"]
        for ft, tol in _TOLERANCE.items():
            if ft in ("flagged_items", "mandate_high", "mandate_medium"):
                continue
            got = self.stats["flag_counts"].get(ft, 0)
            want = base_counts.get(ft, 0)
            self.assertLessEqual(
                abs(got - want),
                tol,
                f"{ft} {got} vs baseline {want} (tol {tol})",
            )


if __name__ == "__main__":
    unittest.main()
