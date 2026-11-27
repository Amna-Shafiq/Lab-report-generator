"""Verification, report content, printing / reprints, and a real Playwright PDF."""

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from labapp import reports
from labapp.app import create_app
from labapp.lab_tests import (CaseLockedError, get_ordered_tests, reopen_case,
                              save_results, verify_case)
from labapp.registration import create_case, get_case
from labapp.reports import CaseNotVerifiedError, build_report, print_case
from tests.helpers import logged_in_client
from tests.test_step1 import DbTestCase


class CaseFixture(DbTestCase):
    """A case with HBsAg + Stool Occult Blood ordered."""

    def setUp(self):
        super().setUp()
        self.reports_dir = Path(self.tmp.name) / "reports"
        self.case = create_case(self.conn, self.patient()["id"], consultant="Dr. Sana",
                                test_ids=[self.test_ids["HBSAG"], self.test_ids["STOOL_OB"]])
        self.cid = self.case["id"]

    def enter(self, hbsag="Positive", stool="Negative", hbsag_method=None):
        methods, values = {}, {}
        for ot in get_ordered_tests(self.conn, self.cid):
            ct_id, p_id = ot.case_test["id"], ot.parameters[0].row["id"]
            value = hbsag if ot.case_test["code"] == "HBSAG" else stool
            values[ct_id] = {p_id: (value, "auto")}
            if hbsag_method and ot.case_test["code"] == "HBSAG":
                methods[ct_id] = hbsag_method
        save_results(self.conn, self.cid, methods, values)

    def status(self):
        return get_case(self.conn, self.cid)["status"]


class VerifyTests(CaseFixture):
    def test_cannot_verify_with_missing_results(self):
        with self.assertRaisesRegex(ValueError, "HBsAg Screening, Stool for Occult Blood"):
            verify_case(self.conn, self.cid)

    def test_verify_locks_results(self):
        self.enter()
        verify_case(self.conn, self.cid)
        self.assertEqual(self.status(), "verified")
        with self.assertRaises(CaseLockedError):
            self.enter(hbsag="Negative")

    def test_reopen_unlocks(self):
        self.enter()
        verify_case(self.conn, self.cid)
        reopen_case(self.conn, self.cid)
        self.assertEqual(self.status(), "in_progress")
        self.assertIsNone(get_case(self.conn, self.cid)["verified_at"])
        self.enter(hbsag="Negative")

    def test_cannot_reopen_unverified(self):
        with self.assertRaises(CaseLockedError):
            reopen_case(self.conn, self.cid)


class RemoveTestTests(CaseFixture):
    def ct_id(self, code):
        return next(ot.case_test["id"] for ot in get_ordered_tests(self.conn, self.cid)
                    if ot.case_test["code"] == code)

    def test_remove_test_and_its_results(self):
        from labapp.lab_tests import remove_test
        self.enter()
        stool = self.ct_id("STOOL_OB")
        self.assertEqual(remove_test(self.conn, self.cid, stool), "Stool for Occult Blood")
        codes = [ot.case_test["code"] for ot in get_ordered_tests(self.conn, self.cid)]
        self.assertEqual(codes, ["HBSAG"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM results WHERE case_test_id = ?",
                                           (stool,)).fetchone()[0], 0)
        self.assertEqual(self.status(), "in_progress")  # HBsAg result remains

    def test_status_back_to_registered_when_no_results_left(self):
        from labapp.lab_tests import remove_test
        ot = next(o for o in get_ordered_tests(self.conn, self.cid) if o.case_test["code"] == "STOOL_OB")
        save_results(self.conn, self.cid, {},
                     {ot.case_test["id"]: {ot.parameters[0].row["id"]: ("Positive", "auto")}})
        remove_test(self.conn, self.cid, ot.case_test["id"])
        self.assertEqual(self.status(), "registered")

    def test_cannot_remove_last_test(self):
        from labapp.lab_tests import remove_test
        remove_test(self.conn, self.cid, self.ct_id("STOOL_OB"))
        with self.assertRaisesRegex(ValueError, "at least one test"):
            remove_test(self.conn, self.cid, self.ct_id("HBSAG"))

    def test_cannot_remove_after_verify(self):
        from labapp.lab_tests import remove_test
        self.enter()
        verify_case(self.conn, self.cid)
        with self.assertRaises(CaseLockedError):
            remove_test(self.conn, self.cid, self.ct_id("STOOL_OB"))

    def test_cannot_remove_another_cases_test(self):
        from labapp.lab_tests import remove_test
        other = create_case(self.conn, self.patient(name="B")["id"], consultant=None,
                            test_ids=[self.test_ids["HBSAG"], self.test_ids["ANTI_HCV"]])
        with self.assertRaises(LookupError):
            remove_test(self.conn, other["id"], self.ct_id("STOOL_OB"))


