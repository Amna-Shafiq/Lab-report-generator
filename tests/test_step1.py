"""Numbering, patients and cases. Run: .venv\\Scripts\\python -m unittest -v"""

import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from labapp import registration
from labapp.db import connect, init_db
from labapp.numbering import (format_case_no, format_reg_no, generate_reg_no,
                              is_valid_luhn, luhn_check_digit, normalize_nic,
                              normalize_reg_no, parse_case_no)
from labapp.registration import (DuplicateNICError, PatientNotFoundError,
                                 create_case, create_patient, get_case_by_number,
                                 get_patient_by_reg_no, register_patient)


class NumberingTests(unittest.TestCase):
    def test_luhn_known_value(self):
        self.assertEqual(luhn_check_digit("7992739871"), "3")
        self.assertTrue(is_valid_luhn("79927398713"))

    def test_luhn_catches_single_digit_errors(self):
        reg = generate_reg_no()
        for i in range(len(reg)):
            for d in "0123456789":
                if d != reg[i]:
                    self.assertFalse(is_valid_luhn(reg[:i] + d + reg[i + 1:]))

    def test_generated_reg_no_shape(self):
        for _ in range(1000):
            reg = generate_reg_no()
            self.assertEqual(len(reg), 9)
            self.assertTrue(is_valid_luhn(reg))

    def test_normalize_reg_no_accepts_scanner_and_dashes(self):
        reg = generate_reg_no()
        self.assertEqual(normalize_reg_no(format_reg_no(reg) + "\r\n"), reg)
        with self.assertRaises(ValueError):
            normalize_reg_no(reg[:-1] + str((int(reg[-1]) + 1) % 10))
        with self.assertRaises(ValueError):
            normalize_reg_no("１２３４５６７８９")  # full-width digits are not ASCII

    def test_case_no_round_trip(self):
        reg = generate_reg_no()
        self.assertEqual(format_case_no(reg, 2), f"{reg}-2")
        self.assertEqual(parse_case_no(f" {reg}-2 "), (reg, 2))

    def test_case_no_rejects_bad_input(self):
        reg = generate_reg_no()
        bad_reg = reg[:-1] + str((int(reg[-1]) + 1) % 10)
        for text in (f"{bad_reg}-2", f"{reg}-0", reg, f"{format_reg_no(reg)}-2"):
            with self.assertRaises(ValueError, msg=text):
                parse_case_no(text)

    def test_nic(self):
        self.assertEqual(normalize_nic("35202-1234567-1"), "3520212345671")
        self.assertIsNone(normalize_nic("  "))
        with self.assertRaises(ValueError):
            normalize_nic("35202-123")


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "lab.db"
        self.conn = connect(self.path)
        init_db(self.conn)
        self.test_ids = {r["code"]: r["id"] for r in self.conn.execute("SELECT id, code FROM tests")}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def patient(self, **kw):
        kw.setdefault("name", "Ali Khan")
        kw.setdefault("sex", "M")
        kw.setdefault("dob", date(1990, 5, 1))
        return create_patient(self.conn, **kw)


