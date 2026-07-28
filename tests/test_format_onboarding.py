"""Kaduna onboarding + format detection refusal tests."""

from __future__ import annotations

import os
import unittest

from engines.format_detect import (
    FORMAT_FEDERAL,
    FORMAT_KNOWN_UNSUPPORTED,
    FORMAT_SCANNED,
    FORMAT_STATE_NIGER,
    FORMAT_UNKNOWN,
    classify_budget_file,
    detect_budget_format,
    supported_formats,
)
from engines.state_formats.registry import list_profiles, load_profile


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
KADUNA_PDF = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "samples",
    "incoming",
    "kaduna_fy2025_budget.pdf",
)


class RegistryTests(unittest.TestCase):
    def test_profiles_include_niger_and_kaduna(self):
        ids = list_profiles()
        self.assertIn("state_niger", ids)
        self.assertIn("state_kaduna", ids)
        self.assertIn("state_niger", supported_formats())
        self.assertIn("state_kaduna", supported_formats())


class FormatDetectRefusalTests(unittest.TestCase):
    def test_unknown_message_lists_supported(self):
        result = classify_budget_file(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
        # Tiny PDF may be scanned or unknown
        self.assertIn(result.format_id, {FORMAT_UNKNOWN, FORMAT_SCANNED, FORMAT_KNOWN_UNSUPPORTED})
        self.assertFalse(result.supported)
        self.assertTrue(result.message)
        self.assertIn("Federal", result.message)

    def test_known_unsupported_state_named(self):
        text = (
            "Kano State Government 2026 Approved Budget\n"
            "Capital Expenditure by Project\n"
            "Administrative Code and Description\n"
        )
        # Feed as fake peek by wrapping minimal pdf is hard; unit-test helper path:
        from engines.format_detect import _guess_nigerian_state_name, _detect_state_from_profile

        self.assertEqual(_guess_nigerian_state_name(text), "Kano")
        # Ensure Kano text does not match Niger/Kaduna profiles
        self.assertFalse(_detect_state_from_profile(text, load_profile("state_niger")))
        self.assertFalse(_detect_state_from_profile(text, load_profile("state_kaduna")))


@unittest.skipUnless(os.path.isfile(KADUNA_PDF), "Kaduna FY2025 PDF not in samples/incoming")
class KadunaOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from engines.state_formats import parse_state_pdf
        from engines.classifier import reload_classifier_data
        import engines.flags as flags
        from engines.flags import run_all_flags
        from engines.scorer import score_items
        from collections import Counter

        reload_classifier_data()
        flags.STATE_MANDATE_BUNDLES = flags._load_state_mandates_bundle()

        contents = open(KADUNA_PDF, "rb").read()
        cls.detection = classify_budget_file(contents)
        cls.df, cls.meta = parse_state_pdf(contents, "state_kaduna")
        scored = score_items(
            run_all_flags(cls.df, budget_year="2025", jurisdiction="state_kaduna")
        )
        mandate = Counter()
        for r in scored:
            for f in r.get("flags") or []:
                if f["flag_type"] == "MANDATE_MISMATCH":
                    mandate[(f.get("severity") or "").upper()] += 1
        cls.mandate = mandate
        cls.scored = scored

    def test_detects_as_kaduna(self):
        self.assertEqual(self.detection.format_id, "state_kaduna")
        self.assertTrue(self.detection.supported)

    def test_parses_items(self):
        self.assertGreater(len(self.df), 50)
        self.assertTrue(self.meta.get("section_found"))

    def test_top_descriptions_clean(self):
        from engines.state_formats.parser import description_merge_issues

        top = self.df.sort_values("amount", ascending=False, na_position="last").head(5)
        for _, r in top.iterrows():
            # Allow at most mid-phrase on truncated wraps; dual-verb should be rare in top 5
            issues = description_merge_issues(str(r.description))
            self.assertNotIn("multiple_project_verbs", issues, r.description)

    def test_mandate_flags_medium_capped(self):
        import engines.flags as flags

        self.assertEqual(self.mandate.get("HIGH", 0), 0)
        bundle = flags.STATE_MANDATE_BUNDLES.get("kaduna_state") or {}
        self.assertFalse(bundle.get("reviewed", True))


if __name__ == "__main__":
    unittest.main()
