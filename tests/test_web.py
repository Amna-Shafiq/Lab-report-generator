"""Web UI tests (Flask test client). Run: .venv\\Scripts\\python -m unittest -v"""

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from labapp.app import age_text
from tests.helpers import logged_in_client
from labapp.numbering import format_reg_no


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "lab.db"
        self.app, self.client = logged_in_client(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            self.test_ids = dict(c.execute("SELECT code, id FROM tests"))
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, **overrides):
        data = {"name": "Ali Khan", "sex": "M", "dob": "1990-05-01",
                "nic": "35202-1234567-1", "guardian_name": "Ahmed Khan",
                "consultant": "Dr. Sana", "test_id": [str(self.test_ids["HBSAG"])]}
        data.update(overrides)
        return self.client.post("/patients/new", data=data)

    def reg_no_of(self, name="Ali Khan"):
        with sqlite3.connect(self.db_path) as c:
            reg_no = c.execute("SELECT reg_no FROM patients WHERE name = ?", (name,)).fetchone()[0]
        c.close()
        return reg_no

    def test_register_opens_case_1(self):
        resp = self.register()
        self.assertEqual(resp.status_code, 302)
        page = self.client.get(resp.headers["Location"]).get_data(as_text=True)
        reg_no = self.reg_no_of()
        self.assertIn(f"Case {reg_no}-1", page)
        self.assertIn("Dr. Sana", page)
        self.assertIn("HBsAg", page)

    def test_register_requires_a_test(self):
        resp = self.register(test_id=[])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Select at least one test", resp.get_data(as_text=True))

    def test_validation_errors_keep_input(self):
        resp = self.register(name="", nic="123")
        self.assertEqual(resp.status_code, 400)
        page = resp.get_data(as_text=True)
        self.assertIn("Name is required", page)
        self.assertIn("NIC must have 13 digits", page)
        self.assertIn('value="Ahmed Khan"', page)

    def test_duplicate_nic_says_already_registered(self):
        self.register()
        resp = self.register(name="Other Person", nic="3520212345671")
        self.assertEqual(resp.status_code, 400)
        page = resp.get_data(as_text=True)
        self.assertIn("Patient already registered", page)
        self.assertIn(format_reg_no(self.reg_no_of()), page)

    def test_relation_field_removed(self):
        page = self.client.get("/patients/new").get_data(as_text=True)
        self.assertNotIn("S/O", page)

    def test_next_visit_is_case_2(self):
        self.register()
        reg_no = self.reg_no_of()
        page = self.client.get(f"/patients/{reg_no}").get_data(as_text=True)
        self.assertIn(f"Case {reg_no}-2", page)
        resp = self.client.post(f"/patients/{reg_no}/cases",
                                data={"consultant": "Dr. Ahmed",
                                      "test_id": [str(self.test_ids["ANTI_HCV"])]})
        self.assertEqual(resp.status_code, 302)
        page = self.client.get(resp.headers["Location"]).get_data(as_text=True)
        self.assertIn(f"Case {reg_no}-2", page)
        self.assertIn("Anti HCV", page)

    def test_scan_case_no_and_reg_no(self):
        self.register()
        reg_no = self.reg_no_of()
        resp = self.client.get("/patients", query_string={"q": f"{reg_no}-1"})
        self.assertIn("/cases/", resp.headers["Location"])
        resp = self.client.get("/patients", query_string={"q": format_reg_no(reg_no)})
        self.assertTrue(resp.headers["Location"].endswith(f"/patients/{reg_no}"))

    def test_bad_check_digit_message(self):
        self.register()
        reg_no = self.reg_no_of()
        bad = reg_no[:-1] + str((int(reg_no[-1]) + 1) % 10)
        page = self.client.get("/patients", query_string={"q": bad}).get_data(as_text=True)
        self.assertIn("check digit failed", page)

    def test_enter_result_and_see_it_bold(self):
        case_url = self.register().headers["Location"]
        case_id = int(case_url.rsplit("/", 1)[-1])
        with sqlite3.connect(self.db_path) as c:
            ct_id, p_id = c.execute(
                "SELECT ct.id, p.id FROM case_tests ct JOIN test_parameters p ON p.test_id = ct.test_id "
                "WHERE ct.case_id = ?", (case_id,)).fetchone()
        c.close()
        resp = self.client.post(f"/cases/{case_id}/results", data={
            f"method-{ct_id}": "ELISA",
            f"value-{ct_id}-{p_id}": "Positive",
            f"flag-{ct_id}-{p_id}": "auto"})
        self.assertEqual(resp.status_code, 302)
        page = self.client.get(case_url).get_data(as_text=True)
        self.assertIn('class="abnormal"', page)
        self.assertIn("<option selected>ELISA</option>", page)

    def test_remove_test_from_case(self):
        case_url = self.register(test_id=[str(self.test_ids["HBSAG"]),
                                          str(self.test_ids["ANTI_HCV"])]).headers["Location"]
        case_id = int(case_url.rsplit("/", 1)[-1])
        page = self.client.get(case_url).get_data(as_text=True)
        self.assertEqual(page.count(">Remove</button>"), 2)
        self.assertIn("Remove HBsAg Screening from this case?", page)
        with sqlite3.connect(self.db_path) as c:
            ct_id = c.execute("SELECT ct.id FROM case_tests ct JOIN tests t ON t.id = ct.test_id "
                              "WHERE ct.case_id = ? AND t.code = 'ANTI_HCV'", (case_id,)).fetchone()[0]
        c.close()
        resp = self.client.post(f"/cases/{case_id}/tests/{ct_id}/remove")
        self.assertEqual(resp.status_code, 302)
        page = self.client.get(case_url).get_data(as_text=True)
        self.assertIn("Anti HCV Screening removed from this case", page)
        self.assertNotIn(">Remove</button>", page)

    def test_remove_keeps_unsaved_values_of_other_tests(self):
        """Typed-but-unsaved results for other tests survive removing a test."""
        case_url = self.register(test_id=[str(self.test_ids[c]) for c in
                                          ("HBSAG", "ANTI_HCV", "STOOL_OB")]).headers["Location"]
        case_id = int(case_url.rsplit("/", 1)[-1])
        with sqlite3.connect(self.db_path) as c:
            rows = {code: (ct, p) for ct, p, code in c.execute(
                "SELECT ct.id, p.id, t.code FROM case_tests ct JOIN tests t ON t.id = ct.test_id "
                "JOIN test_parameters p ON p.test_id = ct.test_id WHERE ct.case_id = ?", (case_id,))}
        c.close()
        form = {}
        for code, value in (("HBSAG", "Positive"), ("ANTI_HCV", "Negative"), ("STOOL_OB", "Positive")):
            ct, p = rows[code]
            form[f"value-{ct}-{p}"], form[f"flag-{ct}-{p}"], form[f"method-{ct}"] = value, "auto", "ICT"
        ct_ob = rows["STOOL_OB"][0]
        resp = self.client.post(f"/cases/{case_id}/tests/{ct_ob}/remove", data=form)
        self.assertEqual(resp.status_code, 302)
        page = self.client.get(case_url).get_data(as_text=True)
        self.assertIn("Your other entries were saved", page)
        self.assertNotIn('colspan="2">Stool for Occult Blood', page)   # its results table is gone
        with sqlite3.connect(self.db_path) as c:
            saved = dict(c.execute(
                "SELECT t.code, r.value FROM results r JOIN case_tests ct ON ct.id = r.case_test_id "
                "JOIN tests t ON t.id = ct.test_id WHERE ct.case_id = ?", (case_id,)))
        c.close()
        self.assertEqual(saved, {"HBSAG": "Positive", "ANTI_HCV": "Negative"})

    def test_remove_refused_keeps_typed_values(self):
        case_url = self.register().headers["Location"]  # HBsAg only
        case_id = int(case_url.rsplit("/", 1)[-1])
        with sqlite3.connect(self.db_path) as c:
            ct, p = c.execute("SELECT ct.id, p.id FROM case_tests ct JOIN test_parameters p "
                              "ON p.test_id = ct.test_id WHERE ct.case_id = ?", (case_id,)).fetchone()
        c.close()
        resp = self.client.post(f"/cases/{case_id}/tests/{ct}/remove",
                                data={f"value-{ct}-{p}": "Positive", f"flag-{ct}-{p}": "auto"})
        self.assertEqual(resp.status_code, 400)
        page = resp.get_data(as_text=True)
        self.assertIn("at least one test", page)
        self.assertIn("<option selected>Positive</option>", page)

    def test_save_without_fields_does_not_clear_results(self):
        case_url = self.register().headers["Location"]
        case_id = int(case_url.rsplit("/", 1)[-1])
        with sqlite3.connect(self.db_path) as c:
            ct, p = c.execute("SELECT ct.id, p.id FROM case_tests ct JOIN test_parameters p "
                              "ON p.test_id = ct.test_id WHERE ct.case_id = ?", (case_id,)).fetchone()
        c.close()
        self.client.post(f"/cases/{case_id}/results", data={f"value-{ct}-{p}": "Positive"})
        self.client.post(f"/cases/{case_id}/results", data={})
        with sqlite3.connect(self.db_path) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM results").fetchone()[0], 1)
        c.close()

    def test_unknown_patient_404(self):
        self.assertEqual(self.client.get("/patients/123456789").status_code, 404)

    def test_age_text(self):
        today = date(2026, 11, 23)
        self.assertEqual(age_text("1990-11-24", today), "35 Y")
        self.assertEqual(age_text("1990-11-23", today), "36 Y")
        self.assertEqual(age_text("2026-08-30", today), "2 M")
        self.assertEqual(age_text("2026-11-13", today), "10 D")


if __name__ == "__main__":
    unittest.main()
