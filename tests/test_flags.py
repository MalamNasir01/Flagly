"""Unit tests for Flagly flag engines (deterministic fixtures)."""

import math
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.classifier import classify_project, classify_with_match
from engines.flags import (
    flag_inflated_amount,
    flag_missing_location,
    flag_vague_location,
    flag_mandate_mismatch,
    flag_duplicates,
    run_all_flags,
    get_last_run_stats,
)
import pandas as pd


def _row(**kwargs):
    base = {
        'row_id': 1,
        'description': '',
        'amount': None,
        'location': None,
        'ministry': None,
        'mda_name': None,
        'project_code': None,
        'project_name': None,
        'project_status': None,
        'is_mda_level': False,
        '_flags': [],
        '_exclude': False,
    }
    base.update(kwargs)
    if base.get('project_name') is None:
        base['project_name'] = base.get('description')
    return base


class ClassifierTests(unittest.TestCase):
    def test_solar_street_lights_is_renewable(self):
        cat, kw = classify_with_match('Supply & Installation of Solar Street Lights')
        self.assertEqual(cat, 'renewable_energy')
        self.assertIn('solar', kw)

    def test_arms_is_military(self):
        self.assertEqual(classify_project('Procurement of Arms (all types)'), 'military_equipment')

    def test_road_with_lga(self):
        self.assertEqual(
            classify_project('Construction of dual carriageway in Kaduna LGA'),
            'roads',
        )

    def test_tie_break_longest_keyword(self):
        # "solar street light" is longer/more specific than generic "street light"
        cat, kw = classify_with_match('Solar street light installation project')
        self.assertEqual(cat, 'renewable_energy')
        self.assertEqual(kw, 'solar street light')


class InflatedAmountTests(unittest.TestCase):
    def test_14bn_road_with_lga_not_inflated(self):
        row = _row(
            description='Construction of dual carriageway Kano Maiduguri Road Phase II',
            amount=1_400_000_000,
            location='Kano',
            mda_name='FEDERAL MINISTRY OF WORKS',
        )
        self.assertIsNone(flag_inflated_amount(row))

    def test_unknown_category_skips_inflated(self):
        row = _row(description='Miscellaneous administrative contingency pool', amount=9_000_000_000)
        self.assertIsNone(flag_inflated_amount(row))

    def test_relative_benchmark_flags_extreme(self):
        # water_supply benchmark 60M * 3 = 180M
        row = _row(description='Borehole drilling for rural water supply', amount=900_000_000, location='Niger')
        flag = flag_inflated_amount(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['flag_type'], 'INFLATED_AMOUNT')
        self.assertEqual(flag['evidence']['rule'], 'relative_benchmark')


class MissingLocationTests(unittest.TestCase):
    def test_arms_not_missing_location(self):
        row = _row(
            description='Procurement of Arms (all types)',
            amount=16_000_000_000,
            location=None,
            mda_name='NIGERIAN ARMY',
        )
        self.assertIsNone(flag_missing_location(row))

    def test_14bn_road_with_lga_not_missing(self):
        row = _row(
            description='Construction of dual carriageway in Ikeja LGA',
            amount=1_400_000_000,
            location='Lagos',
            mda_name='FEDERAL MINISTRY OF WORKS',
        )
        self.assertIsNone(flag_missing_location(row))


class VagueLocationTests(unittest.TestCase):
    def test_multiple_lots_with_kaduna_not_flagged(self):
        row = _row(
            description='Rehabilitation of feeder roads in multiple lots across Kaduna',
            amount=800_000_000,
            location='Kaduna',
            mda_name='FEDERAL MINISTRY OF WORKS',
        )
        self.assertIsNone(flag_vague_location(row))

    def test_vague_phrase_no_location_fires(self):
        row = _row(
            description='Empowerment programme in selected locations',
            amount=50_000_000,
            location=None,
            mda_name='STATE HOUSE',
        )
        flag = flag_vague_location(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['flag_type'], 'VAGUE_LOCATION')
        self.assertEqual(flag['evidence']['phrase'], 'selected locations')
        # phrase severity high in seed file
        self.assertEqual(flag['severity'], 'HIGH')

    def test_medium_elevates_at_5m(self):
        row = _row(
            description='Capacity building nationwide for civil servants',
            amount=6_000_000,
            location=None,
            mda_name='STATE HOUSE',
        )
        flag = flag_vague_location(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['evidence']['phrase'], 'nationwide')
        self.assertEqual(flag['severity'], 'HIGH')  # elevated from medium


