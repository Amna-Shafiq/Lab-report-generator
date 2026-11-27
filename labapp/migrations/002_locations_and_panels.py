"""v2: case location/reference fields, optional parameters, lab wording, Urine RE + Stool RE."""

from labapp.catalog import add_test, load_definition

# code -> (new name, new section, display order); single tests' parameter is renamed too.
RENAMES = {
    "HBSAG":       ("HBsAg Screening",        "Viral Hepatitis", 10),
    "ANTI_HCV":    ("Anti HCV Screening",     "Viral Hepatitis", 20),
    "ANTI_HIV":    ("Anti HIV",               "Serology",        30),
    "HPYLORI_AB":  ("H. Pylori Ab",           "Serology",        40),
    "STOOL_HP_AG": ("Stool for H. Pylori Ag", "Specimen: Stool", 50),
    "STOOL_OB":    ("Stool for Occult Blood", "Specimen: Stool", 60),
}


def upgrade(conn):
    # 1. Case fields that were removed earlier and are wanted again.
    conn.execute("ALTER TABLE cases ADD COLUMN registration_location TEXT")
    conn.execute("ALTER TABLE cases ADD COLUMN destination_location TEXT")
    conn.execute("ALTER TABLE cases ADD COLUMN reference TEXT")

    # 2. Optional parameters (may be left blank; printed as "-").
    conn.execute("ALTER TABLE test_parameters ADD COLUMN is_required INTEGER NOT NULL "
                 "DEFAULT 1 CHECK (is_required IN (0, 1))")

    # 3. Match the lab's printed wording.
    for code, (name, section, order) in RENAMES.items():
        conn.execute("UPDATE tests SET name = ?, section = ?, display_order = ? WHERE code = ?",
                     (name, section, order, code))
        conn.execute("UPDATE test_parameters SET name = ? WHERE code = ? AND test_id = "
                     "(SELECT id FROM tests WHERE code = ?)", (name, code, code))

    # Hepatitis / HIV: Non-Reactive / Reactive -> Negative / Positive (options and saved results).
    params = "(SELECT p.id FROM test_parameters p JOIN tests t ON t.id = p.test_id " \
             "WHERE t.code IN ('HBSAG', 'ANTI_HCV', 'ANTI_HIV'))"
    for old, new in (("Non-Reactive", "Negative"), ("Reactive", "Positive")):
        conn.execute(f"UPDATE parameter_options SET value = ? WHERE value = ? AND parameter_id IN {params}",
                     (new, old))
        conn.execute(f"UPDATE results SET value = ? WHERE value = ? AND parameter_id IN {params}",
                     (new, old))
    conn.execute(f"UPDATE test_parameters SET normal_text = 'Negative' WHERE id IN {params}")

    # 4. Multi-parameter panels.
    add_test(conn, load_definition("stool_re.json"))
    add_test(conn, load_definition("urine_re.json"))
