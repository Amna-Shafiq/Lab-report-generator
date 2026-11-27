"""Password protection: first-start setup, login, auto-lock, forgot password, changes."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from labapp import app as app_module
from labapp import auth
from labapp.app import create_app
from labapp.db import connect
from tests.helpers import (TEST_ANSWER, TEST_PASSWORD, TEST_QUESTION, logged_in_client,
                           set_test_password)


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "lab.db"
        patcher = mock.patch.object(app_module, "WRONG_PASSWORD_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def stored(self, key):
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()


class FirstStartTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.client = create_app(self.db_path).test_client()

    def test_every_page_sends_to_setup_until_password_exists(self):
        for url in ("/", "/patients", "/pending", "/patients/new", "/login", "/forgot-password"):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertTrue(resp.headers["Location"].endswith("/setup"), url)

    def test_setup_creates_password_and_logs_in(self):
        resp = self.client.post("/setup", data={"password": "abc123", "confirm": "abc123",
                                                "question": TEST_QUESTION, "answer": "Lahore"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/patients").status_code, 200)
        self.assertTrue(self.stored(auth.PASSWORD_KEY).startswith("scrypt:"))  # hash, not the password
        self.assertNotIn("abc123", self.stored(auth.PASSWORD_KEY))
        self.assertNotIn("lahore", self.stored(auth.ANSWER_KEY).lower())
        self.assertEqual(self.client.get("/setup").status_code, 302)  # setup can't be run twice

    def test_setup_validation(self):
        for data, message in (
            ({"password": "abc", "confirm": "abc"}, "at least 6"),
            ({"password": "abc123", "confirm": "abc124"}, "do not match"),
            ({"password": "abc123", "confirm": "abc123", "question": "", "answer": "x"}, "security question"),
            ({"password": "abc123", "confirm": "abc123", "question": "Q?", "answer": "ab"}, "at least 3"),
        ):
            resp = self.client.post("/setup", data=data)
            self.assertEqual(resp.status_code, 400)
            self.assertIn(message, resp.get_data(as_text=True))
        self.assertIsNone(self.stored(auth.PASSWORD_KEY))


class LoginTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.db_path)
        set_test_password(self.db_path)
        self.client = self.app.test_client()

    def test_pages_need_login(self):
        for url in ("/patients", "/pending", "/patients/new", "/cases/1", "/prints/1.pdf", "/security"):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn("/login", resp.headers["Location"], url)

    def test_wrong_password(self):
        resp = self.client.post("/login", data={"password": "nope"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Wrong password", resp.get_data(as_text=True))
        self.assertEqual(self.client.get("/patients").status_code, 302)

    def test_login_returns_to_requested_page(self):
        resp = self.client.post("/login", data={"password": TEST_PASSWORD, "next": "/pending"})
        self.assertTrue(resp.headers["Location"].endswith("/pending"))

    def test_login_never_redirects_to_another_site(self):
        resp = self.client.post("/login", data={"password": TEST_PASSWORD, "next": "//evil.example"})
        self.assertEqual(resp.headers["Location"], "/")

    def test_login_page_has_forgot_link(self):
        self.assertIn("Forgot password?", self.client.get("/login").get_data(as_text=True))

    def test_logout(self):
        self.client.post("/login", data={"password": TEST_PASSWORD})
        self.client.get("/logout")
        self.assertEqual(self.client.get("/patients").status_code, 302)

    def test_auto_lock_after_idle(self):
        self.client.post("/login", data={"password": TEST_PASSWORD})
        self.assertEqual(self.client.get("/patients").status_code, 200)
        later = time.time() + 15 * 60 + 5
        with mock.patch.object(app_module, "_now", return_value=later):
            resp = self.client.get("/patients")
        self.assertEqual(resp.status_code, 302)
        page = self.client.get(resp.headers["Location"]).get_data(as_text=True)
        self.assertIn("Locked after 15 minutes", page)

    def test_activity_keeps_session_alive(self):
        self.client.post("/login", data={"password": TEST_PASSWORD})
        start = time.time()
        for minutes in (10, 20, 30):  # a request every 10 minutes: never idle for 15
            with mock.patch.object(app_module, "_now", return_value=start + minutes * 60):
                self.assertEqual(self.client.get("/keepalive").status_code, 204)
        with mock.patch.object(app_module, "_now", return_value=start + 31 * 60):
            self.assertEqual(self.client.get("/patients").status_code, 200)

    def test_pages_include_auto_lock_script(self):
        self.client.post("/login", data={"password": TEST_PASSWORD})
        page = self.client.get("/patients").get_data(as_text=True)
        self.assertIn("/logout?idle=1", page)
        self.assertIn("Log out", page)


class ForgotPasswordTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.db_path)
        set_test_password(self.db_path)
        self.client = self.app.test_client()

    def test_shows_question(self):
        self.assertIn(TEST_QUESTION, self.client.get("/forgot-password").get_data(as_text=True))

    def test_wrong_answer(self):
        resp = self.client.post("/forgot-password", data={"answer": "Karachi", "password": "newpass1",
                                                          "confirm": "newpass1"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not correct", resp.get_data(as_text=True))
        self.assertEqual(self.client.post("/login", data={"password": TEST_PASSWORD}).status_code, 302)

    def test_right_answer_sets_new_password(self):
        resp = self.client.post("/forgot-password", data={"answer": "  LAHORE ", "password": "newpass1",
                                                          "confirm": "newpass1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/patients").status_code, 200)  # logged in
        self.client.get("/logout")
        self.assertEqual(self.client.post("/login", data={"password": TEST_PASSWORD}).status_code, 401)
        self.assertEqual(self.client.post("/login", data={"password": "newpass1"}).status_code, 302)

    def test_new_password_must_be_valid(self):
        resp = self.client.post("/forgot-password", data={"answer": TEST_ANSWER, "password": "a",
                                                          "confirm": "a"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least 6", resp.get_data(as_text=True))


class SecuritySettingsTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.app, self.client = logged_in_client(self.db_path)

    def test_change_password_needs_current(self):
        resp = self.client.post("/security", data={"form": "password", "current": "wrong",
                                                   "password": "another1", "confirm": "another1"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Current password is wrong", resp.get_data(as_text=True))

    def test_change_password(self):
        resp = self.client.post("/security", data={"form": "password", "current": TEST_PASSWORD,
                                                   "password": "another1", "confirm": "another1"})
        self.assertEqual(resp.status_code, 302)
        self.client.get("/logout")
        self.assertEqual(self.client.post("/login", data={"password": "another1"}).status_code, 302)

    def test_change_question(self):
        resp = self.client.post("/security", data={"form": "question", "current": TEST_PASSWORD,
                                                   "question": "Favourite colour?", "answer": "Blue"})
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/forgot-password").get_data(as_text=True)
        self.assertIn("Favourite colour?", page)


if __name__ == "__main__":
    unittest.main()
