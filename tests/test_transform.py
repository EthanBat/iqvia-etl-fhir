"""Transform tests: one test per cleaning rule, driven by the dirt actually
found in the source files (plus a few defensive extras)."""

from fhir_pipeline.transform import (
    UNKNOWN,
    clean_category,
    clean_date,
    clean_datetime,
    transform_allergy,
    transform_patient,
)

# ---------------------------------------------------------------- category

def test_valid_category_passes_through():
    issues = []
    assert clean_category(["food"], "a1", issues) == "food"
    assert issues == []


def test_trailing_space_and_case_are_cleaned():
    issues = []
    assert clean_category(["environmental "], "a1", issues) == "environment"
    assert len(issues) == 1  # the fix is written down, not silent


def test_pet_allergy_maps_to_environment_via_fixes_table():
    issues = []
    assert clean_category(["pet allergy"], "a1", issues) == "environment"


def test_empty_null_and_blank_categories_become_unknown():
    for dirty in ([], [None], [""], None):
        issues = []
        assert clean_category(dirty, "a1", issues) == UNKNOWN
        assert len(issues) == 1


def test_made_up_category_becomes_unknown_not_guessed():
    issues = []
    assert clean_category(["banana"], "a1", issues) == UNKNOWN
    assert "not a valid FHIR category" in issues[0].action


# ------------------------------------------------------------ dates

def test_birth_date_is_parsed():
    assert clean_date("1980-06-15", "birthDate", "Patient", "p1", []) == "1980-06-15"


def test_timestamp_is_kept_and_date_derived_from_it():
    # No information is lost: the original timestamp stays, the date is a bonus.
    recorded_at, recorded_date = clean_datetime(
        "1972-08-10T17:48:38-04:00", "recordedDate", "AllergyIntolerance", "a1", [])
    assert recorded_at == "1972-08-10T17:48:38-04:00"
    assert recorded_date == "1972-08-10"


def test_date_only_value_is_valid_fhir():
    # There never was a time component, so only the date can be stored.
    recorded_at, recorded_date = clean_datetime(
        "1995-05-10", "recordedDate", "AllergyIntolerance", "a1", [])
    assert recorded_at is None
    assert recorded_date == "1995-05-10"


def test_garbage_and_empty_dates_become_null():
    for dirty in ("date", "", "   ", None, 12345):
        issues = []
        assert clean_datetime(dirty, "recordedDate",
                              "AllergyIntolerance", "a1", issues) == (None, None)
        assert len(issues) == 1


# ---------------------------------------------------------------- allergy

def make_allergy(**overrides):
    resource = {
        "resourceType": "AllergyIntolerance",
        "id": "a1",
        "type": "allergy",
        "category": ["food"],
        "criticality": "low",
        "code": {"coding": [{"system": "http://snomed.info/sct",
                             "code": "91935009", "display": "Allergy to peanuts"}]},
        "patient": {"reference": "Patient/p1"},
        "recordedDate": "2020-01-01T00:00:00+00:00",
    }
    resource.update(overrides)
    return resource


def test_clean_allergy_transforms_fully():
    issues = []
    record = transform_allergy(make_allergy(), issues)
    assert record.patient_id == "p1"
    assert record.code == "91935009"
    assert record.category == "food"
    assert record.criticality == "low"
    assert record.recorded_at == "2020-01-01T00:00:00+00:00"
    assert record.recorded_date == "2020-01-01"
    assert issues == []


def test_missing_patient_reference_yields_none_and_issue():
    issues = []
    record = transform_allergy(make_allergy(patient={}), issues)
    assert record.patient_id is None
    assert any(i.field == "patient.reference" for i in issues)


def test_urn_uuid_reference_style_is_supported():
    record = transform_allergy(make_allergy(patient={"reference": "urn:uuid:p9"}), [])
    assert record.patient_id == "p9"


def test_invalid_criticality_becomes_unknown():
    issues = []
    record = transform_allergy(make_allergy(criticality="EXTREME"), issues)
    assert record.criticality == UNKNOWN


# ---------------------------------------------------------------- patient

def make_patient(**overrides):
    resource = {
        "resourceType": "Patient",
        "id": "p1",
        "name": [{"prefix": ["Ms."], "family": "Doe", "given": ["Jane", "Q"]}],
        "telecom": [{"system": "phone", "value": "555-0100", "use": "home"}],
        "gender": "female",
        "birthDate": "1980-06-15",
        "address": [{"line": ["1 Main St"], "city": "Boston",
                     "state": "Massachusetts", "postalCode": "02149",
                     "country": "US"}],
    }
    resource.update(overrides)
    return resource


def test_clean_patient_keeps_every_source_field():
    issues = []
    record = transform_patient(make_patient(), issues)
    assert record.name_prefix == "Ms."
    assert record.family_name == "Doe"
    assert record.given_name == "Jane Q"  # several given names are joined
    assert record.gender == "female"
    assert record.phone == "555-0100"
    assert record.address_line == "1 Main St"
    assert record.postal_code == "02149"
    assert record.country == "US"
    assert issues == []


def test_non_phone_telecom_is_not_mistaken_for_a_phone():
    resource = make_patient(telecom=[{"system": "email", "value": "j@x.com"}])
    assert transform_patient(resource, []).phone is None


def test_patient_with_missing_fields_degrades_gracefully():
    issues = []
    record = transform_patient({"resourceType": "Patient", "id": "p1"}, issues)
    assert record.family_name is None
    assert record.gender == UNKNOWN
    assert record.birth_date is None
    assert issues  # every gap is written down
