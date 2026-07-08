"""LOAD step: build the star schema in SQLite and fill it.

The table definitions live in sql/schema.sql — SQL stays in SQL files,
Python stays in Python files.

INSERT statements are not written by hand: they are generated from each
record's own fields (see _insert). Column names in schema.sql match the
dataclass field names in transform.py, so adding a new column means touching
exactly two places — the schema file and the dataclass — and every INSERT
adapts by itself.

Design notes:
* Dimensions get their own integer keys ("surrogate keys"); original FHIR
  ids are kept in UNIQUE columns, so the warehouse never depends on how the
  source system formats its ids.
* An allergy whose patient is missing from the export links to the seeded
  'Unknown' patient row (key -1, see schema.sql) instead of being dropped —
  dropping it would silently make allergy counts wrong.
* Every run rebuilds all tables inside one transaction: running the pipeline
  twice gives exactly the same database (idempotency), and a failed run
  leaves nothing half-loaded.
"""

import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path

logger = logging.getLogger(__name__)

UNKNOWN_KEY = -1  # key of the seeded 'Unknown' rows (see sql/schema.sql)
SCHEMA_FILE = Path(__file__).parent / "sql" / "schema.sql"


def load_warehouse(db_path: Path, patients: list, allergies: list, issues: list) -> int:
    """(Re)build the warehouse. Returns how many facts had an unknown patient."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:  # one transaction: all-or-nothing
            connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))

            # Dimensions first (facts need their keys).
            patient_keys = {
                p.patient_id: _insert(connection, "dim_patient", asdict(p))
                for p in patients
            }
            allergen_keys = _load_allergens(connection, allergies)
            category_keys = _load_lookup(connection, "dim_category", "category",
                                         {a.category for a in allergies})
            criticality_keys = _load_lookup(connection, "dim_criticality", "criticality",
                                            {a.criticality for a in allergies})

            orphans = _load_facts(connection, allergies, patient_keys,
                                  allergen_keys, category_keys, criticality_keys)

            for issue in issues:
                _insert(connection, "data_quality_issue", asdict(issue))
    finally:
        connection.close()
    return orphans


def _insert(connection, table: str, values: dict) -> int:
    """Generate and run an INSERT from a dict of {column: value}.

    Example: {"code": "x", "code_display": "y"} becomes
        INSERT INTO <table> (code, code_display) VALUES (:code, :code_display)

    Table and column names come from our own code (dataclasses, schema.sql),
    never from the input data; the *values* always go through SQL parameters,
    which is what protects against injection.
    Returns the new row's auto-generated key.
    """
    columns = ", ".join(values)
    placeholders = ", ".join(":" + column for column in values)
    cursor = connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)
    return cursor.lastrowid


def _load_allergens(connection, allergies) -> dict:
    """Insert each distinct allergen code once (many facts share one allergen)."""
    keys = {}
    for a in allergies:
        if a.code is None or (a.code_system, a.code) in keys:
            continue
        keys[(a.code_system, a.code)] = _insert(connection, "dim_allergen", {
            "code_system": a.code_system,
            "code": a.code,
            "code_display": a.code_display,
        })
    return keys


def _load_lookup(connection, table: str, column: str, values: set) -> dict:
    """Fill a small lookup dimension (category, criticality) with its values."""
    return {
        value: _insert(connection, table, {column: value})
        for value in sorted(values)
    }


def _load_facts(connection, allergies, patient_keys, allergen_keys,
                category_keys, criticality_keys) -> int:
    """Insert one fact row per allergy, translating ids into dimension keys."""
    orphans = 0
    for a in allergies:
        patient_key = patient_keys.get(a.patient_id, UNKNOWN_KEY)
        if patient_key == UNKNOWN_KEY:
            orphans += 1
            logger.warning("Allergy %s: patient %r not found -> Unknown patient",
                           a.allergy_id, a.patient_id)
        _insert(connection, "fact_allergy_intolerance", {
            "allergy_id": a.allergy_id,
            "patient_key": patient_key,
            "allergen_key": allergen_keys.get((a.code_system, a.code), UNKNOWN_KEY),
            "category_key": category_keys[a.category],
            "criticality_key": criticality_keys[a.criticality],
            "allergy_type": a.allergy_type,
            "recorded_at": a.recorded_at,
            "recorded_date": a.recorded_date,
        })
    return orphans
