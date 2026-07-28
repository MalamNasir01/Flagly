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
        row = _row(description='Miscellaneous contingency pool item XYZ', amount=9_000_000_000)
        self.assertIsNone(flag_inflated_amount(row))

    def test_relative_benchmark_flags_extreme(self):
        # water_supply default benchmark 500M * 3 = 1.5B
        row = _row(description='Borehole drilling for rural water supply', amount=5_000_000_000, location='Niger')
        flag = flag_inflated_amount(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['flag_type'], 'INFLATED_AMOUNT')
        self.assertEqual(flag['evidence']['rule'], 'relative_benchmark')

    def test_stadium_uses_large_tier_not_flagged(self):
        # 9.8B stadium: large tier 20B * 3 = 60B → should NOT flag
        row = _row(
            description='Construction of national stadium complex',
            amount=9_800_000_000,
            location='Abuja',
            mda_name='FEDERAL MINISTRY OF YOUTH AND SPORTS',
        )
        self.assertIsNone(flag_inflated_amount(row))

    def test_small_sports_field_still_flags_extreme(self):
        # Local pitch without stadium keywords: default 2B * 3 = 6B; 20B should flag
        row = _row(
            description='Upgrade of community sporting facility pitch and stands',
            amount=20_000_000_000,
            location='Kano',
        )
        flag = flag_inflated_amount(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['evidence']['benchmark_tier'], 'default')


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
    def setUp(self):
        import engines.flags as flags
        flags.ACTIVE_MDA_MANDATES = flags.MDA_MANDATES

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

    def test_ferma_solar_domiciled_in_power(self):
        row = _row(
            description=(
                'Supply and Installation of Solar Street Lights domiciled in '
                'Federal Ministry of Power'
            ),
            amount=400_000_000,
            location=None,
            mda_name='FEDERAL ROAD MAINTENANCE AGENCY',
        )
        flag = flag_mandate_mismatch(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['flag_type'], 'MANDATE_MISMATCH')
        self.assertEqual(flag['evidence']['project_category'], 'renewable_energy')

    def test_power_ministry_solar_in_scope(self):
        row = _row(
            description='Supply & Installation of Solar Street Lights',
            amount=250_000_000,
            mda_name='Federal Ministry of Power',
        )
        self.assertIsNone(flag_mandate_mismatch(row))

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


class ScoringDedupeTests(unittest.TestCase):
    def test_stacked_location_flags_do_not_triple_score(self):
        from engines.scorer import score_item, collapse_flags_for_scoring

        stacked = {
            'description': 'Empowerment in selected locations',
            'amount': 50_000_000,
            'flags': [
                {'flag_type': 'INFLATED_AMOUNT', 'severity': 'HIGH'},
                {'flag_type': 'MISSING_LOCATION', 'severity': 'HIGH'},
                {'flag_type': 'VAGUE_LOCATION', 'severity': 'HIGH'},
            ],
        }
        collapsed = collapse_flags_for_scoring(stacked['flags'])
        self.assertEqual(len(collapsed), 2)  # price + location, not 3
        score_item(stacked)
        # One HIGH price + one HIGH location → severity 6*8 + 5 = 53 + base 10 + amount ~10
        self.assertLessEqual(stacked['risk_score'], 100)
        self.assertEqual(stacked['scoring_signal_count'], 2)
        self.assertEqual(stacked['raw_flag_count'], 3)

    def test_rank_and_shortlist(self):
        from engines.scorer import score_items
        items = [
            {'description': 'a', 'amount': 1e9, 'flags': [{'flag_type': 'GHOST_PROJECT', 'severity': 'HIGH'}]},
            {'description': 'b', 'amount': 1e6, 'flags': [{'flag_type': 'MISSING_LOCATION', 'severity': 'MEDIUM'}]},
            {'description': 'c', 'amount': 5e9, 'flags': [
                {'flag_type': 'INFLATED_AMOUNT', 'severity': 'HIGH'},
                {'flag_type': 'MANDATE_MISMATCH', 'severity': 'HIGH'},
            ]},
        ]
        scored = score_items(items, top_n=2)
        self.assertEqual(scored[0]['rank'], 1)
        self.assertTrue(scored[0]['on_shortlist'])
        self.assertTrue(scored[1]['on_shortlist'])
        self.assertFalse(scored[2]['on_shortlist'])
        self.assertGreaterEqual(scored[0]['risk_score'], scored[1]['risk_score'])


