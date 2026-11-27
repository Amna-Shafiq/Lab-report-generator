"""Test catalog queries, result entry and automatic abnormal flags."""

import sqlite3
from dataclasses import dataclass, field

from .db import immediate

FLAG_CHOICES = ("auto", "normal", "abnormal")


EDITABLE_STATUSES = ("registered", "in_progress")


class CaseLockedError(Exception):
    """Results cannot change: the case is verified, printed or cancelled."""


class ResultValidationError(Exception):
    def __init__(self, errors: dict[int, str]):
        super().__init__("Some results are invalid")
        self.errors = errors  # parameter_id -> message


@dataclass
class Parameter:
    row: sqlite3.Row
    options: list[sqlite3.Row] = field(default_factory=list)
    result: sqlite3.Row | None = None


@dataclass
class OrderedTest:
    case_test: sqlite3.Row      # case_tests row joined with test columns
    methods: list[str]
    parameters: list[Parameter]


def list_active_tests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tests WHERE is_active = 1 ORDER BY display_order, name"
    ).fetchall()


def get_ordered_tests(conn: sqlite3.Connection, case_id: int) -> list[OrderedTest]:
    """Everything needed to show / enter results for a case, in report order."""
    case_tests = conn.execute(
        """
        SELECT ct.*, t.code, t.name AS test_name, t.section, t.methods, t.is_sensitive
        FROM case_tests ct JOIN tests t ON t.id = ct.test_id
        WHERE ct.case_id = ?
        ORDER BY t.display_order, t.name
        """,
        (case_id,),
    ).fetchall()
    ordered = []
    for ct in case_tests:
        params = [Parameter(p) for p in conn.execute(
            "SELECT * FROM test_parameters WHERE test_id = ? ORDER BY display_order, id",
            (ct["test_id"],),
        )]
        for p in params:
            p.options = conn.execute(
                "SELECT * FROM parameter_options WHERE parameter_id = ? ORDER BY display_order, id",
                (p.row["id"],),
            ).fetchall()
            p.result = conn.execute(
                "SELECT * FROM results WHERE case_test_id = ? AND parameter_id = ?",
                (ct["id"], p.row["id"]),
            ).fetchone()
        methods = [m.strip() for m in (ct["methods"] or "").split(",") if m.strip()]
        ordered.append(OrderedTest(ct, methods, params))
    return ordered


def auto_abnormal(param: Parameter, value: str) -> bool:
    """Abnormal according to the catalog: option flag, or outside the numeric range."""
    p = param.row
    if p["input_type"] == "dropdown":
        return any(o["value"] == value and o["is_abnormal"] for o in param.options)
    if p["input_type"] == "number":
        n = float(value)
        return ((p["normal_min"] is not None and n < p["normal_min"])
                or (p["normal_max"] is not None and n > p["normal_max"]))
    return False


def save_results(conn: sqlite3.Connection, case_id: int,
                 methods: dict[int, str],
                 values: dict[int, dict[int, tuple[str, str]]],
                 entered_by: str | None = None) -> None:
    """Save results for a case in one transaction.

    methods: case_test_id -> method
    values:  case_test_id -> {parameter_id: (value, flag)}, flag in FLAG_CHOICES.
    An empty value clears that result.
    """
    _require_status(conn, case_id, EDITABLE_STATUSES)
    ordered ={ot.case_test["id"]: ot for ot in get_ordered_tests(conn, case_id)}
    errors: dict[int, str] = {}
    writes: list[tuple] = []     # (case_test_id, parameter_id, value, is_abnormal, manual)
    clears: list[tuple] = []     # (case_test_id, parameter_id)

    for ct_id, method in methods.items():
        if ct_id not in ordered or (method and method not in ordered[ct_id].methods):
            raise ValueError(f"Invalid method {method!r} for case test {ct_id}")

    for ct_id, entries in values.items():
        if ct_id not in ordered:
            raise ValueError(f"Case test {ct_id} does not belong to case {case_id}")
        params = {p.row["id"]: p for p in ordered[ct_id].parameters}
        for param_id, (value, flag) in entries.items():
            param = params.get(param_id)
            if param is None:
                raise ValueError(f"Parameter {param_id} is not part of case test {ct_id}")
            value = (value or "").strip()
            if not value:
                clears.append((ct_id, param_id))
                continue
            if flag not in FLAG_CHOICES:
                raise ValueError(f"Invalid flag {flag!r}")
            kind = param.row["input_type"]
            if kind == "dropdown" and value not in {o["value"] for o in param.options}:
                errors[param_id] = "Choose one of the listed values"
                continue
            if kind == "number":
                try:
                    float(value)
                except ValueError:
                    errors[param_id] = "Enter a number"
                    continue
            if flag == "auto":
                writes.append((ct_id, param_id, value, int(auto_abnormal(param, value)), 0))
            else:
                writes.append((ct_id, param_id, value, int(flag == "abnormal"), 1))

    if errors:
        raise ResultValidationError(errors)

    with immediate(conn):
        _require_status(conn, case_id, EDITABLE_STATUSES)  # re-check under the write lock
        for ct_id, method in methods.items():
            conn.execute("UPDATE case_tests SET method = ? WHERE id = ?", (method or None, ct_id))
        conn.executemany(
            """
            INSERT INTO results (case_test_id, parameter_id, value, is_abnormal,
                                 abnormal_manual, entered_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (case_test_id, parameter_id) DO UPDATE SET
                value = excluded.value,
                is_abnormal = excluded.is_abnormal,
                abnormal_manual = excluded.abnormal_manual,
                entered_by = excluded.entered_by
            """,
            [(*w, entered_by) for w in writes],
        )
        conn.executemany(
            "DELETE FROM results WHERE case_test_id = ? AND parameter_id = ?", clears
        )
        if writes:
            conn.execute(
                "UPDATE cases SET status = 'in_progress' WHERE id = ? AND status = 'registered'",
                (case_id,),
            )


