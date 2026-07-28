"""Step 2 — state flag wiring tests (additive; federal tests remain separate)."""

from __future__ import annotations

import json
import os
import unittest

from engines.flags import (
    ACTIVE_MDA_MANDATES,
    MDA_MANDATES,
    STATE_MDA_MANDATES,
    _aggregate_signal,
    flag_blank_approved_amount,
    flag_inflated_amount,
    flag_mandate_mismatch,
    flag_overhead_dominance,
    run_all_flags,
)
from engines.classifier import get_inflated_benchmark_meta, reload_classifier_data


def _row(**kwargs):
    base = {
        "description": "Construction of classroom block",
        "amount": 100_000_000,
        "location": "BIDA",
        "mda_name": "Ministry of Works",
        "mda_code": "023400100100",
        "ministry": "Ministry of Works",
        "is_mda_level": False,
        "_flags": [],
        "_jurisdiction": "state_niger",
        "_format_b": True,
    }
    base.update(kwargs)
    return base


class StateMandateTests(unittest.TestCase):
    def test_state_mandates_loaded(self):
        self.assertGreater(len(STATE_MDA_MANDATES), 20)
        self.assertIn("MINISTRY OF WORKS", STATE_MDA_MANDATES)

    def test_run_all_flags_switches_mandate_table(self):
        import pandas as pd
        import engines.flags as flags_mod

        df = pd.DataFrame([
            {
                "description": "Construction of primary health centre in Lavun",
                "amount": 80_000_000,
                "location": "LAVUN",
                "mda_name": "Ministry of Works",
                "mda_code": "023400100100",
                "ministry": "Ministry of Works",
                "is_mda_level": False,
                "actuals_2024": None,
                "budget_2025": None,
                "performance_2025": None,
                "budget_2026": 80_000_000,
                "economic_code": "23020101",
                "location_code": "12611200",
            }
        ])
        run_all_flags(df, budget_year="2026", jurisdiction="state_niger")
        self.assertIs(flags_mod.ACTIVE_MDA_MANDATES, STATE_MDA_MANDATES)
        run_all_flags(df, budget_year="2026", jurisdiction="federal")
        self.assertIs(flags_mod.ACTIVE_MDA_MANDATES, MDA_MANDATES)

    def test_works_health_facility_is_high_mandate_on_state(self):
        import engines.flags as flags

        flags.ACTIVE_MDA_MANDATES = STATE_MDA_MANDATES
        row = _row(
            description="Construction of primary health centre and maternity ward",
            amount=200_000_000,
            _project_category="health_facilities",
            _category_keyword="health centre",
        )
        flag = flag_mandate_mismatch(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag["severity"], "HIGH")
        self.assertEqual(flag["evidence"]["mismatch_kind"], "excluded")


class StateInflationTests(unittest.TestCase):
    def test_state_benchmarks_lower_than_federal(self):
        reload_classifier_data()
        fed = get_inflated_benchmark_meta("roads", "road construction", jurisdiction="federal")
        st = get_inflated_benchmark_meta("roads", "road construction", jurisdiction="state_niger")
        self.assertIsNotNone(fed["benchmark"])
        self.assertIsNotNone(st["benchmark"])
        self.assertLess(st["benchmark"], fed["benchmark"])

    def test_state_wide_location_exempts_inflation(self):
        self.assertEqual(_aggregate_signal("Youth empowerment programme", "State Wide"), "state wide")
        row = _row(
            description="Youth empowerment programme for farmers",
            amount=5_000_000_000,
            location="State Wide",
            _project_category="social_welfare",
            _category_keyword="empowerment",
            _jurisdiction="state_niger",
        )
        self.assertIsNone(flag_inflated_amount(row))
        self.assertTrue(row.get("_inflation_exempt"))


class BlankApprovedAmountTests(unittest.TestCase):
    def test_blank_with_prior_is_low_informational(self):
        row = _row(
            amount=None,
            budget_2026=None,
            actuals_2024=50_000_000,
            budget_2025=80_000_000,
            performance_2025=0,
            _jurisdiction="state_niger",
            _format_b=True,
        )
        flag = flag_blank_approved_amount(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag["flag_type"], "BLANK_APPROVED_AMOUNT")
        self.assertEqual(flag["severity"], "LOW")
        self.assertTrue(flag["evidence"].get("informational"))

    def test_blank_large_prior_ongoing_is_medium(self):
        row = _row(
            amount=None,
            budget_2026=None,
            actuals_2024=120_000_000,
            budget_2025=80_000_000,
            project_status="ONGOING",
            description="Continuation of classroom block phase 2",
            _jurisdiction="state_niger",
            _format_b=True,
        )
        flag = flag_blank_approved_amount(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag["severity"], "MEDIUM")
        self.assertTrue(flag["evidence"].get("anomaly"))

    def test_blank_without_prior_is_low(self):
        row = _row(amount=None, budget_2026=None, _format_b=True)
        flag = flag_blank_approved_amount(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag["severity"], "LOW")

    def test_federal_rows_not_flagged(self):
        row = _row(amount=None, _jurisdiction="federal", _format_b=False)
        self.assertIsNone(flag_blank_approved_amount(row))

    def test_overhead_disabled_path_for_state_row(self):
        # Overhead still returns None without is_mda_level / overhead columns
        row = _row(is_mda_level=False, overhead_amount=None, capital_amount=None)
        self.assertIsNone(flag_overhead_dominance(row))

    def test_informational_blank_only_excluded_from_results(self):
        import pandas as pd

        df = pd.DataFrame([
            {
                "description": "Completed borehole project",
                "amount": None,
                "location": "BIDA",
                "mda_name": "Ministry of Water Resources",
                "mda_code": "025200100100",
                "ministry": "Ministry of Water Resources",
                "is_mda_level": False,
                "actuals_2024": 40_000_000,
                "budget_2025": 40_000_000,
                "performance_2025": 10_000_000,
                "budget_2026": None,
                "economic_code": "23020105",
                "location_code": "12611200",
                "project_status": None,
            }
        ])
        results = run_all_flags(df, budget_year="2026", jurisdiction="state_niger")
        self.assertEqual(results, [])
        from engines.flags import get_last_run_stats
        blank = get_last_run_stats().get("blank_approved_amount") or {}
        self.assertEqual(blank.get("low"), 1)
        self.assertEqual(blank.get("informational_only_excluded"), 1)


class StateMandatesFileTests(unittest.TestCase):
    def test_file_only_contains_observed_mdas(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "data",
            "mda_mandates_states.json",
        )
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["_meta"]["jurisdiction"], "niger_state")
        self.assertGreaterEqual(len(doc["mdas"]), 20)
        for m in doc["mdas"]:
            self.assertTrue(m.get("name"))
            self.assertTrue(m.get("mda_code"))
            self.assertIsInstance(m.get("scope"), list)
            self.assertIsInstance(m.get("excluded"), list)


if __name__ == "__main__":
    unittest.main()
