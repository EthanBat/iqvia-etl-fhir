"""Load tests: build a real (temporary) SQLite warehouse and query it,
asserting on what an analyst would actually see."""

import sqlite3

import pytest

from fhir_pipeline.load import UNKNOWN_KEY, load_warehouse
from fhir_pipeline.transform import AllergyRecord, PatientRecord


def patient(pid="p1", **overrides):
    fields = dict(patient_id=pid, name_prefix="Ms.", family_name="Doe",
                  given_name="Jane", gender="female", birth_date="1980-06-15",
                  address_line="1 Main St", city="Boston", state="MA",
                  postal_code="02149", country="US", phone="555-0100")
    fields.update(overrides)
    return PatientRecord(**fields)


def allergy(aid="a1", pid="p1", **overrides):
    fields = dict(allergy_id=aid, patient_id=pid, allergy_type="allergy",
                  category="food", criticality="low",
                  code_system="http://snomed.info/sct", code="91935009",
                  code_display="Allergy to peanuts",
                  recorded_at="2020-01-01T09:30:00+00:00",
                  recorded_date="2020-01-01")
    fields.update(overrides)
    return AllergyRecord(**fields)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "warehouse.db"


def query(db_path, sql):
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_fact_joins_back_to_all_dimensions(db):
    load_warehouse(db, [patient()], [allergy()], [])
    rows = query(db, """
        SELECT p.family_name, a.code_display, c.category, cr.criticality
        FROM fact_allergy_intolerance f
        JOIN dim_patient p USING (patient_key)
        JOIN dim_allergen a USING (allergen_key)
        JOIN dim_category c USING (category_key)
        JOIN dim_criticality cr USING (criticality_key)
    """)
    assert rows == [("Doe", "Allergy to peanuts", "food", "low")]


def test_orphan_allergy_links_to_unknown_patient_instead_of_vanishing(db):
    orphans = load_warehouse(db, [patient()], [allergy(pid="no-such-patient")], [])
    assert orphans == 1
    assert query(db, "SELECT patient_key FROM fact_allergy_intolerance") == [(UNKNOWN_KEY,)]
    # the fact is still there — allergy counts stay correct
    assert query(db, "SELECT COUNT(*) FROM fact_allergy_intolerance") == [(1,)]


def test_shared_allergen_is_stored_once(db):
    load_warehouse(db, [patient("p1"), patient("p2")],
                   [allergy("a1", "p1"), allergy("a2", "p2")], [])
    # 1 real allergen + the seeded Unknown row
    assert query(db, "SELECT COUNT(*) FROM dim_allergen") == [(2,)]


def test_rerun_is_idempotent(db):
    for _ in range(2):
        load_warehouse(db, [patient()], [allergy()], [])
    assert query(db, "SELECT COUNT(*) FROM fact_allergy_intolerance") == [(1,)]
    assert query(db, "SELECT COUNT(*) FROM dim_patient WHERE patient_key != -1") == [(1,)]


def test_patient_without_allergies_still_in_dimension(db):
    load_warehouse(db, [patient("p1"), patient("p2")], [allergy(pid="p1")], [])
    rows = query(db, """
        SELECT p.patient_id, COUNT(f.allergy_key)
        FROM dim_patient p
        LEFT JOIN fact_allergy_intolerance f USING (patient_key)
        WHERE p.patient_key != -1
        GROUP BY p.patient_id ORDER BY p.patient_id
    """)
    assert rows == [("p1", 1), ("p2", 0)]


def test_nothing_from_the_source_is_dropped(db):
    load_warehouse(db, [patient()], [allergy()], [])
    rows = query(db, """
        SELECT p.phone, p.postal_code, p.address_line, f.recorded_at, f.recorded_date
        FROM fact_allergy_intolerance f JOIN dim_patient p USING (patient_key)
    """)
    assert rows == [("555-0100", "02149", "1 Main St",
                     "2020-01-01T09:30:00+00:00", "2020-01-01")]


def test_no_broken_foreign_keys(db):
    load_warehouse(db, [patient()], [allergy(), allergy("a2", "ghost")], [])
    connection = sqlite3.connect(db)
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    connection.close()
    assert violations == []
