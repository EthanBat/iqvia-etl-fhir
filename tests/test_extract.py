"""Extract tests: every way a source line can be bad, plus one happy path."""

import json

from fhir_pipeline.extract import extract_ndjson

VALID_PATIENT = json.dumps({"resourceType": "Patient", "id": "p1", "gender": "female"})


def write_lines(tmp_path, lines):
    path = tmp_path / "input.ndjson"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_valid_line_is_accepted(tmp_path):
    path = write_lines(tmp_path, [VALID_PATIENT])
    records, rejects = extract_ndjson(path, "Patient")
    assert len(records) == 1 and rejects == []
    assert records[0]["id"] == "p1"


def test_broken_json_is_quarantined_not_fatal(tmp_path):
    # Mirrors the real corruption in the export: a key with no value.
    bad = '{"resourceType":"AllergyIntolerance","id":"a1","recordedDate":}'
    path = write_lines(tmp_path, [VALID_PATIENT, bad, VALID_PATIENT])
    records, rejects = extract_ndjson(path, "Patient")
    assert len(records) == 2          # the good lines survive the bad one
    assert len(rejects) == 1
    assert rejects[0]["line_number"] == 2
    assert "broken JSON" in rejects[0]["reason"]
    assert rejects[0]["raw_text"] == bad  # original text kept for replay


def test_wrong_resource_type_is_rejected(tmp_path):
    path = write_lines(tmp_path, [VALID_PATIENT])
    records, rejects = extract_ndjson(path, "AllergyIntolerance")
    assert records == []
    assert rejects[0]["reason"] == "wrong resourceType"


def test_missing_id_is_rejected(tmp_path):
    path = write_lines(tmp_path, ['{"resourceType": "Patient"}'])
    records, rejects = extract_ndjson(path, "Patient")
    assert records == []
    assert rejects[0]["reason"] == "missing id"


def test_blank_lines_are_skipped(tmp_path):
    path = write_lines(tmp_path, ["", VALID_PATIENT, "   ", ""])
    records, rejects = extract_ndjson(path, "Patient")
    assert len(records) == 1 and rejects == []


def test_non_object_json_is_rejected(tmp_path):
    path = write_lines(tmp_path, ['["not", "an", "object"]'])
    records, rejects = extract_ndjson(path, "Patient")
    assert records == []
    assert rejects[0]["reason"] == "not a JSON object"