class PatientTests(DbTestCase):
    def test_create_patient(self):
        p = self.patient(nic="35202-1234567-1", guardian_name=" Ahmed Khan ")
        self.assertTrue(is_valid_luhn(p["reg_no"]))
        self.assertEqual(p["nic"], "3520212345671")
        self.assertEqual(p["guardian_name"], "Ahmed Khan")
        self.assertEqual(get_patient_by_reg_no(self.conn, format_reg_no(p["reg_no"]))["id"],
                         p["id"])

    def test_duplicate_nic_rejected_even_if_formatted_differently(self):
        self.patient(nic="35202-1234567-1")
        with self.assertRaises(DuplicateNICError):
            self.patient(name="Someone Else", nic="3520212345671")

    def test_multiple_patients_without_nic(self):
        self.patient(nic=None)
        self.patient(nic="")

    def test_reg_no_collision_retries(self):
        first = self.patient()
        with mock.patch.object(registration, "generate_reg_no",
                               side_effect=[first["reg_no"], first["reg_no"], "000000000"]):
            second = self.patient(name="Second")
        self.assertEqual(second["reg_no"], "000000000")

    def test_reg_no_gives_up_after_max_attempts(self):
        first = self.patient()
        with mock.patch.object(registration, "generate_reg_no", return_value=first["reg_no"]):
            with self.assertRaises(RuntimeError):
                self.patient(name="Second")

    def test_schema_rejects_bad_data(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.patient(sex="X")
        with self.assertRaises(sqlite3.IntegrityError):
            self.patient(blood_group="C+")
        with self.assertRaises(sqlite3.IntegrityError):
            self.patient(name="   ")

    def test_future_dob_rejected(self):
        with self.assertRaises(ValueError):
            self.patient(dob=date.today() + timedelta(days=1))

    def test_updated_at_trigger(self):
        p = self.patient()
        self.conn.execute("UPDATE patients SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                          (p["id"],))
        self.conn.execute("UPDATE patients SET phone = '0300' WHERE id = ?", (p["id"],))
        row = self.conn.execute("SELECT updated_at FROM patients WHERE id = ?",
                                (p["id"],)).fetchone()
        self.assertNotEqual(row[0], "2000-01-01T00:00:00Z")


class CaseTests(DbTestCase):
    def test_register_creates_case_1_with_tests(self):
        patient, case = register_patient(
            self.conn, name="Sara", sex="F", dob=date(1995, 1, 1),
            case_fields={"consultant": "Dr. Sana", "reference": "SELF",
                         "registration_location": "CLINIC DR SHAFIQ AQIL"},
            test_ids=[self.test_ids["HBSAG"], self.test_ids["ANTI_HCV"]])
        self.assertEqual(case["visit_no"], 1)
        self.assertEqual(case["consultant"], "Dr. Sana")
        self.assertEqual(case["registration_location"], "CLINIC DR SHAFIQ AQIL")
        self.assertIsNone(case["destination_location"])
        ordered = self.conn.execute(
            "SELECT t.code, ct.method FROM case_tests ct JOIN tests t ON t.id = ct.test_id "
            "WHERE ct.case_id = ? ORDER BY t.code", (case["id"],)).fetchall()
        self.assertEqual([tuple(r) for r in ordered], [("ANTI_HCV", "ICT"), ("HBSAG", "ICT")])

    def test_next_visits_number_up(self):
        p = self.patient()
        visits = [create_case(self.conn, p["id"], consultant=None,
                              test_ids=[self.test_ids["HBSAG"]])["visit_no"] for _ in range(3)]
        self.assertEqual(visits, [1, 2, 3])

    def test_visit_numbers_are_per_patient(self):
        a, b = self.patient(), self.patient(name="Other")
        hb = [self.test_ids["HBSAG"]]
        create_case(self.conn, a["id"], consultant=None, test_ids=hb)
        create_case(self.conn, a["id"], consultant=None, test_ids=hb)
        self.assertEqual(create_case(self.conn, b["id"], consultant=None, test_ids=hb)["visit_no"], 1)

    def test_find_case_by_number(self):
        p = self.patient()
        hb = [self.test_ids["HBSAG"]]
        create_case(self.conn, p["id"], consultant=None, test_ids=hb)
        second = create_case(self.conn, p["id"], consultant=None, test_ids=hb)
        self.assertEqual(get_case_by_number(self.conn, p["reg_no"], 2)["id"], second["id"])

    def test_case_needs_a_test(self):
        p = self.patient()
        with self.assertRaises(ValueError):
            create_case(self.conn, p["id"], consultant=None, test_ids=[])
        with self.assertRaises(ValueError):
            create_case(self.conn, p["id"], consultant=None, test_ids=[9999])

    def test_register_is_all_or_nothing(self):
        with self.assertRaises(ValueError):
            register_patient(self.conn, name="Nobody", sex="M", dob=date(1990, 1, 1),
                             test_ids=[])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM patients").fetchone()[0], 0)

    def test_unknown_patient(self):
        with self.assertRaises(PatientNotFoundError):
            create_case(self.conn, 999, consultant=None, test_ids=[self.test_ids["HBSAG"]])

    def test_concurrent_visits_for_same_patient_get_unique_numbers(self):
        p = self.patient()
        hb = [self.test_ids["HBSAG"]]
        visits, errors = [], []
        lock = threading.Lock()

        def worker():
            conn = connect(self.path)
            try:
                for _ in range(5):
                    c = create_case(conn, p["id"], consultant=None, test_ids=hb)
                    with lock:
                        visits.append(c["visit_no"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(visits), list(range(1, 51)))


if __name__ == "__main__":
    unittest.main()