class FormatCSectionHeaderTests(unittest.TestCase):
    def test_section_header_matches_eight_space_layout(self):
        from engines.parser import FORMAT_C_SECTION_RE, FORMAT_C_SECTION_REJECT_RE
        line = '0234004001        FEDERAL ROAD MAINTENANCE AGENCY'
        m = FORMAT_C_SECTION_RE.match(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), '0234004001')
        self.assertEqual(m.group(2).strip(), 'FEDERAL ROAD MAINTENANCE AGENCY')
        self.assertIsNone(FORMAT_C_SECTION_REJECT_RE.search(m.group(2)))

    def test_section_header_rejects_project_like_lines(self):
        from engines.parser import FORMAT_C_SECTION_RE, FORMAT_C_SECTION_REJECT_RE
        # Would match the shape but should be rejected by reject regex when used together
        name = 'SOME PROJECT TITLE ONGOING 1,000,000.00'
        self.assertTrue(FORMAT_C_SECTION_REJECT_RE.search(name))


class DisambiguationTests(unittest.TestCase):
    def test_direct_labour_not_labour_programs(self):
        from engines.classifier import classify_with_match, reload_classifier_data
        reload_classifier_data()
        cat, kw = classify_with_match(
            'CONSTRUCTION/RENOVATION OF STAFF QUARTERS THROUGH DIRECT LABOUR'
        )
        self.assertNotEqual(cat, 'labour_programs')
        self.assertEqual(cat, 'housing')

    def test_labour_room_is_health(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project('Construction of labour room and maternity ward'),
            'health_facilities',
        )

    def test_short_keyword_not_substring_false_positive(self):
        from engines.classifier import classify_with_match, reload_classifier_data
        reload_classifier_data()
        # "hiv" must not match inside "archived"
        cat, kw = classify_with_match(
            'DIGITALISATION OF RECORDS NAFRC PARTICIPANTS/ARCHIVED AND STORAGE'
        )
        self.assertNotEqual(kw, 'hiv')
        self.assertNotEqual(cat, 'disease_control')

    def test_solar_lights_beats_installation_of_catchall(self):
        from engines.classifier import classify_with_match, reload_classifier_data
        reload_classifier_data()
        cat, kw = classify_with_match(
            "PROCUREMENT AND INSTALLATION OF SOLAR LIGHTS IN NEW CADETS' TRAINING SITES"
        )
        self.assertEqual(cat, 'renewable_energy')
        self.assertIn('solar', kw)

    def test_access_road_to_school_is_roads_not_primary(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project(
                'Construction of 3mx3m Four cells Reinforced Concrete Box Culvert '
                'on River Kontagora and 350m Access Road to link Ubanana Primary School - Rigasa'
            ),
            'roads',
        )

    def test_housing_units_along_road_is_housing(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project('Provision of 1,000 Housing Units Along Paiko-Suleja Road'),
            'housing',
        )

    def test_vehicle_purchase_not_tertiary(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project(
                'purchase of 1no. Toyota Camry. 1no.Toyota Hilux and 2no. Toyota '
                'corrolla saloon EDUCATION'
            ),
            'recurrent_admin',
        )

    def test_ambulance_boats_not_medical_equipment(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project(
                'Purchase of 3no Ambulance/ Survellance Boats and Life Jackets'
            ),
            'water_transport',
        )

    def test_dietary_sbcc_is_broadcasting(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project(
                'Promote good dietary habits and healthy lifestyles for all age groups '
                'through appropriate social marketing and communication'
            ),
            'broadcasting',
        )

    def test_campus_road_network_is_tertiary(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project('EDUCATION Completion of Road Network within the Campus & Ext. of ICT Rd'),
            'tertiary_education',
        )

    def test_federal_amasiru_road_not_urban(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project(
                'CONSTRUCTION/REHABILITATION OF AMASIRU-OKPOSI-UBURU-ISHIAGU ROAD'
            ),
            'roads',
        )

    def test_federal_dualization_is_roads(self):
        from engines.classifier import classify_project, reload_classifier_data
        reload_classifier_data()
        self.assertEqual(
            classify_project(
                'BENIN-AKURE DUALIZATION OF ROAD PHASE 1 LENGTH OF ROAD: 150.7KM'
            ),
            'roads',
        )


