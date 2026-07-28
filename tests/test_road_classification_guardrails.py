"""Guardrails: federal road works vs state access/campus disambiguation must not trade off."""

from __future__ import annotations

import unittest

from engines.classifier import classify_project, reload_classifier_data


class RoadClassificationGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reload_classifier_data()

    def test_federal_construction_rehabilitation_road_is_roads(self):
        self.assertEqual(
            classify_project(
                "CONSTRUCTION/REHABILITATION OF AMASIRU-OKPOSI-UBURU-ISHIAGU ROAD"
            ),
            "roads",
        )

    def test_federal_dualization_length_of_road_is_roads(self):
        self.assertEqual(
            classify_project(
                "BENIN-AKURE DUALIZATION OF ROAD PHASE 1 LENGTH OF ROAD: 150.7KM"
            ),
            "roads",
        )

    def test_state_access_road_to_school_is_roads_not_primary(self):
        self.assertEqual(
            classify_project(
                "Construction of 3mx3m Four cells Reinforced Concrete Box Culvert "
                "on River Kontagora and 350m Access Road to link Ubanana Primary School - Rigasa"
            ),
            "roads",
        )

    def test_campus_road_network_is_tertiary_not_roads(self):
        self.assertEqual(
            classify_project(
                "EDUCATION Completion of Road Network within the Campus & Ext. of ICT Rd"
            ),
            "tertiary_education",
        )


if __name__ == "__main__":
    unittest.main()