def remove_test(conn: sqlite3.Connection, case_id: int, case_test_id: int) -> str:
    """Remove an ordered test (and any results entered for it) from an open case.

    A case must keep at least one test. Returns the removed test's name.
    """
    with immediate(conn):
        _require_status(conn, case_id, EDITABLE_STATUSES)
        row = conn.execute(
            "SELECT t.name FROM case_tests ct JOIN tests t ON t.id = ct.test_id "
            "WHERE ct.id = ? AND ct.case_id = ?",
            (case_test_id, case_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"Test {case_test_id} is not on case {case_id}")
        count = conn.execute("SELECT count(*) FROM case_tests WHERE case_id = ?",
                             (case_id,)).fetchone()[0]
        if count <= 1:
            raise ValueError("A case must keep at least one test")
        conn.execute("DELETE FROM case_tests WHERE id = ?", (case_test_id,))  # results cascade
        has_results = conn.execute(
            "SELECT 1 FROM results r JOIN case_tests ct ON ct.id = r.case_test_id "
            "WHERE ct.case_id = ? LIMIT 1", (case_id,)).fetchone()
        if not has_results:
            conn.execute("UPDATE cases SET status = 'registered' WHERE id = ?", (case_id,))
    return row["name"]


# ---------- worklist ----------

def list_pending_cases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Cases not yet printed (or cancelled), oldest first, with result progress."""
    return conn.execute(
        """
        SELECT c.id, c.visit_no, c.status, c.registered_at, c.consultant,
               p.reg_no, p.name,
               (SELECT group_concat(name, ', ') FROM (
                    SELECT t.name FROM case_tests ct JOIN tests t ON t.id = ct.test_id
                    WHERE ct.case_id = c.id ORDER BY t.display_order)) AS test_names,
               (SELECT count(*) FROM case_tests ct
                    JOIN test_parameters tp ON tp.test_id = ct.test_id
                    WHERE ct.case_id = c.id AND tp.is_required = 1) AS total_results,
               (SELECT count(*) FROM case_tests ct
                    JOIN results r ON r.case_test_id = ct.id
                    JOIN test_parameters tp ON tp.id = r.parameter_id
                    WHERE ct.case_id = c.id AND tp.is_required = 1) AS entered_results
        FROM cases c JOIN patients p ON p.id = c.patient_id
        WHERE c.status IN ('registered', 'in_progress', 'verified')
        ORDER BY c.registered_at, c.id
        """
    ).fetchall()


# ---------- verification ----------

def _require_status(conn: sqlite3.Connection, case_id: int, allowed: tuple[str, ...]) -> str:
    row = conn.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        raise LookupError(f"No case {case_id}")
    if row["status"] not in allowed:
        raise CaseLockedError(f"Case is {row['status']}")
    return row["status"]


def missing_results(conn: sqlite3.Connection, case_id: int) -> list[str]:
    """Required parameters that still have no result, e.g. ['HBsAg', 'Urine RE: PH']."""
    rows = conn.execute(
        """
        SELECT t.name AS test_name, p.name AS param_name
        FROM case_tests ct
        JOIN tests t ON t.id = ct.test_id
        JOIN test_parameters p ON p.test_id = ct.test_id
        LEFT JOIN results r ON r.case_test_id = ct.id AND r.parameter_id = p.id
        WHERE ct.case_id = ? AND r.id IS NULL AND p.is_required = 1
        ORDER BY t.display_order, p.display_order
        """,
        (case_id,),
    ).fetchall()
    return [r["test_name"] if r["test_name"] == r["param_name"]
            else f"{r['test_name']}: {r['param_name']}" for r in rows]


def verify_case(conn: sqlite3.Connection, case_id: int, verified_by: str | None = None) -> None:
    """Lock the results. Every parameter of every ordered test must have a result."""
    with immediate(conn):
        _require_status(conn, case_id, EDITABLE_STATUSES)
        missing = missing_results(conn, case_id)
        if missing:
            raise ValueError("Results missing: " + ", ".join(missing))
        conn.execute(
            """
            UPDATE cases SET status = 'verified', verified_by = ?,
                             verified_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                             verify_count = verify_count + 1
            WHERE id = ?
            """,
            (verified_by, case_id),
        )


def reopen_case(conn: sqlite3.Connection, case_id: int) -> None:
    """Unlock a verified or printed case so results can be corrected (re-verify afterwards)."""
    with immediate(conn):
        _require_status(conn, case_id, ("verified", "printed"))
        conn.execute(
            "UPDATE cases SET status = 'in_progress', verified_at = NULL, verified_by = NULL "
            "WHERE id = ?",
            (case_id,),
        )
