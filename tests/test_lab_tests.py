"""Test catalog and result entry."""

import unittest

from labapp.lab_tests import (ResultValidationError, get_ordered_tests,
                              list_active_tests, save_results)
from labapp.registration import create_case, get_case
from tests.test_step1 import DbTestCase


class CatalogTests(DbTestCase):
    def test_seeded_tests(self):
        names = [t["name"] for t in list_active_tests(self.conn)]
        self.assertEqual(names, ["HBsAg Screening", "Anti HCV Screening", "Anti HIV",
                                 "H. Pylori Ab", "Stool for H. Pylori Ag",
                                 "Stool for Occult Blood", "Stool RE", "Urine RE"])

    def test_adding_a_test_is_data_only(self):
        self.conn.execute("INSERT INTO tests (code, name, section, methods, display_order) "
                          "VALUES ('VDRL', 'VDRL', 'Serology', 'ICT', 35)")
        self.assertIn("VDRL", [t["name"] for t in list_active_tests(self.conn)])


class ResultTests(DbTestCase):
    def setUp(self):
        super().setUp()
        p = self.patient()
        self.case = create_case(self.conn, p["id"], consultant=None,
                                test_ids=[self.test_ids["HBSAG"], self.test_ids["STOOL_OB"]])
        self.ordered = {ot.case_test["code"]: ot for ot in get_ordered_tests(self.conn, self.case["id"])}

    def ids(self, code):
        ot = self.ordered[code]
        return ot.case_test["id"], ot.parameters[0].row["id"]

    def result(self, code):
        ct_id, p_id = self.ids(code)
        return self.conn.execute("SELECT * FROM results WHERE case_test_id = ? AND parameter_id = ?",
                                 (ct_id, p_id)).fetchone()

    def save(self, code, value, flag="auto", method=None):
        ct_id, p_id = self.ids(code)
        save_results(self.conn, self.case["id"], {ct_id: method} if method else {},
                     {ct_id: {p_id: (value, flag)}})

    def test_ordered_tests_have_options(self):
        hbsag = self.ordered["HBSAG"]
        self.assertEqual(hbsag.methods, ["ICT", "ELISA"])
        self.assertEqual([o["value"] for o in hbsag.parameters[0].options],
                         ["Negative", "Positive"])
        self.assertEqual(hbsag.parameters[0].row["normal_text"], "Negative")

    def test_reactive_is_abnormal_automatically(self):
        self.save("HBSAG", "Positive")
        self.assertEqual(self.result("HBSAG")["is_abnormal"], 1)
        self.save("HBSAG", "Negative")
        self.assertEqual(self.result("HBSAG")["is_abnormal"], 0)

    def test_manual_override(self):
        self.save("HBSAG", "Negative", flag="abnormal")
        r = self.result("HBSAG")
        self.assertEqual((r["is_abnormal"], r["abnormal_manual"]), (1, 1))

    def test_invalid_value_rejected(self):
        with self.assertRaises(ResultValidationError):
            self.save("HBSAG", "Maybe")
        self.assertIsNone(self.result("HBSAG"))

    def test_empty_value_clears_result(self):
        self.save("STOOL_OB", "Positive")
        self.save("STOOL_OB", "")
        self.assertIsNone(self.result("STOOL_OB"))

    def test_method_change(self):
        self.save("HBSAG", "Positive", method="ELISA")
        method = self.conn.execute("SELECT method FROM case_tests WHERE id = ?",
                                   (self.ids("HBSAG")[0],)).fetchone()[0]
        self.assertEqual(method, "ELISA")
        with self.assertRaises(ValueError):
            self.save("STOOL_OB", "Negative", method="ELISA")  # stool OB only allows ICT

    def test_status_moves_to_in_progress(self):
        self.assertEqual(get_case(self.conn, self.case["id"])["status"], "registered")
        self.save("HBSAG", "Negative")
        self.assertEqual(get_case(self.conn, self.case["id"])["status"], "in_progress")

    def test_cannot_write_to_another_cases_test(self):
        other = create_case(self.conn, self.patient(name="B")["id"], consultant=None,
                            test_ids=[self.test_ids["HBSAG"]])
        ct_id, p_id = self.ids("HBSAG")
        with self.assertRaises(ValueError):
            save_results(self.conn, other["id"], {}, {ct_id: {p_id: ("Positive", "auto")}})


if __name__ == "__main__":
    unittest.main()
