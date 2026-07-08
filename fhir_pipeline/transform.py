"""TRANSFORM step: turn raw FHIR resources into clean, flat records.

Three rules for dirty data:

    1. never crash    — a bad field becomes NULL or 'unknown', the record survives
    2. never be silent — every fix is written down as a QualityIssue
    3. never guess     — fixes come from small lookup tables anyone can review
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"

# The values FHIR actually allows for each field ("value sets").
VALID_CATEGORIES = {"food", "medication", "environment", "biologic"}
VALID_CRITICALITY = {"low", "high", "unable-to-assess"}
VALID_GENDERS = {"male", "female", "other", "unknown"}

# Known-bad spellings seen in the source data, and what they should be.
# An explicit table like this can be reviewed and approved by a person.
CATEGORY_FIXES = {
    "environmental": "environment",
    "pet allergy": "environment",  # animal dander is an environmental allergen
}


@dataclass(frozen=True)
class QualityIssue:
    """One data problem we found, and what we did about it."""
    resource_type: str
    resource_id: str
    field: str
    raw_value: str
    action: str


@dataclass(frozen=True)
class PatientRecord:
    """A cleaned patient — one row of the future dim_patient table.

    Everything the source offers is kept: the warehouse should never know
    less than the export it was built from.
    """
    patient_id: str
    name_prefix: Optional[str]       # e.g. "Mrs."
    family_name: Optional[str]
    given_name: Optional[str]
    gender: str
    birth_date: Optional[str]
    address_line: Optional[str]
    city: Optional[str]
    state: Optional[str]
    postal_code: Optional[str]
    country: Optional[str]
    phone: Optional[str]


@dataclass(frozen=True)
class AllergyRecord:
    """A cleaned allergy — one row of the future fact table."""
    allergy_id: str
    patient_id: Optional[str]
    allergy_type: Optional[str]      # "allergy" or "intolerance"
    category: str                    # food / environment / ... / unknown
    criticality: str                 # low / high / ... / unknown
    code_system: Optional[str]       # e.g. http://snomed.info/sct
    code: Optional[str]              # e.g. 419474003
    code_display: Optional[str]      # e.g. "Allergy to mould"
    recorded_at: Optional[str]       # full timestamp as given, e.g. 2016-12-05T14:24:42-05:00
    recorded_date: Optional[str]     # derived YYYY-MM-DD, for easy grouping


# ---------------------------------------------------------------------------
# Small cleaning helpers — each handles one field, each is unit-tested.
# ---------------------------------------------------------------------------

def clean_category(raw, allergy_id: str, issues: list) -> str:
    """FHIR sends category as a list like ["food"]. Return one clean value."""
    # Take the first non-empty text in the list (this dataset has at most one).
    first = ""
    if isinstance(raw, list):
        for value in raw:
            if isinstance(value, str) and value.strip():
                first = value
                break

    cleaned = first.strip().lower()

    if not cleaned:
        issues.append(QualityIssue("AllergyIntolerance", allergy_id, "category",
                                   repr(raw), "missing -> 'unknown'"))
        return UNKNOWN

    if cleaned in CATEGORY_FIXES:
        cleaned = CATEGORY_FIXES[cleaned]

    if cleaned not in VALID_CATEGORIES:
        issues.append(QualityIssue("AllergyIntolerance", allergy_id, "category",
                                   repr(first), "not a valid FHIR category -> 'unknown'"))
        return UNKNOWN

    if cleaned != first:
        issues.append(QualityIssue("AllergyIntolerance", allergy_id, "category",
                                   repr(first), f"cleaned up -> '{cleaned}'"))
    return cleaned


def clean_choice(raw, valid: set, field: str, resource_type: str,
                 resource_id: str, issues: list) -> str:
    """For fields with a fixed list of allowed values (gender, criticality)."""
    if isinstance(raw, str) and raw.strip().lower() in valid:
        return raw.strip().lower()
    issues.append(QualityIssue(resource_type, resource_id, field,
                               repr(raw), "missing or invalid -> 'unknown'"))
    return UNKNOWN


def clean_date(raw, field: str, resource_type: str,
               resource_id: str, issues: list) -> Optional[str]:
    """Return a YYYY-MM-DD date, or None if the value is missing or garbage.

    A NULL is honest; inventing a date would poison every date-based analysis.
    """
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            pass  # fall through: value like "date" is not parseable
    issues.append(QualityIssue(resource_type, resource_id, field,
                               repr(raw), "missing or invalid -> NULL"))
    return None


def clean_datetime(raw, field: str, resource_type: str,
                   resource_id: str, issues: list):
    """Return (full timestamp, YYYY-MM-DD date) — both are kept.

    The timestamp is stored exactly as the source gave it (no information
    lost); the date is derived from it as a convenience for grouping.
    FHIR also allows date-only values: then the date is kept and the
    timestamp is NULL (there was never a time to preserve).
    """
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            pass  # fall through: value like "date" is not parseable
        else:
            timestamp = raw if "T" in raw else None
            return timestamp, parsed.date().isoformat()
    issues.append(QualityIssue(resource_type, resource_id, field,
                               repr(raw), "missing or invalid -> NULL"))
    return None, None


def get_patient_id(resource: dict, allergy_id: str, issues: list) -> Optional[str]:
    """Pull the plain patient id out of a reference like 'Patient/<uuid>'."""
    reference = (resource.get("patient") or {}).get("reference") or ""
    patient_id = reference.removeprefix("urn:uuid:").split("/")[-1]
    if not patient_id:
        issues.append(QualityIssue("AllergyIntolerance", allergy_id, "patient.reference",
                                   repr(reference), "missing -> will use Unknown patient"))
        return None
    return patient_id


# ---------------------------------------------------------------------------
# One transformer per resource type.
# ---------------------------------------------------------------------------

def transform_patient(resource: dict, issues: list) -> PatientRecord:
    patient_id = resource["id"]
    name = (resource.get("name") or [{}])[0]        # first listed name
    address = (resource.get("address") or [{}])[0]  # first listed address

    # Find the first telecom entry that is a phone number.
    phone = None
    for telecom in resource.get("telecom") or []:
        if telecom.get("system") == "phone" and telecom.get("value"):
            phone = telecom["value"]
            break

    return PatientRecord(
        patient_id=patient_id,
        name_prefix=" ".join(name.get("prefix") or []) or None,
        family_name=name.get("family"),
        given_name=" ".join(name.get("given") or []) or None,
        gender=clean_choice(resource.get("gender"), VALID_GENDERS,
                            "gender", "Patient", patient_id, issues),
        birth_date=clean_date(resource.get("birthDate"),
                              "birthDate", "Patient", patient_id, issues),
        address_line=" ".join(address.get("line") or []) or None,
        city=address.get("city"),
        state=address.get("state"),
        postal_code=address.get("postalCode"),
        country=address.get("country"),
        phone=phone,
    )


def transform_allergy(resource: dict, issues: list) -> AllergyRecord:
    allergy_id = resource["id"]
    # The allergen is coded like: {"coding": [{"system", "code", "display"}]}
    coding = ((resource.get("code") or {}).get("coding") or [{}])[0]
    if not coding.get("code"):
        issues.append(QualityIssue("AllergyIntolerance", allergy_id, "code",
                                   repr(resource.get("code")),
                                   "missing -> will use Unknown allergen"))

    recorded_at, recorded_date = clean_datetime(
        resource.get("recordedDate"),
        "recordedDate", "AllergyIntolerance", allergy_id, issues)

    return AllergyRecord(
        allergy_id=allergy_id,
        patient_id=get_patient_id(resource, allergy_id, issues),
        allergy_type=resource.get("type"),
        category=clean_category(resource.get("category"), allergy_id, issues),
        criticality=clean_choice(resource.get("criticality"), VALID_CRITICALITY,
                                 "criticality", "AllergyIntolerance", allergy_id, issues),
        code_system=coding.get("system"),
        code=coding.get("code"),
        code_display=coding.get("display"),
        recorded_at=recorded_at,
        recorded_date=recorded_date,
    )