class PendingTests(CaseFixture):
    def pending(self):
        from labapp.lab_tests import list_pending_cases
        return {c["id"]: c for c in list_pending_cases(self.conn)}

    def test_new_case_is_pending_with_progress(self):
        c = self.pending()[self.cid]
        self.assertEqual((c["entered_results"], c["total_results"]), (0, 2))
        self.assertEqual(c["test_names"], "HBsAg Screening, Stool for Occult Blood")

    def test_progress_counts_entered_results(self):
        ot = get_ordered_tests(self.conn, self.cid)[0]
        save_results(self.conn, self.cid, {},
                     {ot.case_test["id"]: {ot.parameters[0].row["id"]: ("Positive", "auto")}})
        self.assertEqual(self.pending()[self.cid]["entered_results"], 1)

    def test_printed_case_leaves_the_list(self):
        self.enter()
        verify_case(self.conn, self.cid)
        self.assertEqual(self.pending()[self.cid]["status"], "verified")
        print_case(self.conn, self.cid, self.reports_dir, lambda: b"%PDF")
        self.assertNotIn(self.cid, self.pending())


class ReportContentTests(CaseFixture):
    def test_sections_only_for_ordered_tests(self):
        self.enter()
        r = build_report(self.conn, self.cid)
        self.assertEqual([s.name for s in r["sections"]], ["Viral Hepatitis", "Specimen: Stool"])
        names = [t.name for s in r["sections"] for t in s.tests]
        self.assertEqual(names, ["HBsAg Screening", "Stool for Occult Blood"])
        self.assertNotIn("Anti HIV", names)

    def test_abnormal_flag_and_columns(self):
        self.enter(hbsag="Positive")
        hbsag = build_report(self.conn, self.cid)["sections"][0].tests[0]
        self.assertTrue(hbsag.single)
        row = hbsag.rows[0]
        self.assertEqual((row.normal, row.value, row.abnormal), ("Negative", "Positive", True))

    def test_ict_footnote_flag(self):
        self.enter()  # both tests default to ICT
        self.assertTrue(build_report(self.conn, self.cid)["has_ict"])

    def test_no_ict_footnote_when_no_ict(self):
        case = create_case(self.conn, self.patient(name="B")["id"], consultant=None,
                           test_ids=[self.test_ids["ANTI_HCV"]])
        ot = get_ordered_tests(self.conn, case["id"])[0]
        save_results(self.conn, case["id"], {ot.case_test["id"]: "ELISA"},
                     {ot.case_test["id"]: {ot.parameters[0].row["id"]: ("Negative", "auto")}})
        self.assertFalse(build_report(self.conn, case["id"])["has_ict"])

    def test_barcode_is_svg(self):
        self.assertTrue(reports.barcode_data_uri("482719365-1").startswith("data:image/svg+xml;base64,"))


