# Lab Report Software

## Goal

Offline desktop-style app for a clinical laboratory. Registers patients, creates cases (visits), enters test results, and prints PDF reports with barcodes. All data is stored locally. Cloud sync may come later, so keep the design portable.

## Working rules (important)

- I am a software engineer. Claude writes the code; I review it and ask for changes.
- For each step: explain the plan and any tradeoffs, write the code with tests, run the tests, and summarize what changed.
- Offer 2 options with tradeoffs for any major decision and let me choose.
- Ask before adding fields or features that are not in this spec.
- Make requested changes in the local source only. Rebuild the executable (build.ps1 / dist\) only when I explicitly ask.

## Stack

- Python 3.13, local web server with a browser UI.
- SQLite via the stdlib `sqlite3` module. Connect with `autocommit=True` and manage transactions explicitly. Every connection sets `PRAGMA foreign_keys = ON` and `busy_timeout`; the database uses WAL mode.
- Schema versions: v1 = `labapp/schema.sql` + `labapp/seed_tests.sql` (FROZEN — never edit). Every later change is a numbered file in `labapp/migrations/` (`NNN_name.sql` or `.py` with `upgrade(conn)`), applied at startup by `init_db` in its own transaction; existing databases are backed up to `lab.db.vN.bak` first.
- Test panels are defined as JSON in `labapp/test_definitions/` and inserted by `catalog.add_test` from a migration.
- Primary keys are INTEGER; every table has `updated_at` (kept current by triggers). Schema version is tracked in `PRAGMA user_version`.
- Web UI: Flask 3.1 with server-rendered Jinja2 templates (`labapp/app.py`, `labapp/templates/`), bound to 127.0.0.1:5000 only. Dependencies in `requirements.txt`, installed in `.venv`.
- Run the app: `.venv\Scripts\python run.py` (add `--debug` for auto-reload). Database: `PHARMASOFT_DB` env var, default `data\lab.db`.
- Run tests: `.venv\Scripts\python -m unittest -v`.
- Windows package: `powershell -ExecutionPolicy Bypass -File build.ps1` runs the tests, builds with PyInstaller (`pharmasoft.spec`, folder build, console window) and writes `dist\DrShafiqAqilClinic-<date>.zip` (exe `DrShafiqAqilClinic.exe`; software name is `settings.APP_NAME` = "Dr Shafiq Aqil Clinic"; the report has no lab name/address header, only barcodes) including `packaging\README-client.txt`. No browser is bundled: PDFs use the PC's Microsoft Edge (then Chrome, then Playwright's Chromium). Packaged data lives in `data\` next to the .exe. Env vars: `PHARMASOFT_DB`, `PHARMASOFT_PORT`, `PHARMASOFT_NO_BROWSER=1`.
- PDF: Playwright (headless Chromium) renders `labapp/templates/report.html` to A4 PDF. Barcodes: python-barcode Code 128 as inline SVG. Lab name, address and ICT footnote text live in `labapp/settings.py`.
- Report pagination: each test is its own `.test-block` (`break-inside: avoid`), so a test is never split across pages — if it doesn't fit, the whole test moves to the next page. A section heading travels with its section's first test; all test tables share fixed column widths.
- Saved PDFs go to a `reports\` folder next to the database (back it up together with the database).
- Workflow: results editable while `registered`/`in_progress` → Verify (all results required, locks results) → Print (status `printed`). Reopen unlocks for correction and requires re-verify. Each verify increments `cases.verify_count`; the first print of a verification saves the PDF, later prints of the same verification are reprints of that same file. Every print is recorded in `case_prints`.

## Requirements

- Storage: SQLite (single local file), UNIQUE constraints, foreign keys on, transactions for number generation.
- Patient # (registration no.): random 8 digits + 1 Luhn check digit (9 digits), unique for life, shown as a Code 128 barcode. Stored as TEXT without separators; displayed as `XXXX-XXXX-C`. Generated with `secrets`, inserted with retry on a UNIQUE violation of reg_no. Returning patients keep the same Patient #.
- Case # (visit no.): per-patient visit number. Case # = `<Patient #>-<visit>`, e.g. `482719365-2`. Visit 1 is created automatically when the patient is registered; each new visit is previous + 1, generated inside a `BEGIN IMMEDIATE` transaction. UNIQUE(patient_id, visit_no). Cases are cancelled, never deleted, so numbers are never reused.
- Patient fields: name, father/husband name, DOB (age is computed, never stored), sex, blood group, NIC, phone, address.
- NIC: normalized to 13 digits before storing, nullable, UNIQUE. Not used as the primary key.
- Case fields: registration date/time, registration location, destination location, reference, consultant, status. When creating a case staff enter the four text fields (all optional; each suggests previously used values) and tick the tests — on the registration form for case 1, on the patient page for later visits. Blank reference/consultant print as SELF.
- Optional parameters (`is_required = 0`, e.g. Spot Urinary Protein, Frank Blood) may be left blank, are not needed for verification, and print as "-".
- ICT note (lab's wording, `settings.ICT_NOTE`) prints under every test whose method is ICT.
- Signature block `settings.SIGNATURE` (Dr. M. Shafiq Aqil — no "Prof.") is printed in the PDF page footer, bottom-left of every page; the "Printed by" person (name + title, chosen from `settings.PRINTED_BY_CHOICES`, e.g. Mehtab ur Rehman / Lab Technician) is bottom-right; only "Page X of Y" below (no patient name / Case # in the footer). "Printed by" also has "Other (type name)…": a typed name prints without a title and is offered in the dropdown afterwards. The on-screen preview shows only the doctor's block, once at the end. Printing requires choosing "Printed by"; it is stored in `case_prints.printed_by`. A saved PDF is reused for a reprint only by the same printer.
- Login: one shared password (no usernames), created on first start together with a security question; "Forgot password?" asks that question. Hashes stored in `app_settings` (migration 003). Auto-lock after `settings.AUTO_LOCK_MINUTES` (15) idle minutes, enforced on the server and by a page script; restarting the app logs everyone out. No last-resort reset: if password and answer are both lost, a developer deletes the `app_password_hash` row.
- Removing a test submits the whole results form: values typed for the other tests are saved, not lost.
- Case form: registration/destination location and consultant are dropdowns with "Other (type below)…". Options = `settings.LOCATION_CHOICES` / `CONSULTANT_CHOICES` plus values used before. Both locations default to `settings.DEFAULT_LOCATION` (CLINIC DR SHAFIQ AQIL); consultant defaults to SELF.
- Abnormal flag: set automatically from the test catalog (dropdown option marked abnormal, or number outside normal range); staff can override per result.
- Tests are data-driven: test -> parameters. Each parameter has name, group, input type (dropdown/number/text), options, unit, normal value text, display order. Adding a test must not need code changes.
- Tests to support: HBsAg, Anti HCV, Anti HIV, H. Pylori Ab, Stool H. Pylori Ag, Stool Occult Blood (single qualitative), plus Stool RE and Urine RE (multi-parameter panels with Physical / Chemical / Microscopic groups).
- Results: value, abnormal flag, method (e.g. ICT/ELISA), entered_by, verified_by, timestamp. A case must be verified before printing. Track reprints.
- Report: HTML template exported to PDF. Header with Patient # and Case # barcodes, patient block, sections grouped by test type, columns TEST / NORMAL VALUE / UNIT / RESULT. Abnormal results in bold. The ICT method footnote shows only when the method is ICT. Only print sections for tests ordered on that case.
- Security: simple login, audit log of who entered, edited, verified and printed. HIV and hepatitis results are sensitive.
- Barcode scanner (USB, keyboard-style) must work in the patient search box.
- Home page (`/`, "worklist first"): scanner-focused search box, "+ New patient" with today's counts (cases today / awaiting results / ready to print), and two side-by-side worklists — Awaiting results (progress, "Ready to verify" when complete) and Ready to print — up to 10 rows each with "See all" to /pending. Login lands here. Top menu: Home / Pending cases / New patient (current page underlined).
- UI for non-technical staff: statuses always shown in plain words (`STATUS_LABELS` in app.py: "Waiting for results", "Results being entered", "Verified – ready to print", "Printed"). Case page shows a 1 Enter results → 2 Verify → 3 Print step bar with a "what to do next" box; Verify appears only when all required results are entered; Save results bar is sticky at the bottom; Reopen asks for confirmation. Registration form is split into "1 Patient details" and "2 First visit".
- Pending cases page (`/pending`): cases awaiting results (with entered/total progress) and verified cases not yet printed, oldest first.

## Build order

1. Schema and numbering (patients, cases)
2. HBsAg end to end, including PDF
3. Remaining single qualitative tests (data only)
4. Urine RE, then Stool RE
5. Login, audit log, backup

## Open decisions

