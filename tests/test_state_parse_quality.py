"""Parse-quality guards for Niger row-boundary merges."""

from __future__ import annotations

import os
import unittest

from engines.state_formats.parser import (
    assess_parse_quality,
    description_merge_issues,
    parse_state_text,
)


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class DescriptionMergeIssueTests(unittest.TestCase):
    def test_flags_two_project_verb_phrases(self):
        merged = (
            "Development of Software for MDAs Construction of 10km Rural Roads "
            "and Markets at Katcha"
        )
        self.assertIn("multiple_project_verbs", description_merge_issues(merged))

        merged2 = (
            "Extension of pipeline to new settlements (SURWASH) "
            "Compensation of land acquired from the indigenes of Lenfa"
        )
        self.assertIn("multiple_project_verbs", description_merge_issues(merged2))

        merged3 = (
            "Erosion Control and Agro Climatic Activities (ACReSAL) at Mokwa "
            "Erosion Control and Management Work Behind FUT, Minna"
        )
        self.assertIn("multiple_project_verbs", description_merge_issues(merged3))

    def test_flags_mid_phrase_start(self):
        self.assertIn(
            "mid_phrase_start",
            description_merge_issues(
                "and Sanitation, Forestry Regeneration (UN-HABITA)"
            ),
        )

    def test_clean_single_projects_not_flagged(self):
        clean = [
            "Development of Software for MDAs",
            "Construction of 10km Rural Roads and Markets)World Bank at Katcha",
            "Extension of pipeline to new settlements and rehabilitation of existing ones within chanchaga (SURWASH)",
            "Support improvement to Basic & secondary education opportunities for the girl child. (AGILE) (World Bank Loan)",
            "Construction of Accommodation for Development partners",
        ]
        for desc in clean:
            self.assertEqual(description_merge_issues(desc), [], desc)


class NigerDemergeFixtureTests(unittest.TestCase):
    def test_adjacent_projects_not_merged(self):
        path = os.path.join(FIXTURES, "niger_capital_demerge.txt")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        df, meta = parse_state_text(text, "state_niger")
        descs = df["description"].astype(str).tolist()
        self.assertTrue(any(d == "Development of Software for MDAs" for d in descs))
        self.assertTrue(
            any("10km Rural Roads" in d and "Software" not in d for d in descs)
        )
        self.assertTrue(any("SURWASH" in d and "Lenfa" not in d for d in descs))
        self.assertTrue(any("Lenfa" in d and "SURWASH" not in d for d in descs))
        self.assertTrue(any("Mokwa" in d and "FUT" not in d for d in descs))
        self.assertTrue(any("Behind FUT" in d and "Mokwa" not in d for d in descs))
        self.assertTrue(
            any(
                d.startswith("Suleja, Chanchaga Model Cities") and "and Sanitation" in d
                for d in descs
            )
        )
        self.assertFalse(any(d.startswith("and Sanitation") for d in descs))

        pq = meta.get("parse_quality") or assess_parse_quality(df.to_dict("records"))
        self.assertEqual(pq.get("suspect_rows"), 0)


if __name__ == "__main__":
    unittest.main()
