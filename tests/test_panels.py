"""Urine RE / Stool RE panels, optional parameters, catalog loader, v1 -> v2 migration, case fields."""

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from labapp import catalog
from labapp.app import create_app
from labapp.db import SCHEMA_PATH, SEED_TESTS_PATH, SCHEMA_VERSION, connect, init_db
from labapp.lab_tests import (get_ordered_tests, list_pending_cases, missing_results,
                              save_results, verify_case)
from labapp.registration import create_case
from labapp.reports import build_report
from tests.helpers import logged_in_client
from tests.test_step1 import DbTestCase


class PanelTests(DbTestCase):
    def order(self, *codes):
        case = create_case(self.conn, self.patient()["id"], consultant=None,
                           test_ids=[self.test_ids[c] for c in codes])
        return case["id"], get_ordered_tests(self.conn, case["id"])

    def fill(self, case_id, ot, overrides=None, skip_optional=True):
        """Enter every parameter: overrides, else the normal value / first option / a number."""
        overrides = overrides or {}
        entries = {}
        for p in ot.parameters:
            code = p.row["code"]
            if code in overrides:
                value = overrides[code]
            elif skip_optional and not p.row["is_required"]:
                continue
            elif p.row["input_type"] == "dropdown":
                normal = p.row["normal_text"]
                value = normal if normal in [o["value"] for o in p.options] else p.options[0]["value"]
            elif p.row["input_type"] == "number":
                value = str(p.row["normal_min"] if p.row["normal_min"] is not None else 0)
            else:
                value = "Nil"
            entries[p.row["id"]] = (value, "auto")
        save_results(self.conn, case_id, {}, {ot.case_test["id"]: entries})

    def test_urine_re_structure(self):
        _, [urine] = self.order("URINE_RE")
        groups = []
        for p in urine.parameters:
            if p.row["group_name"] not in groups:
                groups.append(p.row["group_name"])
        self.assertEqual(groups, ["Physical", "Chemical", "Microscopic"])
        self.assertEqual(len(urine.parameters), 22)
        self.assertEqual(urine.methods, [])

    def test_stool_re_structure(self):
        _, [stool] = self.order("STOOL_RE")
        self.assertEqual(len(stool.parameters), 19)
        optional = [p.row["name"] for p in stool.parameters if not p.row["is_required"]]
        self.assertEqual(optional, ["Frank Blood", "Occult Blood"])

    def test_optional_parameters_not_needed_for_verify(self):
        cid, [urine] = self.order("URINE_RE")
        self.fill(cid, urine)
        self.assertEqual(missing_results(self.conn, cid), [])
        verify_case(self.conn, cid)
        rows = {r.name: r for r in build_report(self.conn, cid)["sections"][0].tests[0].rows}
        self.assertEqual(rows["Spot Urinary Protein"].value, "-")
        self.assertEqual(rows["Spot Urinary Protein"].unit, "g/l")

    def test_missing_required_named_with_test(self):
        cid, [urine] = self.order("URINE_RE")
        self.assertIn("Urine RE: PH", missing_results(self.conn, cid))
        self.assertNotIn("Urine RE: Misc", missing_results(self.conn, cid))

    def test_number_range_sets_abnormal(self):
        cid, [urine] = self.order("URINE_RE")
        self.fill(cid, urine, {"SG": "1.040", "PH": "5.5", "PUS_CELLS": "10-12"})
        rows = {r.name: r for r in build_report(self.conn, cid)["sections"][0].tests[0].rows}
        self.assertTrue(rows["Specific Gravity"].abnormal)
        self.assertFalse(rows["PH"].abnormal)
        self.assertTrue(rows["Pus Cells"].abnormal)
        self.assertFalse(rows["Color"].abnormal)

    def test_pending_counts_only_required(self):
        cid, [stool] = self.order("STOOL_RE")
        self.fill(cid, stool)
        c = next(r for r in list_pending_cases(self.conn) if r["id"] == cid)
        self.assertEqual((c["entered_results"], c["total_results"]), (17, 17))


