"""Shared test helpers."""

from labapp import auth
from labapp.app import create_app
from labapp.db import connect

TEST_PASSWORD = "secret123"
TEST_QUESTION = "In which city was the clinic first opened?"
TEST_ANSWER = "Lahore"


def set_test_password(db_path) -> None:
    conn = connect(db_path)
    try:
        auth.set_password(conn, TEST_PASSWORD)
        auth.set_security_question(conn, TEST_QUESTION, TEST_ANSWER)
    finally:
        conn.close()


def logged_in_client(db_path):
    """(app, client) for a fresh app whose password is set, already logged in."""
    app = create_app(db_path)
    set_test_password(db_path)
    client = app.test_client()
    resp = client.post("/login", data={"password": TEST_PASSWORD})
    assert resp.status_code == 302, "test login failed"
    return app, client
