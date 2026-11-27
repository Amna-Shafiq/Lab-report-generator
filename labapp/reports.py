"""Report data, Code 128 barcodes, PDF rendering (Playwright) and print tracking."""

import base64
import hashlib
import io
import os
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from barcode import Code128
from barcode.writer import SVGWriter

from .db import immediate
from .lab_tests import get_ordered_tests
from .numbering import format_case_no

PRINTABLE_STATUSES = ("verified", "printed")


class CaseNotVerifiedError(Exception):
    pass


# ---------- barcodes ----------

def barcode_data_uri(data: str) -> str:
    """Code 128 as an inline SVG data URI (bars only; the caption is printed by the template)."""
    buf = io.BytesIO()
    Code128(data, writer=SVGWriter()).write(buf, options={
        "module_width": 0.25,   # mm per bar unit
        "module_height": 10,    # bar height, mm
        "quiet_zone": 2.5,      # blank margin either side, mm (scanners need it)
        "write_text": False,
    })
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ---------- report data ----------

@dataclass
class ReportRow:
    name: str
    group: str | None
    normal: str
    unit: str
    value: str
    abnormal: bool


@dataclass
class ReportTest:
    name: str
    method: str | None
    rows: list[ReportRow]

    @property
    def single(self) -> bool:
        """Single qualitative test: printed as one row named after the test."""
        return len(self.rows) == 1


@dataclass
class ReportSection:
    name: str
    tests: list[ReportTest] = field(default_factory=list)


def build_report(conn: sqlite3.Connection, case_id: int) -> dict:
    """Everything report.html needs. Only tests ordered on this case are included."""
    case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        raise LookupError(f"No case {case_id}")
    patient = conn.execute("SELECT * FROM patients WHERE id = ?", (case["patient_id"],)).fetchone()

    sections: dict[str, ReportSection] = {}
    for ot in get_ordered_tests(conn, case_id):
        rows = [
            ReportRow(
                name=p.row["name"], group=p.row["group_name"],
                normal=p.row["normal_text"] or "", unit=p.row["unit"] or "",
                value=p.result["value"] if p.result else "-",  # blank optional -> "-"
                abnormal=bool(p.result and p.result["is_abnormal"]),
            )
            for p in ot.parameters
        ]
        section = sections.setdefault(ot.case_test["section"],
                                      ReportSection(ot.case_test["section"]))
        section.tests.append(ReportTest(ot.case_test["test_name"], ot.case_test["method"], rows))

    case_no = format_case_no(patient["reg_no"], case["visit_no"])
    return {
        "case": case,
        "patient": patient,
        "case_no": case_no,
        "sections": list(sections.values()),
        "has_ict": any(t.method == "ICT" for s in sections.values() for t in s.tests),
        "patient_barcode": barcode_data_uri(patient["reg_no"]),
        "case_barcode": barcode_data_uri(case_no),
        "is_final": case["status"] in PRINTABLE_STATUSES,
    }


# ---------- PDF ----------

def _footer_block(lines: list[str] | None, align: str) -> str:
    """A signature-style block: first line bold, the rest smaller."""
    # Chromium draws footers at a reduced scale, so these px sizes look ~25% smaller on paper.
    indent = "padding-left:4mm" if align == "left" else "padding-right:0"
    return (f'<div style="text-align:{align}">' + "".join(
        f'<div style="font-size:{"16px; font-weight:700" if i == 0 else f"12px; font-weight:600; {indent}"}">'
        f'{escape(line)}</div>'
        for i, line in enumerate(lines or [])
    ) + "</div>")


def html_to_pdf(html: str, footer_text: str = "", signature: list[str] | None = None,
                printed_by: list[str] | None = None) -> bytes:
    """Render HTML to an A4 PDF with headless Chromium. Page margins come from CSS @page.

    Printed at the bottom of EVERY page: the doctor's signature block on the left and
    the "printed by" block (e.g. name + "Lab Technician") on the right, then
    footer_text (e.g. patient name and Case #) with "Page X of Y". The footer sits in
    the bottom page margin, so report.html reserves enough space there (@page).
    """
    from playwright.sync_api import sync_playwright  # imported lazily: slow, and only needed here

    footer = (
        '<div style="width:100%; padding:0 14mm; font-family:Segoe UI, Arial, sans-serif; color:#000;">'
        '<div style="display:flex; justify-content:space-between; align-items:flex-start;">'
        f'{_footer_block(signature, "left")}{_footer_block(printed_by, "right")}</div>'
        '<div style="font-size:8px; display:flex; justify-content:space-between; margin-top:3mm;">'
        f'<span>{escape(footer_text)}</span>'
        '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div></div>'
    )
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True,
                            display_header_footer=True, header_template="<span></span>",
                            footer_template=footer)
        finally:
            browser.close()


