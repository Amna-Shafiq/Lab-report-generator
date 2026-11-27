"""The shared app password and the security question used by "Forgot password".

Only salted scrypt hashes of the password and of the answer are stored.
"""

import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_KEY = "app_password_hash"
QUESTION_KEY = "security_question"
ANSWER_KEY = "security_answer_hash"
MIN_LENGTH = 6
MIN_ANSWER_LENGTH = 3

SUGGESTED_QUESTIONS = [
    "In which city was the clinic first opened?",
    "What was the name of your first school?",
    "What is your mother's maiden name?",
    "What was the make of your first car?",
]


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value,
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (key, value),
    )


def _normalize_answer(answer: str) -> str:
    """'  Lahore ' and 'lahore' count as the same answer."""
    return " ".join((answer or "").casefold().split())


# ---------- security question ----------

def get_question(conn: sqlite3.Connection) -> str | None:
    return _get(conn, QUESTION_KEY)


def validate_question(question: str, answer: str) -> str | None:
    if not (question or "").strip():
        return "Choose or type a security question"
    if len(_normalize_answer(answer)) < MIN_ANSWER_LENGTH:
        return f"The answer must be at least {MIN_ANSWER_LENGTH} characters"
    return None


def set_security_question(conn: sqlite3.Connection, question: str, answer: str) -> None:
    error = validate_question(question, answer)
    if error:
        raise ValueError(error)
    _put(conn, QUESTION_KEY, question.strip())
    _put(conn, ANSWER_KEY, generate_password_hash(_normalize_answer(answer)))


def check_answer(conn: sqlite3.Connection, answer: str) -> bool:
    stored = _get(conn, ANSWER_KEY)
    return stored is not None and check_password_hash(stored, _normalize_answer(answer))


# ---------- password ----------


def is_password_set(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (PASSWORD_KEY,)).fetchone() is not None


def check_password(conn: sqlite3.Connection, password: str) -> bool:
    stored = _get(conn, PASSWORD_KEY)
    return stored is not None and check_password_hash(stored, password or "")


def validate_new_password(password: str, confirm: str) -> str | None:
    """Error message, or None if acceptable."""
    if len(password or "") < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters"
    if password != confirm:
        return "The two passwords do not match"
    return None


def set_password(conn: sqlite3.Connection, password: str) -> None:
    if len(password or "") < MIN_LENGTH:
        raise ValueError(f"Password must be at least {MIN_LENGTH} characters")
    _put(conn, PASSWORD_KEY, generate_password_hash(password))
