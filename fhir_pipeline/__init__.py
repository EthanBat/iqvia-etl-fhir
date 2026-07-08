"""File-based FHIR pipeline: NDJSON exports -> star-schema SQLite warehouse.

The classic ETL pattern, one module per step:

    extract.py    read NDJSON files, quarantine broken lines
    transform.py  clean the values, keep an audit trail of every fix
    load.py       build the star schema in SQLite
    __main__.py   ties the three steps together (python3 -m fhir_pipeline)
"""

__version__ = "1.0.0"
