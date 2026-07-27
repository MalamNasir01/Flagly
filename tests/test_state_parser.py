"""Tests for budget format detection and Niger State capital parser (step 1)."""

from __future__ import annotations

import os
import unittest

from engines.format_detect import (
    FORMAT_FEDERAL,
    FORMAT_STATE_NIGER,
    FORMAT_UNKNOWN,
    UnsupportedBudgetFormat,
    _detect_federal_a_signals,
    _detect_state_niger,
    detect_budget_format,
)
from engines.state_formats import parse_state_text


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class FormatDetectTests(unittest.TestCase):
    def test_niger_signals_from_fixture_text(self):
        path = os.path.join(FIXTURES, "niger_capital_sample.txt")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(_detect_state_niger(text))

    def test_federal_a_signals(self):
        text = (
            "Federal Government of Nigeria\n"
            "APPROPRIATION BILL (DETAILS)\n"
            "PERSONNEL COST  OVERHEAD  CAPITAL\n"
        )
        self.assertTrue(_detect_federal_a_signals(text))
        self.assertFalse(_detect_state_niger(text))

    def test_unknown_has_no_signals(self):
        self.assertFalse(_detect_state_niger("Random corporate report 2026"))
        self.assertFalse(_detect_federal_a_signals("Random corporate report 2026"))


class NigerParserFixtureTests(unittest.TestCase):
    def test_parses_sample_capital_rows(self):
        path = os.path.join(FIXTURES, "niger_capital_sample.txt")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        df, meta = parse_state_text(text, "state_niger")
        self.assertTrue(meta.get("section_found"))
        self.assertIn("approved_2026", meta.get("columns_detected") or [])
        self.assertEqual(meta.get("amount_column_used"), "approved_2026")
        self.assertGreater(len(df), 5)

        # Full (non-truncated) project name
        first = df.iloc[0]
        self.assertIn("Accommodation for Development partners", str(first["description"]))
        self.assertTrue(str(first["description"]).startswith("Construction"))
        self.assertNotIn("OFFICE BUILDINGS", str(first["description"]))
        self.assertNotIn("Location Code", str(first["description"]))
        self.assertEqual(first["mda_code"], "011100100100")
        self.assertIn("Executive Governor", str(first["mda_name"]))
        self.assertEqual(first["location"], "CHANCHAGA")
        self.assertEqual(float(first["amount"]), 1_000_000_000.0)

        # No economic-fragment fake rows
        descs = df["description"].astype(str).tolist()
        self.assertFalse(any(d.strip() == "OFFICE BUILDINGS" for d in descs))
        self.assertFalse(any("ORGANS" == d.strip() for d in descs))

        mda_pct = df["mda_name"].notna().mean()
        self.assertGreater(mda_pct, 0.9)

    def test_multiline_description_not_midword_cut(self):
        path = os.path.join(FIXTURES, "niger_capital_wrap.txt")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        df, meta = parse_state_text(text, "state_niger")
        self.assertGreater(len(df), 2)
        hits = df[df["description"].astype(str).str.contains("improved seeds", case=False, na=False)]
        self.assertGreater(len(hits), 0)
        desc = str(hits.iloc[0]["description"])
        self.assertIn("improved seeds", desc)
        self.assertIn("Rice", desc)
        self.assertNotEqual(desc[-1], "-")
        self.assertEqual(float(hits.iloc[0]["amount"]), 90_000_000.0)

    def test_guest_houses_wrap_joins(self):
        path = os.path.join(FIXTURES, "niger_capital_sample.txt")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        df, _ = parse_state_text(text, "state_niger")
        hits = df[
            df["description"].astype(str).str.contains(
                "Guest Houses Behind Government House", case=False, na=False
            )
        ]
        self.assertGreater(len(hits), 0)
        desc = str(hits.iloc[0]["description"])
        self.assertIn("28nos", desc)
        self.assertIn("Guest Houses Behind Government House", desc)


if __name__ == "__main__":
    unittest.main()