class MandateMismatchTests(unittest.TestCase):
    def test_ferma_solar_street_lights(self):
        row = _row(
            description='Supply & Installation of Solar Street Lights',
            amount=250_000_000,
            location='Abuja',
            mda_name='Federal Road Maintenance Agency',
        )
        flag = flag_mandate_mismatch(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['flag_type'], 'MANDATE_MISMATCH')
        self.assertEqual(flag['evidence']['project_category'], 'renewable_energy')
        # renewable_energy is not in FERMA scope or excluded → MEDIUM
        self.assertEqual(flag['severity'], 'MEDIUM')

    def test_unclassified_skips_mandate(self):
        row = _row(
            description='Miscellaneous contingency for unforeseen needs',
            amount=100_000_000,
            mda_name='Federal Road Maintenance Agency',
        )
        self.assertIsNone(flag_mandate_mismatch(row))


class DuplicateTests(unittest.TestCase):
    def test_same_text_different_mda_not_duplicate(self):
        a = _row(row_id=1, description='Construction of barracks accommodation block for personnel',
                 amount=500_000_000, mda_name='NIGERIAN ARMY 1 DIV')
        b = _row(row_id=2, description='Construction of barracks accommodation block for personnel',
                 amount=500_000_000, mda_name='NIGERIAN ARMY 2 DIV')
        rows = flag_duplicates([a, b])
        self.assertFalse(any(f.get('flag_type') == 'DUPLICATE_CLUSTER' for f in a.get('_flags', [])))
        self.assertFalse(any(f.get('flag_type') == 'DUPLICATE_CLUSTER' for f in b.get('_flags', [])))

    def test_same_mda_same_amount_flags(self):
        a = _row(row_id=1, description='Construction of barracks accommodation block for personnel HQ',
                 amount=500_000_000, mda_name='NIGERIAN ARMY')
        b = _row(row_id=2, description='Construction of barracks accommodation block for personnel HQ',
                 amount=500_000_000, mda_name='NIGERIAN ARMY')
        flag_duplicates([a, b])
        self.assertTrue(any(f.get('flag_type') == 'DUPLICATE_CLUSTER' for f in a.get('_flags', [])))


class UnclassifiedStatsTests(unittest.TestCase):
    def test_unclassified_count_logged(self):
        df = pd.DataFrame([
            {'row_id': 1, 'description': 'Miscellaneous contingency pool item', 'amount': 1e6,
             'location': None, 'ministry': 'STATE HOUSE', 'mda_name': 'STATE HOUSE',
             'project_code': 'X1', 'project_name': 'Miscellaneous contingency pool item',
             'project_status': None, 'is_mda_level': False},
            {'row_id': 2, 'description': 'Construction of dual carriageway in Lagos', 'amount': 1.4e9,
             'location': 'Lagos', 'ministry': 'FEDERAL MINISTRY OF WORKS',
             'mda_name': 'FEDERAL MINISTRY OF WORKS', 'project_code': 'X2',
             'project_name': 'Construction of dual carriageway in Lagos',
             'project_status': 'ONGOING', 'is_mda_level': False},
        ])
        run_all_flags(df, budget_year='2026')
        stats = get_last_run_stats()
        self.assertEqual(stats['unclassified_count'], 1)
        self.assertEqual(stats['total_items'], 2)


if __name__ == '__main__':
    unittest.main()
