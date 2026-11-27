"""Add tests to the catalog from plain data (JSON files in labapp/test_definitions/).

Definition format:
{
  "code": "URINE_RE", "name": "Urine RE", "section": "Urine Examination",
  "methods": null, "is_sensitive": false, "display_order": 70,
  "parameters": [
    {"code": "COLOR", "name": "Color", "group": "Physical", "type": "dropdown",
     "options": ["Pale Yellow", "Red"], "abnormal": ["Red"], "normal": "Pale Yellow"},
    {"code": "SG", "name": "Specific Gravity", "group": "Physical", "type": "number",
     "normal": "1.005 - 1.030", "min": 1.005, "max": 1.030},
    {"code": "MISC", "name": "Misc", "type": "text", "unit": "/HPF", "required": false}
  ]
}
Parameters print in the order listed. "required": false = may be left blank (prints "-").
"""

import json
import sqlite3
from pathlib import Path

CATALOG_DIR = Path(__file__).with_name("test_definitions")
INPUT_TYPES = ("dropdown", "number", "text")


def load_definition(name: str) -> dict:
    return json.loads((CATALOG_DIR / name).read_text(encoding="utf-8"))


def add_test(conn: sqlite3.Connection, spec: dict) -> int:
    """Insert a new test with its parameters and options. Caller owns the transaction."""
    params = spec.get("parameters") or []
    if not params:
        raise ValueError(f"{spec.get('code')}: a test needs at least one parameter")
    codes = [p["code"] for p in params]
    if len(set(codes)) != len(codes):
        raise ValueError(f"{spec['code']}: duplicate parameter codes")

    test_id = conn.execute(
        """
        INSERT INTO tests (code, name, section, methods, is_sensitive, display_order)
        VALUES (?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (spec["code"], spec["name"], spec["section"], spec.get("methods"),
         int(spec.get("is_sensitive", False)), spec.get("display_order", 0)),
    ).fetchall()[0][0]

    for order, p in enumerate(params, start=1):
        kind = p["type"]
        if kind not in INPUT_TYPES:
            raise ValueError(f"{spec['code']}.{p['code']}: unknown type {kind!r}")
        options = p.get("options") or []
        abnormal = set(p.get("abnormal") or [])
        if kind == "dropdown" and not options:
            raise ValueError(f"{spec['code']}.{p['code']}: dropdown needs options")
        if kind != "dropdown" and options:
            raise ValueError(f"{spec['code']}.{p['code']}: only dropdowns have options")
        if not abnormal <= set(options):
            raise ValueError(f"{spec['code']}.{p['code']}: abnormal values not in options: "
                             f"{sorted(abnormal - set(options))}")

        param_id = conn.execute(
            """
            INSERT INTO test_parameters (test_id, code, name, group_name, input_type, unit,
                                         normal_text, normal_min, normal_max, display_order,
                                         is_required)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (test_id, p["code"], p["name"], p.get("group"), kind, p.get("unit"),
             p.get("normal"), p.get("min"), p.get("max"), order * 10,
             int(p.get("required", True))),
        ).fetchall()[0][0]
        conn.executemany(
            "INSERT INTO parameter_options (parameter_id, value, is_abnormal, display_order) "
            "VALUES (?, ?, ?, ?)",
            [(param_id, o, int(o in abnormal), i) for i, o in enumerate(options, start=1)],
        )
    return test_id
