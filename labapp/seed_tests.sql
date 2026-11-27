-- Starter test catalog. Applied once, right after the schema is created.
-- Adding a test = adding rows here (or, later, through an admin screen). No code changes.
--
-- Pattern for a single qualitative test: one row in tests, one parameter, its dropdown options.
-- Option is_abnormal = 1 makes the result abnormal (bold) automatically; staff can override.

INSERT INTO tests (code, name, section, methods, is_sensitive, display_order) VALUES
    ('HBSAG',       'HBsAg',              'Serology',          'ICT,ELISA', 1, 10),
    ('ANTI_HCV',    'Anti HCV',           'Serology',          'ICT,ELISA', 1, 20),
    ('ANTI_HIV',    'Anti HIV',           'Serology',          'ICT,ELISA', 1, 30),
    ('HPYLORI_AB',  'H. Pylori Ab',       'Serology',          'ICT,ELISA', 0, 40),
    ('STOOL_HP_AG', 'Stool H. Pylori Ag', 'Stool Examination', 'ICT',       0, 50),
    ('STOOL_OB',    'Stool Occult Blood', 'Stool Examination', 'ICT',       0, 60);

-- One parameter per single test, named like the test.
INSERT INTO test_parameters (test_id, code, name, input_type, normal_text, display_order)
SELECT id, code, name, 'dropdown',
       CASE WHEN section = 'Serology' AND code <> 'HPYLORI_AB' THEN 'Non-Reactive' ELSE 'Negative' END,
       1
FROM tests;

-- Reactive / Non-Reactive for hepatitis and HIV screening.
INSERT INTO parameter_options (parameter_id, value, is_abnormal, display_order)
SELECT p.id, o.value, o.is_abnormal, o.ord
FROM test_parameters p
JOIN tests t ON t.id = p.test_id
CROSS JOIN (SELECT 'Non-Reactive' AS value, 0 AS is_abnormal, 1 AS ord
            UNION ALL SELECT 'Reactive', 1, 2) o
WHERE t.code IN ('HBSAG', 'ANTI_HCV', 'ANTI_HIV');

-- Positive / Negative for the rest.
INSERT INTO parameter_options (parameter_id, value, is_abnormal, display_order)
SELECT p.id, o.value, o.is_abnormal, o.ord
FROM test_parameters p
JOIN tests t ON t.id = p.test_id
CROSS JOIN (SELECT 'Negative' AS value, 0 AS is_abnormal, 1 AS ord
            UNION ALL SELECT 'Positive', 1, 2) o
WHERE t.code IN ('HPYLORI_AB', 'STOOL_HP_AG', 'STOOL_OB');
