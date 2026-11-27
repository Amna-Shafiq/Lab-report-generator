"""Flask web UI: patient search, registration, visits and result entry."""

import os
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from flask import (Flask, abort, current_app, flash, g, redirect, render_template,
                   request, send_file, session, url_for)

from . import auth, lab_tests, reports, settings
from . import registration as reg
from .db import connect, immediate, init_db
from .numbering import (format_case_no, format_nic, format_reg_no, normalize_nic,
                        parse_case_no)

SEXES = {"M": "Male", "F": "Female", "O": "Other"}
# Plain-language case statuses shown to staff (the stored values stay short codes).
STATUS_LABELS = {
    "registered": "Waiting for results",
    "in_progress": "Results being entered",
    "verified": "Verified – ready to print",
    "printed": "Printed",
    "cancelled": "Cancelled",
}
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
_NUMERIC_INPUT = re.compile(r"[0-9\s-]+")


def create_app(db_path: str | Path) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)
    app.config["REPORTS_DIR"] = Path(db_path).parent / "reports"  # saved PDFs
    # Random per start: signs the login cookie, so restarting the app logs everyone out.
    app.secret_key = os.urandom(32)
    # Strict: the login cookie is never sent with requests started by other websites.
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_db(conn)
    finally:
        conn.close()

    _register_helpers(app)
    _register_auth(app)
    _register_routes(app)
    return app


# ---------- per-request DB connection ----------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def _close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------- display helpers ----------

def age_text(dob: str, today: date | None = None) -> str:
    """'1990-05-01' -> '35 Y'; infants as months or days."""
    born = date.fromisoformat(dob)
    today = today or date.today()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if years >= 1:
        return f"{years} Y"
    months = (today.year - born.year) * 12 + today.month - born.month - (today.day < born.day)
    if months >= 1:
        return f"{months} M"
    return f"{(today - born).days} D"


def local_time(utc_iso: str) -> str:
    return datetime.fromisoformat(utc_iso).astimezone().strftime("%d-%m-%Y %I:%M %p")


def short_time(utc_iso: str, today: date | None = None) -> str:
    """'09:10 AM' for today, '21 Nov, 09:10 AM' for earlier days."""
    t = datetime.fromisoformat(utc_iso).astimezone()
    if t.date() == (today or date.today()):
        return t.strftime("%I:%M %p")
    return t.strftime("%d %b, %I:%M %p")


def _register_helpers(app: Flask) -> None:
    app.teardown_appcontext(_close_db)
    app.jinja_env.filters.update(
        reg_no=format_reg_no,
        nic=lambda v: format_nic(v) if v else "",
        age=age_text,
        local_time=local_time,
        short_time=short_time,
        status_label=lambda s: STATUS_LABELS.get(s, s),
        dmy=lambda iso: date.fromisoformat(iso).strftime("%d-%m-%Y") if iso else "",
        sex=lambda v: SEXES.get(v, v),
    )
    app.jinja_env.globals.update(
        case_no=format_case_no, SEXES=SEXES, BLOOD_GROUPS=BLOOD_GROUPS, lab=settings,
    )


# ---------- form parsing ----------

def _parse_patient_form(form) -> tuple[dict, dict]:
    """Return (kwargs for register_patient, errors by field name)."""
    errors: dict[str, str] = {}
    data = {
        "name": form.get("name", "").strip(),
        "guardian_name": form.get("guardian_name", ""),
        "sex": form.get("sex", ""),
        "blood_group": form.get("blood_group") or None,
        "nic": form.get("nic", ""),
        "phone": form.get("phone", ""),
        "address": form.get("address", ""),
    }
    if not data["name"]:
        errors["name"] = "Name is required"
    if data["sex"] not in SEXES:
        errors["sex"] = "Select sex"
    if data["blood_group"] not in (None, *BLOOD_GROUPS):
        errors["blood_group"] = "Invalid blood group"
    try:
        normalize_nic(data["nic"])
    except ValueError:
        errors["nic"] = "NIC must have 13 digits"

    dob_raw = form.get("dob", "").strip()
    age_raw = form.get("age_years", "").strip()
    if dob_raw:
        try:
            data["dob"] = date.fromisoformat(dob_raw)
            data["dob_is_estimated"] = False
            if data["dob"] > date.today():
                errors["dob"] = "Date of birth is in the future"
        except ValueError:
            errors["dob"] = "Invalid date"
    elif age_raw:
        if age_raw.isdecimal() and 0 <= int(age_raw) <= 130:
            today = date.today()
            # Approximate DOB: same day-of-year, N years ago (Feb 29 -> Feb 28).
            try:
                data["dob"] = today.replace(year=today.year - int(age_raw))
            except ValueError:
                data["dob"] = today.replace(year=today.year - int(age_raw), day=28)
            data["dob_is_estimated"] = True
        else:
            errors["dob"] = "Age must be a whole number of years (0-130)"
    else:
        errors["dob"] = "Enter date of birth or age"
    return data, errors


