"""Run the pipeline:  python3 -m fhir_pipeline

The three ETL steps, in order:

    1. EXTRACT   read the NDJSON files, quarantine broken lines
    2. TRANSFORM clean the values, note every fix as a quality issue
    3. LOAD      rebuild the star schema in SQLite
"""

import argparse
import logging
import sys
from pathlib import Path

from .extract import extract_ndjson, write_rejects
from .load import load_warehouse
from .transform import transform_allergy, transform_patient


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="fhir_pipeline",
        description="Load FHIR NDJSON exports into a star-schema SQLite warehouse.")
    parser.add_argument("--patients", type=Path, default=Path("data/Patient.ndjson"))
    parser.add_argument("--allergies", type=Path,
                        default=Path("data/AllergyIntolerance.ndjson"))
    parser.add_argument("--db", type=Path, default=Path("output/warehouse.db"))
    parser.add_argument("--rejects", type=Path, default=Path("output/rejects.ndjson"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    # 1. EXTRACT
    patients_raw, patient_rejects = extract_ndjson(args.patients, "Patient")
    allergies_raw, allergy_rejects = extract_ndjson(args.allergies, "AllergyIntolerance")
    rejects = patient_rejects + allergy_rejects
    write_rejects(rejects, args.rejects)

    # 2. TRANSFORM  (issues collects a note for every fix that was needed)
    issues = []
    patients = [transform_patient(r, issues) for r in patients_raw]
    allergies = [transform_allergy(r, issues) for r in allergies_raw]

    # 3. LOAD
    orphans = load_warehouse(args.db, patients, allergies, issues)

    print()
    print("Pipeline run complete")
    print(f"  Patients loaded        : {len(patients)}")
    print(f"  Allergy facts loaded   : {len(allergies)}")
    print(f"  ...with unknown patient: {orphans}")
    print(f"  Lines rejected         : {len(rejects)} -> {args.rejects}")
    print(f"  Cleaning actions       : {len(issues)} -> table data_quality_issue")
    print(f"  Warehouse              : {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