class PrintTests(CaseFixture):
    def setUp(self):
        super().setUp()
        self.renders = 0

    def fake_pdf(self):
        self.renders += 1
        return b"%PDF-1.4 fake " + str(self.renders).encode()

    def print_(self, printed_by="Mehtab ur Rehman"):
        return print_case(self.conn, self.cid, self.reports_dir, self.fake_pdf, printed_by=printed_by)

    def test_different_printer_gets_own_pdf(self):
        self.ready()
        first = self.print_("Mehtab ur Rehman")
        other = self.print_("Someone Else")
        self.assertEqual(other["is_reprint"], 1)
        self.assertNotEqual(other["pdf_path"], first["pdf_path"])   # their name is on the PDF
        again = self.print_("Mehtab ur Rehman")
        self.assertEqual(again["pdf_path"], first["pdf_path"])      # same person: same file
        self.assertEqual(self.renders, 2)
        self.assertEqual(other["printed_by"], "Someone Else")

    def ready(self):
        self.enter()
        verify_case(self.conn, self.cid)

    def test_cannot_print_unverified(self):
        self.enter()
        with self.assertRaises(CaseNotVerifiedError):
            self.print_()

    def test_first_print_then_reprint_uses_same_file(self):
        self.ready()
        first = self.print_()
        self.assertEqual(first["is_reprint"], 0)
        self.assertTrue((self.reports_dir / first["pdf_path"]).is_file())
        self.assertEqual(self.status(), "printed")
        second = self.print_()
        self.assertEqual(second["is_reprint"], 1)
        self.assertEqual(second["pdf_path"], first["pdf_path"])
        self.assertEqual(self.renders, 1)

    def test_correction_creates_new_pdf(self):
        self.ready()
        first = self.print_()
        reopen_case(self.conn, self.cid)
        self.enter(hbsag="Negative")
        verify_case(self.conn, self.cid)
        corrected = self.print_()
        self.assertEqual(corrected["is_reprint"], 0)
        self.assertNotEqual(corrected["pdf_path"], first["pdf_path"])
        self.assertTrue((self.reports_dir / first["pdf_path"]).is_file())  # old version kept

    def test_missing_file_is_regenerated(self):
        self.ready()
        first = self.print_()
        (self.reports_dir / first["pdf_path"]).unlink()
        again = self.print_()
        self.assertEqual(again["is_reprint"], 1)
        self.assertTrue((self.reports_dir / again["pdf_path"]).is_file())
        self.assertEqual(self.renders, 2)


class WebReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "lab.db"
        _, self.client = logged_in_client(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            hbsag = c.execute("SELECT id FROM tests WHERE code = 'HBSAG'").fetchone()[0]
        c.close()
        resp = self.client.post("/patients/new", data={
            "name": "Ali Khan", "sex": "M", "dob": "1990-05-01", "test_id": [str(hbsag)]})
        self.case_url = resp.headers["Location"]
        self.cid = int(self.case_url.rsplit("/", 1)[-1])
        with sqlite3.connect(self.db_path) as c:
            self.ct_id, self.p_id = c.execute(
                "SELECT ct.id, p.id FROM case_tests ct JOIN test_parameters p ON p.test_id = ct.test_id "
                "WHERE ct.case_id = ?", (self.cid,)).fetchone()
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def enter(self, value="Positive", method="ICT"):
        return self.client.post(f"/cases/{self.cid}/results", data={
            f"method-{self.ct_id}": method,
            f"value-{self.ct_id}-{self.p_id}": value,
            f"flag-{self.ct_id}-{self.p_id}": "auto"})

    def test_preview_before_verify_is_marked(self):
        self.enter()
        page = self.client.get(f"/cases/{self.cid}/report").get_data(as_text=True)
        self.assertIn("NOT VERIFIED", page)
        self.assertIn('<td class="abnormal">Positive</td>', page)
        self.assertIn("Sensitivity and specificity of the ICT", page)
        self.assertEqual(len(re.findall(r'src="data:image/svg\+xml', page)), 2)

    def test_no_ict_footnote_for_elisa(self):
        self.enter(method="ELISA")
        page = self.client.get(f"/cases/{self.cid}/report").get_data(as_text=True)
        self.assertNotIn("Sensitivity and specificity of the ICT", page)

    def test_confirmations_present(self):
        page = self.client.get(self.case_url).get_data(as_text=True)
        self.assertNotIn("Verify case</button>", page)      # step 1: results still missing
        self.assertIn("Step 1 — enter the results", page)
        self.assertNotIn(">Remove</button>", page)  # only one test: nothing to remove
        self.enter()
        page = self.client.get(self.case_url).get_data(as_text=True)
        self.assertIn("Step 2 — verify", page)
        self.assertIn("return confirm('Verify this case?", page)

    def test_steps_after_verify_and_print(self):
        self.enter()
        self.client.post(f"/cases/{self.cid}/verify")
        page = self.client.get(self.case_url).get_data(as_text=True)
        self.assertIn("Step 3 — print", page)
        self.assertIn("Verified – ready to print", page)        # plain-language status
        self.assertIn("return confirm('Reopen this case", page)
        with mock.patch.object(reports, "html_to_pdf", return_value=b"%PDF-1.4 test"):
            self.client.post(f"/cases/{self.cid}/print", data=self.PRINTER)
        page = self.client.get(self.case_url).get_data(as_text=True)
        self.assertIn("Done — the report has been printed", page)

    def test_pending_page(self):
        page = self.client.get("/pending").get_data(as_text=True)
        self.assertIn("Awaiting results (1)", page)
        self.assertIn("0 / 1", page)
        self.enter()
        self.client.post(f"/cases/{self.cid}/verify")
        page = self.client.get("/pending").get_data(as_text=True)
        self.assertIn("Awaiting results (0)", page)
        self.assertIn("Ready to print (1)", page)

    PRINTER = {"printed_by": "Mehtab ur Rehman"}

    def test_print_requires_verification(self):
        self.enter()
        resp = self.client.post(f"/cases/{self.cid}/print", data=self.PRINTER)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("Cannot print", self.client.get(self.case_url).get_data(as_text=True))

    def test_print_requires_printed_by(self):
        self.enter()
        self.client.post(f"/cases/{self.cid}/verify")
        for data in ({}, {"printed_by": ""}, {"printed_by": "   "}):
            resp = self.client.post(f"/cases/{self.cid}/print", data=data)
            self.assertEqual(resp.status_code, 302)
            self.assertNotIn("/prints/", resp.headers["Location"])
        self.assertIn("Choose who is printing", self.client.get(self.case_url).get_data(as_text=True))

    def test_printed_by_dropdown_and_footer(self):
        self.enter()
        self.client.post(f"/cases/{self.cid}/verify")
        page = self.client.get(self.case_url).get_data(as_text=True)
        self.assertIn('<option value="Mehtab ur Rehman">Mehtab ur Rehman (Lab Technician)</option>', page)
        with mock.patch.object(reports, "html_to_pdf", return_value=b"%PDF-1.4 test") as pdf:
            self.client.post(f"/cases/{self.cid}/print", data=self.PRINTER)
        kwargs = pdf.call_args.kwargs
        self.assertEqual(kwargs["printed_by"], ["Mehtab ur Rehman", "Lab Technician"])
        self.assertEqual(kwargs["signature"][0], "Dr. M. Shafiq Aqil")
        self.assertNotIn("footer_text", kwargs)   # no patient name / Case # in the page footer
        self.assertIn("Mehtab ur Rehman", self.client.get(self.case_url).get_data(as_text=True))

    def test_printed_by_other_typed_name(self):
        self.enter()
        self.client.post(f"/cases/{self.cid}/verify")
        page = self.client.get(self.case_url).get_data(as_text=True)
        self.assertIn('<option value="__other__">Other (type name)…</option>', page)
        with mock.patch.object(reports, "html_to_pdf", return_value=b"%PDF-1.4 test") as pdf:
            resp = self.client.post(f"/cases/{self.cid}/print",
                                    data={"printed_by": "__other__", "printed_by_other": "  Ali   Raza "})
        self.assertIn("/prints/", resp.headers["Location"])
        self.assertEqual(pdf.call_args.kwargs["printed_by"], ["Ali Raza"])   # typed name: no title
        page = self.client.get(self.case_url).get_data(as_text=True)
        self.assertIn('<option value="Ali Raza">Ali Raza</option>', page)    # offered next time

    def test_printed_by_other_left_blank(self):
        self.enter()
        self.client.post(f"/cases/{self.cid}/verify")
        resp = self.client.post(f"/cases/{self.cid}/print",
                                data={"printed_by": "__other__", "printed_by_other": "   "})
        self.assertNotIn("/prints/", resp.headers["Location"])
        self.assertIn("type a name under Other", self.client.get(self.case_url).get_data(as_text=True))

    def test_verify_print_and_open_pdf(self):
        self.enter()
        self.client.post(f"/cases/{self.cid}/verify")
        with mock.patch.object(reports, "html_to_pdf", return_value=b"%PDF-1.4 test"):
            resp = self.client.post(f"/cases/{self.cid}/print", data=self.PRINTER)
        self.assertIn("/prints/", resp.headers["Location"])
        pdf = self.client.get(resp.headers["Location"])
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertEqual(pdf.data, b"%PDF-1.4 test")
        pdf.close()
        page = self.client.get(self.case_url).get_data(as_text=True)
        self.assertIn("Reprint report", page)
        self.assertIn("First print", page)


class RealPdfTest(unittest.TestCase):
    """Renders a real PDF with Chromium (~1-2 s)."""

    def test_html_to_pdf(self):
        try:
            pdf = reports.html_to_pdf("<h1>Hello</h1>")
        except Exception as e:  # browser not installed on this machine
            self.skipTest(f"Playwright/Chromium unavailable: {e}")
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