def _parse_visit_form(form) -> tuple[dict, list[int], dict]:
    """Case text fields (consultant, locations, reference) + selected tests (test_id checkboxes)."""
    errors = {}
    try:
        test_ids = [int(v) for v in form.getlist("test_id")]
    except ValueError:
        test_ids = []
    if not test_ids:
        errors["tests"] = "Select at least one test"
    case_fields = {}
    for f in reg.CASE_TEXT_FIELDS:
        value = form.get(f, "")
        if value == OTHER:  # dropdown "Other": use the typed text
            value = form.get(f"{f}_other", "")
        case_fields[f] = value
    return case_fields, test_ids, errors


OTHER = "__other__"


def _case_choices(conn) -> dict[str, list[str]]:
    """Dropdown options: configured values first, then others used before."""
    used = reg.case_field_suggestions(conn)

    def merge(preset, previous):
        seen, out = set(), []
        for v in [*preset, *previous]:
            if v.casefold() not in seen:
                seen.add(v.casefold())
                out.append(v)
        return out

    locations = merge(settings.LOCATION_CHOICES,
                      used["registration_location"] + used["destination_location"])
    return {
        "registration_location": locations,
        "destination_location": locations,
        "consultant": merge(settings.CONSULTANT_CHOICES, used["consultant"]),
    }


def _tests_by_section(conn) -> dict[str, list]:
    sections: dict[str, list] = {}
    for t in lab_tests.list_active_tests(conn):
        sections.setdefault(t["section"], []).append(t)
    return sections


# ---------- password protection ----------

HOME_LIST_LIMIT = 10  # rows per worklist on the home page; the rest via "See all"

PUBLIC_ENDPOINTS = {"login", "setup", "forgot_password", "logout", "static"}
WRONG_PASSWORD_DELAY = 1.0  # seconds; slows down guessing


def _now() -> float:
    """Clock for the idle timer (separate function so tests can fake it)."""
    return time.time()


def _requested_page() -> str:
    """Current page path, for returning after login ('/pending', not '/pending?')."""
    return request.full_path.rstrip("?")