class CatalogLoaderTests(DbTestCase):
    def spec(self, **param):
        p = {"code": "X", "name": "X", "type": "dropdown", "options": ["A", "B"], **param}
        return {"code": "NEW", "name": "New", "section": "S", "parameters": [p]}

    def test_abnormal_must_be_an_option(self):
        with self.assertRaises(ValueError):
            catalog.add_test(self.conn, self.spec(abnormal=["C"]))

    def test_dropdown_needs_options(self):
        with self.assertRaises(ValueError):
            catalog.add_test(self.conn, self.spec(options=[]))

    def test_add_valid_test(self):
        test_id = catalog.add_test(self.conn, self.spec(abnormal=["B"]))
        opts = self.conn.execute(
            "SELECT o.value, o.is_abnormal FROM parameter_options o JOIN test_parameters p "
            "ON p.id = o.parameter_id WHERE p.test_id = ? ORDER BY o.display_order", (test_id,)).fetchall()
        self.assertEqual([tuple(r) for r in opts], [("A", 0), ("B", 1)])


class MigrationTests(unittest.TestCase):
    """Upgrade a database created by the previous version (v1) of the app."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "lab.db"
        conn = connect(self.path)
        conn.executescript("BEGIN;" + SCHEMA_PATH.read_text(encoding="utf-8")
                           + SEED_TESTS_PATH.read_text(encoding="utf-8")
                           + "PRAGMA user_version = 1; COMMIT;")
        # A v1 patient with a saved HBsAg result in the old wording.
        conn.execute("INSERT INTO patients (reg_no, name, dob, sex) VALUES ('000000000', 'Old', '1990-01-01', 'F')")
        conn.execute("INSERT INTO cases (patient_id, visit_no, registered_at) VALUES (1, 1, '2026-11-23T10:00:00Z')")
        conn.execute("INSERT INTO case_tests (case_id, test_id) SELECT 1, id FROM tests WHERE code = 'HBSAG'")
        conn.execute("INSERT INTO results (case_test_id, parameter_id, value, is_abnormal) "
                     "SELECT 1, p.id, 'Reactive', 1 FROM test_parameters p JOIN tests t ON t.id = p.test_id "
                     "WHERE t.code = 'HBSAG'")
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_upgrade_keeps_data_and_converts_wording(self):
        conn = connect(self.path)
        try:
            init_db(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT value FROM results").fetchone()[0], "Positive")
            self.assertEqual(conn.execute("SELECT name FROM tests WHERE code = 'HBSAG'").fetchone()[0],
                             "HBsAg Screening")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(cases)")]
            self.assertIn("registration_location", cols)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM tests WHERE code = 'URINE_RE'").fetchone())
            self.assertEqual(conn.execute("SELECT name FROM patients").fetchone()[0], "Old")
        finally:
            conn.close()
        self.assertTrue(Path(f"{self.path}.v1.bak").is_file())

    def test_init_db_twice_is_harmless(self):
        conn = connect(self.path)
        try:
            init_db(conn)
            init_db(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM tests WHERE code = 'URINE_RE'").fetchone()[0], 1)
        finally:
            conn.close()


class CaseFieldWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "lab.db"
        _, self.client = logged_in_client(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            self.urine = c.execute("SELECT id FROM tests WHERE code = 'URINE_RE'").fetchone()[0]
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_locations_and_reference_saved_and_suggested(self):
        resp = self.client.post("/patients/new", data={
            "name": "Fouzia", "sex": "F", "age_years": "35", "test_id": [str(self.urine)],
            "registration_location": "CLINIC DR SHAFIQ AQIL",
            "destination_location": "CLINIC DR SHAFIQ AQIL", "reference": ""})
        case_url = resp.headers["Location"]
        cid = case_url.rsplit("/", 1)[-1]
        report = self.client.get(f"/cases/{cid}/report").get_data(as_text=True)
        self.assertEqual(report.count("CLINIC DR SHAFIQ AQIL"), 2)
        self.assertIn("SELF", report)                     # blank reference prints SELF
        self.assertIn("Physical", report)                 # panel groups
        self.assertNotIn('class="test-title"', report)    # only test in its section: no title row
        form = self.client.get("/patients/new").get_data(as_text=True)
        self.assertIn("<option selected>CLINIC DR SHAFIQ AQIL</option>", form)

    def register(self, **fields):
        data = {"name": "Fouzia", "sex": "F", "age_years": "35", "test_id": [str(self.urine)]}
        data.update(fields)
        cid = self.client.post("/patients/new", data=data).headers["Location"].rsplit("/", 1)[-1]
        with sqlite3.connect(self.db_path) as c:
            row = c.execute("SELECT registration_location, destination_location, consultant "
                            "FROM cases WHERE id = ?", (cid,)).fetchone()
        c.close()
        return cid, row

    def test_new_case_form_defaults(self):
        form = self.client.get("/patients/new").get_data(as_text=True)
        self.assertEqual(form.count("<option selected>CLINIC DR SHAFIQ AQIL</option>"), 2)
        self.assertIn("<option >Dr. Shafiq Aqil</option>", form)          # offered, not pre-selected
        self.assertIn('<option value="" selected>SELF</option>', form)    # consultant defaults to SELF
        self.assertIn("Other (type below)", form)

    def test_dropdown_choice_saved(self):
        _, row = self.register(registration_location="CLINIC DR SHAFIQ AQIL",
                               destination_location="CLINIC DR SHAFIQ AQIL",
                               consultant="Dr. Shafiq Aqil")
        self.assertEqual(tuple(row), ("CLINIC DR SHAFIQ AQIL", "CLINIC DR SHAFIQ AQIL", "Dr. Shafiq Aqil"))

    def test_other_typed_value_saved_and_offered_next_time(self):
        _, row = self.register(registration_location="__other__",
                               registration_location_other="City Hospital",
                               consultant="__other__", consultant_other="Dr. Imran")
        self.assertEqual((row[0], row[2]), ("City Hospital", "Dr. Imran"))
        form = self.client.get("/patients/new").get_data(as_text=True)
        self.assertIn("<option >Dr. Imran</option>", form)
        self.assertIn("<option >City Hospital</option>", form)

    def test_other_left_blank_means_self(self):
        cid, row = self.register(consultant="__other__", consultant_other="  ")
        self.assertIsNone(row[2])
        report = self.client.get(f"/cases/{cid}/report").get_data(as_text=True)
        self.assertIn("<td class=\"v\">SELF</td>", report)

    def test_app_name_and_no_lab_header(self):
        page = self.client.get("/patients/new").get_data(as_text=True)
        self.assertIn("<title>New patient · Dr Shafiq Aqil Clinic</title>", page)
        self.assertIn('class="brand" href="/">Dr Shafiq Aqil Clinic</a>', page)
        cid, _ = self.register()
        report = self.client.get(f"/cases/{cid}/report").get_data(as_text=True)
        self.assertNotIn("Your Laboratory Name", report)
        self.assertNotIn("lab-name", report)
        self.assertIn("Patient #", report)   # barcodes stay

    def test_each_test_is_one_unsplittable_block(self):
        with sqlite3.connect(self.db_path) as c:
            extra = [str(r[0]) for r in c.execute(
                "SELECT id FROM tests WHERE code IN ('HBSAG', 'ANTI_HCV', 'STOOL_RE')")]
        c.close()
        cid, _ = self.register(test_id=[str(self.urine), *extra])
        report = self.client.get(f"/cases/{cid}/report").get_data(as_text=True)
        self.assertEqual(report.count('<div class="test-block">'), 4)   # one block per test
        self.assertIn(".test-block { break-inside: avoid; }", report)  # never split across pages
        self.assertEqual(report.count("<h2>"), 3)                        # one heading per section

    def test_signature_replaces_verified_by(self):
        cid, _ = self.register()
        report = self.client.get(f"/cases/{cid}/report").get_data(as_text=True)
        self.assertIn("Dr. M. Shafiq Aqil", report)
        self.assertNotIn("Prof.", report)
        self.assertIn("Consultant Gastroenterologist", report)
        self.assertNotIn("Verified by", report)

    def test_rejected_form_keeps_other_text(self):
        resp = self.client.post("/patients/new", data={
            "name": "", "sex": "F", "age_years": "35", "test_id": [str(self.urine)],
            "consultant": "__other__", "consultant_other": "Dr. Imran"})
        self.assertEqual(resp.status_code, 400)
        page = resp.get_data(as_text=True)
        self.assertIn('value="Dr. Imran"', page)
        self.assertIn('<option value="__other__" selected>', page)


if __name__ == "__main__":
    unittest.main()