def _launch_browser(pw):
    """Use a browser already on the PC, so the packaged app needs no bundled Chromium.

    Order: Microsoft Edge (preinstalled on Windows 10/11), Google Chrome, then
    Playwright's own Chromium (present on developer machines).
    """
    errors = []
    for channel in ("msedge", "chrome", None):
        try:
            return pw.chromium.launch(channel=channel) if channel else pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - try the next browser
            errors.append(f"{channel or 'chromium'}: {str(e).splitlines()[0]}")
    raise RuntimeError("No browser available to create the PDF. Install Microsoft Edge or "
                       "Google Chrome.\n" + "\n".join(errors))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_matches(path: Path, sha256: str) -> bool:
    try:
        return _sha256(path.read_bytes()) == sha256
    except OSError:
        return False


def print_case(conn: sqlite3.Connection, case_id: int, reports_dir: Path,
               render_pdf: Callable[[], bytes], printed_by: str | None = None) -> sqlite3.Row:
    """Record a print and return the case_prints row.

    The first print after a verification renders and saves the PDF. Printing
    again for the same verification is a reprint: the same saved file when the
    same person prints it, a new file (with their name) when someone else does.
    After reopen + re-verify, a new PDF is generated (corrected report).
    """
    case = conn.execute(
        "SELECT c.*, p.reg_no FROM cases c JOIN patients p ON p.id = c.patient_id WHERE c.id = ?",
        (case_id,),
    ).fetchone()
    if case is None:
        raise LookupError(f"No case {case_id}")
    if case["status"] not in PRINTABLE_STATUSES:
        raise CaseNotVerifiedError("Verify the case before printing")

    version = case["verify_count"]
    first = conn.execute(
        "SELECT * FROM case_prints WHERE case_id = ? AND verification_no = ? ORDER BY id LIMIT 1",
        (case_id, version),
    ).fetchone()
    # The printer's name is on the PDF, so reuse a saved file only if the same person printed it.
    same_printer = conn.execute(
        "SELECT * FROM case_prints WHERE case_id = ? AND verification_no = ? AND printed_by IS ? "
        "ORDER BY id LIMIT 1",
        (case_id, version, printed_by),
    ).fetchone()

    if same_printer and _file_matches(reports_dir / same_printer["pdf_path"], same_printer["pdf_sha256"]):
        rel_path, sha = same_printer["pdf_path"], same_printer["pdf_sha256"]
    else:
        # Rendering takes ~1 s, so do it before taking the write lock.
        pdf = render_pdf()
        rel_path = (f"{case['reg_no']}/{format_case_no(case['reg_no'], case['visit_no'])}"
                    f"_v{version}_{secrets.token_hex(3)}.pdf")
        target = reports_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(pdf)
        os.replace(tmp, target)  # atomic: never leaves a half-written PDF
        sha = _sha256(pdf)

    with immediate(conn):
        current = conn.execute("SELECT status, verify_count FROM cases WHERE id = ?",
                               (case_id,)).fetchone()
        if current["status"] not in PRINTABLE_STATUSES or current["verify_count"] != version:
            raise CaseNotVerifiedError("The case was reopened while printing; try again")
        row = conn.execute(
            """
            INSERT INTO case_prints (case_id, verification_no, pdf_path, pdf_sha256,
                                     is_reprint, printed_by)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING *
            """,
            (case_id, version, rel_path, sha, int(first is not None), printed_by),
        ).fetchall()[0]
        conn.execute("UPDATE cases SET status = 'printed' WHERE id = ? AND status = 'verified'",
                     (case_id,))
    return row


def previous_printers(conn: sqlite3.Connection, limit: int = 50) -> list[str]:
    """Names that have printed reports before, most frequent first (for the dropdown)."""
    return [r[0] for r in conn.execute(
        "SELECT printed_by FROM case_prints WHERE printed_by IS NOT NULL "
        "GROUP BY printed_by ORDER BY count(*) DESC, printed_by LIMIT ?", (limit,))]


def list_prints(conn: sqlite3.Connection, case_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM case_prints WHERE case_id = ? ORDER BY id DESC", (case_id,)
    ).fetchall()


def get_print(conn: sqlite3.Connection, print_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM case_prints WHERE id = ?", (print_id,)).fetchone()
