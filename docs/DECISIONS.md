# Design decisions

Some notes on why the pipeline is built the way it is, what I considered and
rejected along the way, and what I'd do differently at production scale.

## Profiling first

Before writing any pipeline code I went through the raw files line by line.
That turned out to be time well spent, because the files contain a number of
problems you would never guess from the FHIR documentation alone:

- two lines of broken JSON in `AllergyIntolerance.ndjson` (lines 13 and 33,
  truncated values)
- one allergy that references a patient who isn't in the patient export
- non-standard categories: `"pet allergy"`, and `"environmental "` with a
  trailing space (7 records in total)
- empty, `[null]` or `[""]` categories (3 records)
- a `recordedDate` that is missing, empty, or literally the string `"date"`
  (4 records)
- one date-only `recordedDate` — which is actually valid FHIR, just lower
  precision
- two patients with no allergies at all
- and notably: no `reaction`, `severity` or `clinicalStatus` fields anywhere
  in either file

A pipeline written straight from the spec would have crashed on line 13.
Knowing all of this up front meant each case could be handled deliberately
instead of being discovered in a stack trace.

## Why plain Python and SQLite

The brief asks for a solution that runs locally with a local relational
database. With just the standard library and SQLite there is literally
nothing to install: clone it, run `python3 -m fhir_pipeline`, done. And
SQLite is a real relational engine — SQL, transactions, foreign keys — it
just doesn't need a server. For 48 input records, anything heavier is setup
cost without benefit.

I did consider alternatives. pandas would have made the transforms shorter,
but it makes row-level error handling (quarantining a single bad record)
awkward, and it adds a dependency. DuckDB is great for analytics, but SQLite
seemed like the most universal reading of "local relational database".
Postgres in Docker would be closest to production, but then "we can run it
locally" suddenly depends on having Docker installed.

At real production scale — say, daily multi-GB exports — the picture changes
completely: files land in object storage, transforms run in Spark or dbt,
the data lives in a warehouse like Snowflake or BigQuery, and an orchestrator
(Airflow, Dagster) takes care of scheduling, retries and alerting. The
extract/transform/load split in this codebase maps pretty much 1:1 onto that
world, which is exactly why I kept the steps separate.

## Structure

```
extract.py -> transform.py -> load.py     (wired together by __main__.py)
```

Each step is small, testable on its own, and replaceable: swapping SQLite
for Postgres only touches `load.py`, and swapping local files for the FHIR
bulk-export API only touches `extract.py`. The hand-off between the steps is
a pair of small dataclasses (`PatientRecord`, `AllergyRecord`); once
transform has run, nothing downstream ever looks at raw FHIR again.

Extraction currently reads line by line into a list. For very large files
the same loop would become a generator so memory use stays flat.

Two smaller choices worth mentioning:

The DDL lives in `sql/schema.sql` rather than in a Python string. SQL in SQL
files gets syntax highlighting, can be reviewed by someone who only works in
SQL, and diffs cleanly when the model evolves.

The INSERT statements are generated from the dataclass fields instead of
written by hand. Column names in `schema.sql` match the dataclass field
names, so adding a column means touching exactly two places — the schema
file and the dataclass. Values always go through bound parameters; the only
things ever formatted into the SQL string are identifiers that come from our
own code.

## How bad data is handled

I stuck to three rules throughout:

1. Never crash on bad data. One corrupt record shouldn't abort a clinical
   batch — damage is contained to a field where possible, a record at worst.
2. Never fix silently. Rejected lines go to `output/rejects.ndjson`, and
   every field-level fix becomes a row in the `data_quality_issue` table, so
   the warehouse stays auditable.
3. Never guess. All fixes are deterministic: explicit value sets and an
   explicit fixes table. No fuzzy matching, no inference.

Applied to the actual problems in the data:

Broken JSON gets quarantined, not auto-repaired. Line 13 could plausibly be
patched by hand, but any repair is a guess about what the source system
meant. Parking the line with its reason and raising it upstream is the
honest option, and the line can be replayed once it's fixed.