class AggregateInflationTests(unittest.TestCase):
    def test_nationwide_programme_exempt_from_inflation(self):
        row = _row(
            description='Youth empowerment programmes across the nation wide for payments',
            amount=14_000_000_000,
            location=None,
            mda_name='FEDERAL MINISTRY OF YOUTH DEVELOPMENT',
        )
        self.assertIsNone(flag_inflated_amount(row))
        self.assertTrue(row.get('_inflation_exempt'))

    def test_army_barracks_across_all_exempt(self):
        row = _row(
            description='CONSTRUCTION OF RESIDENTIAL ACCOMMODATION FOR OFFICERS AND SOLDIERS ACROSS NIGERIAN ARMY BARRACKS',
            amount=44_000_000_000,
            mda_name='NIGERIAN ARMY',
        )
        self.assertIsNone(flag_inflated_amount(row))


class GhostYearRangeTests(unittest.TestCase):
    def test_active_plan_range_not_ghost(self):
        from engines.flags import flag_ghost_project
        row = _row(
            description='Mid-term review of National Medical Laboratory Strategic Plan (2023–2027)',
            amount=50_000_000,
            mda_name='FEDERAL MINISTRY OF HEALTH',
        )
        self.assertIsNone(flag_ghost_project(row, [row['description']], '2026'))

    def test_ascii_hyphen_range_not_ghost(self):
        from engines.flags import flag_ghost_project
        row = _row(
            description='Mid-term review of National Medical Laboratory Strategic Plan (2023-2027)',
            amount=50_000_000,
        )
        self.assertIsNone(flag_ghost_project(row, [row['description']], '2026'))

    def test_genuinely_past_year_still_flags(self):
        from engines.flags import flag_ghost_project
        row = _row(
            description='Completion of abandoned 2019 BUDGET classroom block project',
            amount=80_000_000,
        )
        flag = flag_ghost_project(row, [row['description']], '2026')
        self.assertIsNotNone(flag)
        self.assertEqual(flag['flag_type'], 'GHOST_PROJECT')


class MandateSeveritySplitTests(unittest.TestCase):
    def test_excluded_is_high(self):
        # FERMA excluded includes sports_facilities
        row = _row(
            description='Construction of sports complex pavilion',
            amount=200_000_000,
            mda_name='Federal Road Maintenance Agency',
        )
        flag = flag_mandate_mismatch(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['severity'], 'HIGH')
        self.assertEqual(flag['evidence']['mismatch_kind'], 'excluded')

    def test_not_in_scope_is_medium(self):
        row = _row(
            description='Supply & Installation of Solar Street Lights',
            amount=250_000_000,
            mda_name='Federal Road Maintenance Agency',
        )
        flag = flag_mandate_mismatch(row)
        self.assertIsNotNone(flag)
        self.assertEqual(flag['severity'], 'MEDIUM')
        self.assertEqual(flag['evidence']['mismatch_kind'], 'not_in_scope')


if __name__ == '__main__':
    unittest.main()
