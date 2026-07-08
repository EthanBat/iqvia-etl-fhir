# FHIR Allergy Pipeline

A small file-based ETL pipeline. It reads FHIR `Patient` and
`AllergyIntolerance` resources from NDJSON exports, cleans them up, and loads
a star schema into a local SQLite database, so the data can be used for
patient-level reporting or trend analysis.

There are no third-party dependencies — everything runs on the Python 3.9+
standard library. You only need `pytest` if you want to run the tests.

## Running it

```bash
# defaults: reads data/*.ndjson, writes output/warehouse.db
python3 -m fhir_pipeline

# or point it at other files
python3 -m fhir_pipeline --patients data/Patient.ndjson \
                         --allergies data/AllergyIntolerance.ndjson \
                         --db output/warehouse.db

# tests
python3 -m pip install pytest
python3 -m pytest
```

At the end of a run you get a short summary:

```
Pipeline run complete
  Patients loaded        : 10
  Allergy facts loaded   : 36
  ...with unknown patient: 1
  Lines rejected         : 2 -> output/rejects.ndjson
  Cleaning actions       : 14 -> table data_quality_issue
  Warehouse              : output/warehouse.db
```

## What a run produces

Everything lands in `output/` (git-ignored, rebuilt on every run):

- `warehouse.db` — the SQLite warehouse with the star schema, ready to
  query: `sqlite3 output/warehouse.db`. The example queries below run
  against it as-is.
- `rejects.ndjson` — the quarantine. One line per input line the pipeline
  couldn't use, with the source file, line number, the reason (including
  which field broke and the text around the error), and the original line
  untouched — so it can be fixed upstream and replayed.

## How it works

Classic ETL, one module per step:

```
 NDJSON files           fhir_pipeline/               SQLite
┌───────────────┐   ┌─────────┐ ┌───────────┐ ┌────────┐
│ Patient       │──►│ EXTRACT │►│ TRANSFORM │►│ LOAD   │──► warehouse.db
│ AllergyIntol. │   └────┬────┘ └───────────┘ └────────┘
└───────────────┘        ▼
                 output/rejects.ndjson
```

The extract step reads each file line by line. If a line doesn't parse, the
run doesn't crash: the line gets written to `output/rejects.ndjson` together
with the reason, so it can be fixed upstream and replayed later.

The transform step does the cleaning. Values are checked against the official
FHIR value sets, a couple of known-bad spellings get fixed through a small
lookup table, and anything unusable becomes `unknown` or NULL. Every fix is
recorded; nothing is corrected silently.

The load step rebuilds the star schema inside a single transaction, so
running the pipeline twice gives you exactly the same database.

## The data model

One fact table in the middle, dimensions around it. The grain of the fact
table is one row per documented allergy per patient, which means analysis is
mostly a matter of counting rows.

```
        dim_patient                    dim_allergen
    (WHO: demographics)          (WHAT: SNOMED-coded substance)
              ▲                              ▲
              │                              │
              └──── fact_allergy_intolerance ┘
           (allergy_id, type, recorded_at, recorded_date)
              ┌──────────────┴──────────────┐
              ▼                             ▼
        dim_category                  dim_criticality
     (food / environment)          (low / high / unknown)
```

There's also a `data_quality_issue` table holding every cleaning action the
pipeline took, so you can ask the warehouse itself what was wrong with the
source data.

One detail worth pointing out: one allergy in the export references a patient
that isn't in the patient file. Dropping it would quietly understate the
allergy counts, so instead it gets linked to a special "Unknown" patient row
(`patient_key = -1`). The totals stay correct and the gap stays visible.

Nothing from the source is thrown away either. Every patient field is loaded
(name prefix, address, phone, postal code and so on), and `recordedDate` is
kept twice: once as the original timestamp (`recorded_at`) and once as a
plain date (`recorded_date`), because that's what you usually group by.

## Some example queries

```sql
-- Allergy burden per patient (patients without allergies included)
SELECT p.family_name, p.given_name, COUNT(f.allergy_key) AS allergies
FROM dim_patient p
LEFT JOIN fact_allergy_intolerance f USING (patient_key)
WHERE p.patient_key != -1
GROUP BY p.patient_key
ORDER BY allergies DESC;

-- Allergies by category and criticality
SELECT c.category, cr.criticality, COUNT(*) AS n
FROM fact_allergy_intolerance f
JOIN dim_category c USING (category_key)
JOIN dim_criticality cr USING (criticality_key)
GROUP BY 1, 2 ORDER BY n DESC;

-- Most common allergens
SELECT a.code_display, COUNT(*) AS n
FROM fact_allergy_intolerance f
JOIN dim_allergen a USING (allergen_key)
GROUP BY a.allergen_key ORDER BY n DESC;

-- What did the pipeline have to clean up?
SELECT field, action, COUNT(*) AS n
FROM data_quality_issue GROUP BY 1, 2;
```

## What the pipeline had to deal with

The source files contain a handful of problems. In short:

- two lines of broken JSON — quarantined to `output/rejects.ndjson`
- one allergy pointing to a patient that doesn't exist — kept, linked to the
  Unknown patient row
- categories like `"pet allergy"` and `"environmental "` (trailing space) —
  fixed via an explicit lookup table, and recorded
- empty or null categories — set to `unknown`, and recorded
- `recordedDate` missing, empty, or literally the string `"date"` — stored as
  NULL, and recorded
- two patients without any allergies — still present in `dim_patient`, just
  use a LEFT JOIN

## Project layout

```
fhir_pipeline/     the pipeline package (extract / transform / load / CLI)
fhir_pipeline/sql/ the star-schema DDL (schema.sql)
tests/             29 tests (pytest), one per cleaning rule / model behavior
data/              source NDJSON exports
output/            generated: warehouse.db, rejects.ndjson (git-ignored)
docs/DECISIONS.md  design decisions and trade-offs
```

If you want to know why things are built this way, and what I would change at
production scale, have a look at [docs/DECISIONS.md](docs/DECISIONS.md).
