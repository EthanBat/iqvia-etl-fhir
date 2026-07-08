-- Star schema for the FHIR allergy warehouse.
--
-- One fact table in the middle, dimension tables around it:
--
--   dim_patient      WHO   the allergy belongs to
--   dim_allergen     WHAT  substance (SNOMED-coded)
--   dim_category     TYPE  of allergen (food / environment / ...)
--   dim_criticality  RISK  level (low / high / ...)
--   fact_allergy_intolerance : one row = one documented allergy
--
-- The whole schema is dropped and rebuilt on every run (full refresh),
-- so running the pipeline twice gives exactly the same database.
--
-- NOTE: column names deliberately match the field names of the dataclasses
-- in transform.py — the Python code generates its INSERT statements from
-- those fields. To add a column: add it here AND to the dataclass. Done.

DROP TABLE IF EXISTS fact_allergy_intolerance;
DROP TABLE IF EXISTS dim_patient;
DROP TABLE IF EXISTS dim_allergen;
DROP TABLE IF EXISTS dim_category;
DROP TABLE IF EXISTS dim_criticality;
DROP TABLE IF EXISTS data_quality_issue;

CREATE TABLE dim_patient (
    patient_key   INTEGER PRIMARY KEY,
    patient_id    TEXT NOT NULL UNIQUE,   -- the original FHIR id
    name_prefix   TEXT,
    family_name   TEXT,
    given_name    TEXT,
    gender        TEXT NOT NULL,
    birth_date    TEXT,
    address_line  TEXT,
    city          TEXT,
    state         TEXT,
    postal_code   TEXT,
    country       TEXT,
    phone         TEXT
);

CREATE TABLE dim_allergen (
    allergen_key  INTEGER PRIMARY KEY,
    code_system   TEXT,                   -- e.g. http://snomed.info/sct
    code          TEXT,                   -- e.g. 419474003
    code_display  TEXT,                   -- e.g. 'Allergy to mould'
    UNIQUE (code_system, code)
);

CREATE TABLE dim_category (
    category_key  INTEGER PRIMARY KEY,
    category      TEXT NOT NULL UNIQUE
);

CREATE TABLE dim_criticality (
    criticality_key INTEGER PRIMARY KEY,
    criticality     TEXT NOT NULL UNIQUE
);

CREATE TABLE fact_allergy_intolerance (
    allergy_key     INTEGER PRIMARY KEY,
    allergy_id      TEXT NOT NULL UNIQUE, -- the original FHIR id
    patient_key     INTEGER NOT NULL REFERENCES dim_patient (patient_key),
    allergen_key    INTEGER NOT NULL REFERENCES dim_allergen (allergen_key),
    category_key    INTEGER NOT NULL REFERENCES dim_category (category_key),
    criticality_key INTEGER NOT NULL REFERENCES dim_criticality (criticality_key),
    allergy_type    TEXT,                 -- allergy | intolerance
    recorded_at     TEXT,                 -- full timestamp as exported
    recorded_date   TEXT                  -- derived YYYY-MM-DD, for grouping
);

-- Every cleaning action the transform step took, kept queryable.
CREATE TABLE data_quality_issue (
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    field         TEXT NOT NULL,
    raw_value     TEXT,
    action        TEXT NOT NULL
);

-- Seed the 'Unknown' rows (key -1) that broken references fall back to:
-- an allergy whose patient is missing links here instead of being dropped.
INSERT INTO dim_patient (patient_key, patient_id, gender)
VALUES (-1, 'UNKNOWN', 'unknown');

INSERT INTO dim_allergen (allergen_key, code, code_display)
VALUES (-1, 'UNKNOWN', 'Unknown allergen');
