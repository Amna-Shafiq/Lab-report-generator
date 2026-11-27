-- Lab Report Software: schema version 1.
-- Applied by db.init_db() when PRAGMA user_version = 0, together with seed_tests.sql,
-- inside one transaction (init_db adds BEGIN / user_version / COMMIT).
-- Timestamps are UTC ISO-8601: 'YYYY-MM-DDTHH:MM:SSZ'.

-- ===================== Patients and cases =====================

CREATE TABLE patients (
    id                INTEGER PRIMARY KEY,
    -- 8 random digits + 1 Luhn check digit, stored without separators.
    reg_no            TEXT    NOT NULL UNIQUE
                      CHECK (length(reg_no) = 9 AND reg_no NOT GLOB '*[^0-9]*'),
    name              TEXT    NOT NULL CHECK (trim(name) <> ''),
    guardian_name     TEXT,   -- father / husband name
    -- Age is computed from dob, never stored. If only age is known, the app
    -- derives an approximate dob and sets dob_is_estimated = 1.
    dob               TEXT    NOT NULL CHECK (dob = date(dob)),
    dob_is_estimated  INTEGER NOT NULL DEFAULT 0 CHECK (dob_is_estimated IN (0, 1)),
    sex               TEXT    NOT NULL CHECK (sex IN ('M', 'F', 'O')),
    blood_group       TEXT    CHECK (blood_group IN ('A+','A-','B+','B-','AB+','AB-','O+','O-')),
    -- 13 digits, no dashes. NULL allowed (children, walk-ins); UNIQUE ignores NULLs.
    nic               TEXT    UNIQUE
                      CHECK (length(nic) = 13 AND nic NOT GLOB '*[^0-9]*'),
    phone             TEXT,
    address           TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_patients_name  ON patients (name COLLATE NOCASE);
CREATE INDEX idx_patients_phone ON patients (phone);

-- One row per visit. Case # = <reg_no>-<visit_no>, e.g. 482719365-2.
-- Cases are never deleted (cancel instead), so visit numbers are never reused.
CREATE TABLE cases (
    id            INTEGER PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients (id) ON DELETE RESTRICT,
    visit_no      INTEGER NOT NULL CHECK (visit_no >= 1),
    registered_at TEXT    NOT NULL,
    consultant    TEXT,
    status        TEXT    NOT NULL DEFAULT 'registered'
                  CHECK (status IN ('registered', 'in_progress', 'verified',
                                    'printed', 'cancelled')),
    verified_at   TEXT,                             -- set by verify, cleared by reopen
    verified_by   TEXT,                             -- user id once login exists (step 5)
    verify_count  INTEGER NOT NULL DEFAULT 0,       -- +1 on every verify; identifies the version
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (patient_id, visit_no),
    CHECK ((status IN ('verified', 'printed')) = (verified_at IS NOT NULL))
);

CREATE INDEX idx_cases_status ON cases (status);

-- ===================== Test catalog (data-driven) =====================

CREATE TABLE tests (
    id            INTEGER PRIMARY KEY,
    code          TEXT    NOT NULL UNIQUE,          -- stable key, e.g. 'HBSAG'
    name          TEXT    NOT NULL,                 -- shown on screen / report
    section       TEXT    NOT NULL,                 -- report section, e.g. 'Serology'
    methods       TEXT,                             -- allowed methods, comma-separated; first = default
    is_sensitive  INTEGER NOT NULL DEFAULT 0 CHECK (is_sensitive IN (0, 1)),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    display_order INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE test_parameters (
    id            INTEGER PRIMARY KEY,
    test_id       INTEGER NOT NULL REFERENCES tests (id) ON DELETE CASCADE,
    code          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    group_name    TEXT,                             -- Physical / Chemical / Microscopic
    input_type    TEXT    NOT NULL CHECK (input_type IN ('dropdown', 'number', 'text')),
    unit          TEXT,
    normal_text   TEXT,                             -- printed in NORMAL VALUE column
    normal_min    REAL,                             -- number inputs: auto abnormal outside range
    normal_max    REAL,
    display_order INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (test_id, code)
);

CREATE TABLE parameter_options (
    id            INTEGER PRIMARY KEY,
    parameter_id  INTEGER NOT NULL REFERENCES test_parameters (id) ON DELETE CASCADE,
    value         TEXT    NOT NULL,
    is_abnormal   INTEGER NOT NULL DEFAULT 0 CHECK (is_abnormal IN (0, 1)),
    display_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (parameter_id, value)
);

-- ===================== Orders and results =====================

-- A test ordered on a case. Method (ICT/ELISA) applies to the whole test.
CREATE TABLE case_tests (
    id          INTEGER PRIMARY KEY,
    case_id     INTEGER NOT NULL REFERENCES cases (id) ON DELETE RESTRICT,
    test_id     INTEGER NOT NULL REFERENCES tests (id) ON DELETE RESTRICT,
    method      TEXT,
    ordered_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (case_id, test_id)
);

CREATE TABLE results (
    id              INTEGER PRIMARY KEY,
    case_test_id    INTEGER NOT NULL REFERENCES case_tests (id) ON DELETE CASCADE,
    parameter_id    INTEGER NOT NULL REFERENCES test_parameters (id) ON DELETE RESTRICT,
    value           TEXT    NOT NULL,
    is_abnormal     INTEGER NOT NULL CHECK (is_abnormal IN (0, 1)),
    abnormal_manual INTEGER NOT NULL DEFAULT 0 CHECK (abnormal_manual IN (0, 1)),  -- 1 = staff override
    entered_by      TEXT,                           -- user id once login exists (step 5)
    entered_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (case_test_id, parameter_id)
);

-- Every print of a report. The PDF file is saved once per verification;
-- printing again for the same verification is a reprint of the same file.
CREATE TABLE case_prints (
    id          INTEGER PRIMARY KEY,
    case_id     INTEGER NOT NULL REFERENCES cases (id) ON DELETE RESTRICT,
    verification_no INTEGER NOT NULL,               -- cases.verify_count this PDF shows
    pdf_path    TEXT    NOT NULL,                   -- relative to the reports folder
    pdf_sha256  TEXT    NOT NULL,
    is_reprint  INTEGER NOT NULL CHECK (is_reprint IN (0, 1)),
    printed_by  TEXT,                               -- user id once login exists (step 5)
    printed_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_case_prints_case ON case_prints (case_id);

-- ===================== updated_at triggers =====================

CREATE TRIGGER trg_patients_touch AFTER UPDATE ON patients
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE patients SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id;
END;

CREATE TRIGGER trg_cases_touch AFTER UPDATE ON cases
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE cases SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id;
END;

CREATE TRIGGER trg_case_tests_touch AFTER UPDATE ON case_tests
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE case_tests SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id;
END;

CREATE TRIGGER trg_results_touch AFTER UPDATE ON results
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE results SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id;
END;
