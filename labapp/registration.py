"""Patient registration and case (visit) creation."""

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timezone

from .db import immediate
from .numbering import generate_reg_no, normalize_nic, normalize_reg_no

MAX_REG_NO_ATTEMPTS = 5


class DuplicateNICError(Exception):
    def __init__(self, nic: str):
        super().__init__(f"Patient already registered with NIC {nic}")
        self.nic = nic


class PatientNotFoundError(Exception):
    pass


def _is_unique_violation(err: sqlite3.IntegrityError, column: str) -> bool:
    return str(err) == f"UNIQUE constraint failed: {column}"


def _clean(text: str | None) -> str | None:
    """Trim whitespace; store empty strings as NULL."""
    if text is None:
        return None
    text = text.strip()
    return text or None


def _utc_now_iso(now: datetime | None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- internal inserts (caller owns the transaction) ----------

def _insert_patient(conn: sqlite3.Connection, *, name: str, sex: str, dob: date,
                    dob_is_estimated: bool = False, guardian_name: str | None = None,
                    blood_group: str | None = None, nic: str | None = None,
                    phone: str | None = None, address: str | None = None) -> sqlite3.Row:
    """Insert with a fresh random reg_no; retry only on a reg_no collision.

    A failed INSERT rolls back only that statement, so retrying inside an
    open transaction is safe.
    """
    if dob > date.today():
        raise ValueError("Date of birth is in the future")
    nic = normalize_nic(nic)
    params = {
        "name": _clean(name), "guardian_name": _clean(guardian_name),
        "dob": dob.isoformat(), "dob_is_estimated": int(dob_is_estimated),
        "sex": sex, "blood_group": blood_group, "nic": nic,
        "phone": _clean(phone), "address": _clean(address),
    }
    for _ in range(MAX_REG_NO_ATTEMPTS):
        params["reg_no"] = generate_reg_no()
        try:
            return conn.execute(
                """
                INSERT INTO patients (reg_no, name, guardian_name, dob, dob_is_estimated,
                                      sex, blood_group, nic, phone, address)
                VALUES (:reg_no, :name, :guardian_name, :dob, :dob_is_estimated,
                        :sex, :blood_group, :nic, :phone, :address)
                RETURNING *
                """,
                params,
            ).fetchall()[0]
        except sqlite3.IntegrityError as err:
            if _is_unique_violation(err, "patients.reg_no"):
                continue
            if _is_unique_violation(err, "patients.nic"):
                raise DuplicateNICError(nic) from err
            raise
    raise RuntimeError(f"Could not generate a unique reg_no in {MAX_REG_NO_ATTEMPTS} attempts")


CASE_TEXT_FIELDS = ("registration_location", "destination_location", "reference", "consultant")


def _insert_case(conn: sqlite3.Connection, patient_id: int, *, consultant: str | None,
                 test_ids: Iterable[int], now: datetime | None,
                 registration_location: str | None = None,
                 destination_location: str | None = None,
                 reference: str | None = None) -> sqlite3.Row:
    """Insert the patient's next visit and its ordered tests. Needs an IMMEDIATE transaction."""
    test_ids = sorted(set(test_ids))
    if not test_ids:
        raise ValueError("Select at least one test")
    placeholders = ",".join("?" * len(test_ids))
    tests = conn.execute(
        f"SELECT id, methods FROM tests WHERE is_active = 1 AND id IN ({placeholders})",
        test_ids,
    ).fetchall()
    if len(tests) != len(test_ids):
        raise ValueError("Unknown or inactive test selected")

    if conn.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
        raise PatientNotFoundError(f"No patient with id {patient_id}")
    visit_no = conn.execute(
        "SELECT COALESCE(MAX(visit_no), 0) + 1 FROM cases WHERE patient_id = ?", (patient_id,)
    ).fetchone()[0]
    case = conn.execute(
        """
        INSERT INTO cases (patient_id, visit_no, registered_at, consultant,
                           registration_location, destination_location, reference)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING *
        """,
        (patient_id, visit_no, _utc_now_iso(now), _clean(consultant),
         _clean(registration_location), _clean(destination_location), _clean(reference)),
    ).fetchall()[0]
    conn.executemany(
        "INSERT INTO case_tests (case_id, test_id, method) VALUES (?, ?, ?)",
        [(case["id"], t["id"], (t["methods"] or "").split(",")[0] or None) for t in tests],
    )
    return case


# ---------- public API (each call is one transaction) ----------

def register_patient(conn: sqlite3.Connection, *, case_fields: dict | None = None,
                     test_ids: Iterable[int], now: datetime | None = None,
                     **patient_fields) -> tuple[sqlite3.Row, sqlite3.Row]:
    """New patient + their first case (visit 1) with ordered tests, all or nothing.

    case_fields: any of CASE_TEXT_FIELDS (consultant, locations, reference).
    """
    case_fields = {"consultant": None, **(case_fields or {})}
    with immediate(conn):
        patient = _insert_patient(conn, **patient_fields)
        case = _insert_case(conn, patient["id"], test_ids=test_ids, now=now, **case_fields)
    return patient, case


def create_patient(conn: sqlite3.Connection, **patient_fields) -> sqlite3.Row:
    """Patient only, no case."""
    with immediate(conn):
        return _insert_patient(conn, **patient_fields)


def create_case(conn: sqlite3.Connection, patient_id: int, *, consultant: str | None,
                test_ids: Iterable[int], now: datetime | None = None,
                **other_case_fields) -> sqlite3.Row:
    """Next visit for an existing patient (visit_no = previous + 1).

    other_case_fields: registration_location, destination_location, reference.
    """
    with immediate(conn):
        return _insert_case(conn, patient_id, consultant=consultant,
                            test_ids=test_ids, now=now, **other_case_fields)


def case_field_suggestions(conn: sqlite3.Connection, limit: int = 200) -> dict[str, list[str]]:
    """Previously used values for each case text field (for autocomplete)."""
    return {
        f: [r[0] for r in conn.execute(
            f"SELECT {f} FROM cases WHERE {f} IS NOT NULL "
            f"GROUP BY {f} ORDER BY count(*) DESC, {f} LIMIT ?", (limit,))]
        for f in CASE_TEXT_FIELDS
    }


# ---------- lookups ----------

def get_patient(conn: sqlite3.Connection, patient_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()


def get_patient_by_reg_no(conn: sqlite3.Connection, raw: str) -> sqlite3.Row | None:
    """Lookup from typed or scanned input. Raises ValueError on a bad check digit."""
    reg_no = normalize_reg_no(raw)
    return conn.execute("SELECT * FROM patients WHERE reg_no = ?", (reg_no,)).fetchone()


def get_patient_by_nic(conn: sqlite3.Connection, raw: str) -> sqlite3.Row | None:
    nic = normalize_nic(raw)
    if nic is None:
        return None
    return conn.execute("SELECT * FROM patients WHERE nic = ?", (nic,)).fetchone()


def search_patients(conn: sqlite3.Connection, q: str, limit: int = 50) -> list[sqlite3.Row]:
    """Case-insensitive name substring match, or phone prefix match."""
    q = (q or "").strip()
    if not q:
        return []
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return conn.execute(
        """
        SELECT * FROM patients
        WHERE name LIKE :contains ESCAPE '\\' OR phone LIKE :prefix ESCAPE '\\'
        ORDER BY name COLLATE NOCASE, id
        LIMIT :limit
        """,
        {"contains": f"%{escaped}%", "prefix": f"{escaped}%", "limit": limit},
    ).fetchall()


def get_case(conn: sqlite3.Connection, case_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()


def get_case_by_number(conn: sqlite3.Connection, reg_no: str, visit_no: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.* FROM cases c JOIN patients p ON p.id = c.patient_id
        WHERE p.reg_no = ? AND c.visit_no = ?
        """,
        (reg_no, visit_no),
    ).fetchone()


def count_cases_today(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Cases registered since local midnight (registered_at is stored in UTC)."""
    now = (now or datetime.now().astimezone())
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(
        "SELECT count(*) FROM cases WHERE registered_at >= ? AND status <> 'cancelled'",
        (_utc_now_iso(midnight),),
    ).fetchone()[0]


def list_cases(conn: sqlite3.Connection, patient_id: int) -> list[sqlite3.Row]:
    """Newest first, with the ordered test names."""
    return conn.execute(
        """
        SELECT c.*, group_concat(t.name, ', ') AS test_names
        FROM cases c
        LEFT JOIN case_tests ct ON ct.case_id = c.id
        LEFT JOIN tests t ON t.id = ct.test_id
        WHERE c.patient_id = ?
        GROUP BY c.id
        ORDER BY c.visit_no DESC
        """,
        (patient_id,),
    ).fetchall()