The allergy with the orphan patient reference is kept and linked to an
"Unknown" patient row (`patient_key = -1`). Dropping it would silently
understate allergy counts; this way the totals stay right and the gap stays
queryable. This is also the standard Kimball treatment for a missing
dimension member.

`"pet allergy"` maps to `environment` through a small fixes table. The
mapping is clinically defensible (animal dander is an environmental
allergen, and the SNOMED codes on those records back that up), and because
it lives in one small dict, a data steward can review and approve it. Values
that can't be mapped become `unknown` rather than being guessed at.

Bad dates become NULL. An honest gap beats an invented date. The one
date-only value is valid FHIR, so it's kept as-is.

And nothing from the source is dropped. Every field the export provides gets
loaded (name prefix, address, phone, postal code, ...), and `recordedDate`
is stored twice on purpose: `recorded_at` keeps the original timestamp
exactly as exported, `recorded_date` is derived from it for convenient
grouping. Information lost at load time can't be recovered later. (In a real
deployment, loading personal fields like phone numbers would have to be
weighed against data-minimization rules for health data — here the data is
synthetic, so completeness wins.)

## The star schema

The grain comes first (Kimball's rule number one): one fact row is one
documented allergy/intolerance observation for one patient.

A few things about the model:

- `fact_allergy_intolerance` is a factless fact table. There is no numeric
  measure in the source to sum, so the recorded event itself is the measure
  and analyses count rows.
- Dimensions get surrogate integer keys; the original FHIR UUIDs are kept in
  UNIQUE columns. The warehouse shouldn't depend on how the source system
  happens to format its ids.
- `allergy_id` stays on the fact as a degenerate dimension — useful for
  tracing a fact back to its source resource, but not worth a table of its
  own.
- On the dimensions suggested in the brief: *patient* became `dim_patient`,
  and *allergen type* became `dim_allergen` (the SNOMED substance) plus
  `dim_category`. *Reaction type* and *severity* are a different story: the
  export contains no reaction or severity fields at all, so I didn't model
  them — a dimension over data that is 100% absent would just be fiction.
  The closest real field, `criticality` (patient-level risk), became
  `dim_criticality`. If reactions show up later they'd arrive as a separate
  child table anyway, since one allergy can have many reactions and that's a
  different grain.
- The fact keeps both `recorded_at` (exact timestamp, no information loss)
  and `recorded_date` (derived `YYYY-MM-DD`, what analysis usually groups
  by). If fiscal or weekly reporting ever becomes a requirement, a proper
  `dim_date` calendar dimension would be the first extension.

## Idempotency

Each run drops and rebuilds all tables inside one transaction. Re-running
the pipeline always produces the same state, with no duplicates, and a
failed run leaves no half-loaded warehouse behind. For batch file input this
is the simplest strategy that is actually correct. With continuous or much
larger feeds, the natural next step is incremental upserts keyed on the
natural keys.

## Tests

There are 29 tests in three files, mirroring the three steps. The idea is to
test our rules, not the language: the tests cover the cleaning rules and the
modeling behavior — the code we own — not JSON parsing itself.

Every problem found during profiling has a named test, for example
`test_orphan_allergy_links_to_unknown_patient_instead_of_vanishing` and
`test_broken_json_is_quarantined_not_fatal`, so the suite doubles as a
description of how the pipeline behaves. The load tests build a real
(temporary) SQLite file and assert on what an analyst would actually see,
including `test_rerun_is_idempotent` and a foreign-key integrity sweep.

## Limitations and what I'd do next

- Validation depth: the value-set checks are hand-rolled for the fields this
  pipeline uses. A production system would validate resources against the
  full FHIR spec, e.g. with the HAPI validator or the `fhir.resources`
  library.
- Terminology: SNOMED display text is taken from the source as-is; a real
  deployment would resolve codes against a terminology service.
- Operations: the printed summary and the `data_quality_issue` table are a
  starting point for observability. Production would add scheduling,
  alerting on reject-rate thresholds, and data lineage.
- PHI: names are loaded because this is synthetic data. Real clinical data
  would need de-identification and access controls before any analytics.
