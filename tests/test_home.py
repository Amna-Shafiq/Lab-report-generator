"""Home page: search box, today's numbers, worklists."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from labapp import app as app_module
from labapp.registration import count_cases_today, create_case, create_patient
from tests.helpers import logged_in_client
from tests.test_step1 import DbTestCase

PKT = timezone(timedelta(hours=5))


class HomePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "lab.db"
        _, self.client = logged_in_client(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            self.ids = dict(c.execute("SELECT code, id FROM tests"))
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, name, *codes):
        resp = self.client.post("/patients/new", data={
            "name": name, "sex": "F", "age_years": "30", "test_id": [str(self.ids[c]) for c in codes]})
        return int(resp.headers["Location"].rsplit("/", 1)[-1])

    def enter_all(self, case_id, value="Negative"):
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute("SELECT ct.id, p.id FROM case_tests ct JOIN test_parameters p "
                             "ON p.test_id = ct.test_id WHERE ct.case_id = ?", (case_id,)).fetchall()
        c.close()
        data = {}
        for ct, p in rows:
            data[f"value-{ct}-{p}"], data[f"flag-{ct}-{p}"] = value, "auto"
        self.client.post(f"/cases/{case_id}/results", data=data)

    def home(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_empty_home(self):
        page = self.home()
        self.assertIn('name="q" value="" autofocus', page)  # scanner-ready search box
        self.assertIn('action="/patients"', page)
        self.assertIn("<strong>0</strong> cases today", page)
        self.assertIn("Nothing waiting for results.", page)
        self.assertIn("Nothing waiting to print.", page)

    def test_worklists_and_counts(self):
        a = self.register("Ali Khan", "HBSAG", "ANTI_HCV")
        b = self.register("Sara Bibi", "STOOL_OB")
        self.enter_all(b)
        self.client.post(f"/cases/{b}/verify")
        page = self.home()
        self.assertIn("<strong>2</strong> cases today", page)
        self.assertIn("<strong>1</strong> awaiting results", page)
        self.assertIn("<strong>1</strong> ready to print", page)
        self.assertIn(f'href="/cases/{a}"', page)
        self.assertIn("HBsAg Screening, Anti HCV Screening", page)
        self.assertIn(">0 / 2<", page)
        self.assertIn(f'href="/cases/{b}"', page)
        self.assertIn("Verified", page)

    def test_ready_to_verify_label(self):
        a = self.register("Ali Khan", "HBSAG")
        self.enter_all(a)
        self.assertIn("Ready to verify", self.home())

    def test_list_limit_and_see_all(self):
        for i in range(app_module.HOME_LIST_LIMIT + 2):
            self.register(f"Patient {i}", "HBSAG")
        page = self.home()
        self.assertEqual(page.count('href="/cases/'), app_module.HOME_LIST_LIMIT)
        self.assertIn(f"See all {app_module.HOME_LIST_LIMIT + 2} →", page)

    def test_nav_has_home(self):
        page = self.home()
        self.assertIn('<a href="/" class="active" aria-current="page">Home</a>', page)
        self.assertIn('<a href="/pending">Pending cases</a>', page)


class CountTodayTests(DbTestCase):
    def test_counts_only_since_local_midnight(self):
        p = create_patient(self.conn, name="A", sex="M", dob=datetime(1990, 1, 1).date())
        hb = [self.test_ids["HBSAG"]]
        create_case(self.conn, p["id"], consultant=None, test_ids=hb,
                    now=datetime(2026, 11, 22, 23, 50, tzinfo=PKT))      # yesterday, local
        create_case(self.conn, p["id"], consultant=None, test_ids=hb,
                    now=datetime(2026, 11, 23, 0, 10, tzinfo=PKT))       # today, local
        self.assertEqual(count_cases_today(self.conn, now=datetime(2026, 11, 23, 9, 0, tzinfo=PKT)), 1)


if __name__ == "__main__":
    unittest.main()