def _safe_next(target: str | None) -> str:
    """Only allow redirects to pages of this app (no //other-site URLs)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


def _log_in() -> None:
    session.clear()
    session["auth"] = True
    session["last_seen"] = _now()


def _register_auth(app: Flask) -> None:
    idle_limit = settings.AUTO_LOCK_MINUTES * 60

    @app.before_request
    def require_password():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not auth.is_password_set(get_db()):
            return redirect(url_for("setup"))
        if not session.get("auth"):
            return redirect(url_for("login", next=_requested_page()))
        now = _now()
        if now - session.get("last_seen", 0) > idle_limit:
            session.clear()
            flash(f"Locked after {settings.AUTO_LOCK_MINUTES} minutes without activity. "
                  "Enter the password to continue.", "error")
            return redirect(url_for("login", next=_requested_page()))
        session["last_seen"] = now
        return None

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """First start: create the password and the security question."""
        db = get_db()
        if auth.is_password_set(db):
            return redirect(url_for("login"))
        form, error = request.form, None
        if request.method == "POST":
            error = (auth.validate_new_password(form.get("password", ""), form.get("confirm", ""))
                     or auth.validate_question(form.get("question", ""), form.get("answer", "")))
            if not error:
                with immediate(db):
                    auth.set_password(db, form["password"])
                    auth.set_security_question(db, form["question"], form["answer"])
                _log_in()
                flash("Password created. Remember your security question answer — "
                      "it is the only way to reset a forgotten password.")
                return redirect(url_for("index"))
        return render_template("setup.html", form=form, error=error,
                               questions=auth.SUGGESTED_QUESTIONS), (400 if error else 200)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        db = get_db()
        if not auth.is_password_set(db):
            return redirect(url_for("setup"))
        error = None
        if request.method == "POST":
            if auth.check_password(db, request.form.get("password", "")):
                _log_in()
                return redirect(_safe_next(request.form.get("next")))
            time.sleep(WRONG_PASSWORD_DELAY)
            error = "Wrong password"
        return render_template("login.html", error=error,
                               next=request.values.get("next", "")), (401 if error else 200)

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        db = get_db()
        if not auth.is_password_set(db):
            return redirect(url_for("setup"))
        question = auth.get_question(db)
        error = None
        if request.method == "POST":
            f = request.form
            if not auth.check_answer(db, f.get("answer", "")):
                time.sleep(WRONG_PASSWORD_DELAY)
                error = "That answer is not correct"
            else:
                error = auth.validate_new_password(f.get("password", ""), f.get("confirm", ""))
                if not error:
                    with immediate(db):
                        auth.set_password(db, f["password"])
                    _log_in()
                    flash("New password set.")
                    return redirect(url_for("index"))
        return render_template("forgot_password.html", question=question, error=error), (
            400 if error else 200)

    @app.get("/keepalive")
    def keepalive():
        """Pinged by the page while staff are active; require_password refreshes last_seen."""
        return "", 204

    @app.get("/logout")
    def logout():
        idle = request.args.get("idle") == "1"
        session.clear()
        if idle:
            flash(f"Locked after {settings.AUTO_LOCK_MINUTES} minutes without activity. "
                  "Enter the password to continue.", "error")
        return redirect(url_for("login"))

    @app.route("/security", methods=["GET", "POST"])
    def security_settings():
        """Change the password or the security question (current password required)."""
        db = get_db()
        errors: dict[str, str] = {}
        if request.method == "POST":
            f = request.form
            which = f.get("form")
            if not auth.check_password(db, f.get("current", "")):
                time.sleep(WRONG_PASSWORD_DELAY)
                errors[which] = "Current password is wrong"
            elif which == "password":
                err = auth.validate_new_password(f.get("password", ""), f.get("confirm", ""))
                if err:
                    errors[which] = err
                else:
                    with immediate(db):
                        auth.set_password(db, f["password"])
                    flash("Password changed.")
                    return redirect(url_for("security_settings"))
            elif which == "question":
                err = auth.validate_question(f.get("question", ""), f.get("answer", ""))
                if err:
                    errors[which] = err
                else:
                    with immediate(db):
                        auth.set_security_question(db, f["question"], f["answer"])
                    flash("Security question changed.")
                    return redirect(url_for("security_settings"))
            else:
                abort(400)
        return render_template("security.html", errors=errors, question=auth.get_question(db),
                               questions=auth.SUGGESTED_QUESTIONS), (400 if errors else 200)


# ---------- routes ----------

def _register_routes(app: Flask) -> None:

    @app.get("/")
    def index():
        """Home: search box, today's numbers, and the two worklists."""
        db = get_db()
        pending = lab_tests.list_pending_cases(db)
        awaiting = [c for c in pending if c["status"] in lab_tests.EDITABLE_STATUSES]
        to_print = [c for c in pending if c["status"] == "verified"]
        return render_template(
            "home.html", awaiting=awaiting[:HOME_LIST_LIMIT], to_print=to_print[:HOME_LIST_LIMIT],
            awaiting_count=len(awaiting), to_print_count=len(to_print),
            today_count=reg.count_cases_today(db), today=datetime.now(),
        )

    @app.get("/patients")
    def search():
        """Search box. A scanned/typed Case #, Patient # or NIC jumps straight to the record."""
        q = request.args.get("q", "").strip()
        results, message = [], None
        if q:
            db = get_db()
            digits = re.sub(r"\D", "", q)
            try:
                reg_no, visit_no = parse_case_no(q)
            except ValueError:
                reg_no = None
            if reg_no:
                case = reg.get_case_by_number(db, reg_no, visit_no)
                if case:
                    return redirect(url_for("case_detail", case_id=case["id"]))
                message = f"No case {q}"
            elif _NUMERIC_INPUT.fullmatch(q) and len(digits) == 9:
                try:
                    patient = reg.get_patient_by_reg_no(db, q)
                    if patient:
                        return redirect(url_for("patient_detail", reg_no=patient["reg_no"]))
                    message = f"No patient with Patient # {format_reg_no(digits)}"
                except ValueError:
                    message = f"{q} is not a valid Patient # (check digit failed)"
            elif _NUMERIC_INPUT.fullmatch(q) and len(digits) == 13:
                patient = reg.get_patient_by_nic(db, q)
                if patient:
                    return redirect(url_for("patient_detail", reg_no=patient["reg_no"]))
                message = f"No patient with NIC {format_nic(digits)}"
            else:
                results = reg.search_patients(db, q)
                if not results:
                    message = f"No patients match “{q}”"
        return render_template("search.html", q=q, results=results, message=message)

    @app.get("/pending")
    def pending():
        cases = lab_tests.list_pending_cases(get_db())
        return render_template(
            "pending.html",
            awaiting=[c for c in cases if c["status"] in lab_tests.EDITABLE_STATUSES],
            to_print=[c for c in cases if c["status"] == "verified"],
        )

    @app.route("/patients/new", methods=["GET", "POST"])
    def new_patient():
        db = get_db()
        form, errors, duplicate = request.form, {}, None
        if request.method == "POST":
            data, errors = _parse_patient_form(form)
            case_fields, test_ids, visit_errors = _parse_visit_form(form)
            errors.update(visit_errors)
            if not errors:
                try:
                    patient, case = reg.register_patient(
                        db, case_fields=case_fields, test_ids=test_ids, **data)
                except reg.DuplicateNICError:
                    duplicate = reg.get_patient_by_nic(db, data["nic"])
                    errors["nic"] = "Patient already registered with this NIC"
                else:
                    flash(f"Registered {patient['name']} — Patient # "
                          f"{format_reg_no(patient['reg_no'])}, Case # "
                          f"{format_case_no(patient['reg_no'], case['visit_no'])}")
                    return redirect(url_for("case_detail", case_id=case["id"]))
        return render_template(
            "patient_form.html", form=form, errors=errors, duplicate=duplicate,
            sections=_tests_by_section(db), suggestions=reg.case_field_suggestions(db),
            choices=_case_choices(db),
            selected={int(v) for v in form.getlist("test_id") if v.isdecimal()},
        ), (400 if errors else 200)

    def _patient_or_404(reg_no):
        try:
            patient = reg.get_patient_by_reg_no(get_db(), reg_no)
        except ValueError:
            patient = None
        if patient is None:
            abort(404)
        return patient

    def _render_patient(patient, form=None, errors=None, status=200):
        db = get_db()
        cases = reg.list_cases(db, patient["id"])
        form = form if form is not None else {}
        return render_template(
            "patient_detail.html", patient=patient, cases=cases,
            next_visit=(cases[0]["visit_no"] + 1) if cases else 1,
            sections=_tests_by_section(db), form=form, errors=errors or {},
            suggestions=reg.case_field_suggestions(db), choices=_case_choices(db),
            selected={int(v) for v in getattr(form, "getlist", lambda _: [])("test_id")
                      if v.isdecimal()},
        ), status

    @app.get("/patients/<reg_no>")
    def patient_detail(reg_no):
        return _render_patient(_patient_or_404(reg_no))

    @app.post("/patients/<reg_no>/cases")
    def new_case(reg_no):
        patient = _patient_or_404(reg_no)
        case_fields, test_ids, errors = _parse_visit_form(request.form)
        if errors:
            return _render_patient(patient, request.form, errors, 400)
        case = reg.create_case(get_db(), patient["id"], test_ids=test_ids, **case_fields)
        flash(f"Case {format_case_no(patient['reg_no'], case['visit_no'])} created")
        return redirect(url_for("case_detail", case_id=case["id"]))

    def _render_case(case_id, errors=None, status=200):
        db = get_db()
        case = reg.get_case(db, case_id)
        if case is None:
            abort(404)
        return render_template(
            "case_detail.html", case=case, patient=reg.get_patient(db, case["patient_id"]),
            ordered=lab_tests.get_ordered_tests(db, case_id), errors=errors or {},
            form=request.form, editable=case["status"] in lab_tests.EDITABLE_STATUSES,
            missing=lab_tests.missing_results(db, case_id),
            printable=case["status"] in reports.PRINTABLE_STATUSES,
            prints=reports.list_prints(db, case_id),
            other_printers=[n for n in reports.previous_printers(db)
                            if n.casefold() not in {p["name"].casefold()
                                                    for p in settings.PRINTED_BY_CHOICES}],
        ), status

    @app.get("/cases/<int:case_id>")
    def case_detail(case_id):
        return _render_case(case_id)

    def _results_from_form(db, case_id, form):
        """Read method / value / flag fields for the case's current tests.

        Only fields present in the form are used, so a form without result
        fields never clears saved results.
        """
        methods, values = {}, {}
        for ot in lab_tests.get_ordered_tests(db, case_id):
            ct_id = ot.case_test["id"]
            if f"method-{ct_id}" in form:
                methods[ct_id] = form[f"method-{ct_id}"]
            entries = {}
            for p in ot.parameters:
                key = f"value-{ct_id}-{p.row['id']}"
                if key in form:
                    entries[p.row["id"]] = (form[key], form.get(f"flag-{ct_id}-{p.row['id']}", "auto"))
            if entries:
                values[ct_id] = entries
        return methods, values

    @app.post("/cases/<int:case_id>/results")
    def save_results(case_id):
        db = get_db()
        if reg.get_case(db, case_id) is None:
            abort(404)
        methods, values = _results_from_form(db, case_id, request.form)
        try:
            lab_tests.save_results(db, case_id, methods, values)
        except lab_tests.ResultValidationError as err:
            return _render_case(case_id, err.errors, 400)
        except lab_tests.CaseLockedError as err:
            flash(f"Results not saved: {err}. Reopen the case to make corrections.", "error")
            return redirect(url_for("case_detail", case_id=case_id))
        except ValueError:
            abort(400)
        flash("Results saved")
        return redirect(url_for("case_detail", case_id=case_id))

    @app.post("/cases/<int:case_id>/tests/<int:case_test_id>/remove")
    def remove_test(case_id, case_test_id):
        """Remove a test. The Remove button submits the whole results form, so values
        typed (but not yet saved) for the other tests are saved instead of lost."""
        db = get_db()
        try:
            name = lab_tests.remove_test(db, case_id, case_test_id)
        except LookupError:
            abort(404)
        except (ValueError, lab_tests.CaseLockedError) as err:
            flash(f"Cannot remove test: {err}", "error")
            return _render_case(case_id, status=400)  # re-show the typed values, unsaved
        methods, values = _results_from_form(db, case_id, request.form)
        if methods or values:
            try:
                lab_tests.save_results(db, case_id, methods, values)
            except lab_tests.ResultValidationError as err:
                flash(f"{name} removed. Some other results need correcting before they can be saved.",
                      "error")
                return _render_case(case_id, err.errors, 400)
            flash(f"{name} removed from this case. Your other entries were saved.")
        else:
            flash(f"{name} removed from this case")
        return redirect(url_for("case_detail", case_id=case_id))

    @app.post("/cases/<int:case_id>/verify")
    def verify_case(case_id):
        try:
            lab_tests.verify_case(get_db(), case_id)
            flash("Case verified — results are locked and the report can be printed")
        except LookupError:
            abort(404)
        except (ValueError, lab_tests.CaseLockedError) as err:
            flash(f"Cannot verify: {err}", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    @app.post("/cases/<int:case_id>/reopen")
    def reopen_case(case_id):
        try:
            lab_tests.reopen_case(get_db(), case_id)
            flash("Case reopened — correct the results and verify again")
        except LookupError:
            abort(404)
        except lab_tests.CaseLockedError as err:
            flash(f"Cannot reopen: {err}", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    def _report_context(case_id) -> dict:
        try:
            return reports.build_report(get_db(), case_id)
        except LookupError:
            abort(404)

    @app.get("/cases/<int:case_id>/report")
    def report_preview(case_id):
        """On-screen preview. Not recorded as a print. Auto-locks like the other pages."""
        return render_template("report.html", idle_lock=True, **_report_context(case_id))

    def _render_pdf(case_id, printer: dict) -> bytes:
        ctx = _report_context(case_id)
        lines = [printer["name"]] + ([printer["title"]] if printer["title"] else [])
        return reports.html_to_pdf(render_template("report.html", **ctx),
                                   signature=settings.SIGNATURE, printed_by=lines)

    def _printer_from_form(form) -> dict | None:
        """Chosen preset (name + title), or a typed name via "Other" (no title)."""
        name = form.get("printed_by", "")
        if name == OTHER:
            name = form.get("printed_by_other", "")
        name = " ".join(name.split())[:60]
        if not name:
            return None
        presets = {p["name"].casefold(): p for p in settings.PRINTED_BY_CHOICES}
        return presets.get(name.casefold(), {"name": name, "title": ""})

    @app.post("/cases/<int:case_id>/print")
    def print_report(case_id):
        db = get_db()
        printer = _printer_from_form(request.form)
        if printer is None:
            flash("Choose who is printing the report (Printed by), or type a name under Other.",
                  "error")
            return redirect(url_for("case_detail", case_id=case_id))
        try:
            row = reports.print_case(
                db, case_id, current_app.config["REPORTS_DIR"],
                render_pdf=lambda: _render_pdf(case_id, printer),
                printed_by=printer["name"],
            )
        except LookupError:
            abort(404)
        except reports.CaseNotVerifiedError as err:
            flash(f"Cannot print: {err}", "error")
            return redirect(url_for("case_detail", case_id=case_id))
        return redirect(url_for("print_pdf", print_id=row["id"]))

    @app.get("/prints/<int:print_id>.pdf")
    def print_pdf(print_id):
        row = reports.get_print(get_db(), print_id)
        if row is None:
            abort(404)
        path = Path(current_app.config["REPORTS_DIR"]) / row["pdf_path"]
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="application/pdf",
                         download_name=Path(row["pdf_path"]).name)
