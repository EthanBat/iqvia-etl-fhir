"""EXTRACT step: read the NDJSON files.

Each line of an NDJSON file should be one JSON object (one FHIR resource).
Real exports sometimes contain broken lines, so the rule here is:

    a broken line never crashes the pipeline — it goes to a "rejects" file
    with the reason, so someone can look at it later.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# A JSON object key: a quoted string followed by a colon.
_JSON_KEY = re.compile(r'"([^"\\]+)"\s*:')


def extract_ndjson(path: Path, expected_resource_type: str):
    """Read one NDJSON file and return two lists: (records, rejects).

    records : parsed FHIR resources (plain dicts) that look usable
    rejects : lines we could not use, with the reason why
    """
    records = []
    rejects = []

    with open(path, encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            text = line.strip()
            if not text:
                continue  # skip blank lines

            # Reason 1 to reject: the line is not valid JSON at all.
            try:
                resource = json.loads(text)
            except json.JSONDecodeError as error:
                reason, context = _describe_json_error(error, text)
                rejects.append(_reject(path, line_number, reason, text, context))
                continue

            # Reason 2: it is valid JSON but not an object like {...}
            if not isinstance(resource, dict):
                rejects.append(_reject(path, line_number, "not a JSON object", text))
                continue

            # Reason 3: it is the wrong kind of resource for this file.
            if resource.get("resourceType") != expected_resource_type:
                rejects.append(_reject(path, line_number, "wrong resourceType", text))
                continue

            # Reason 4: no id — we could never trace this record back.
            if not resource.get("id"):
                rejects.append(_reject(path, line_number, "missing id", text))
                continue

            records.append(resource)

    return records, rejects


def _describe_json_error(error: json.JSONDecodeError, text: str) -> tuple:
    """Turn a parse error into something a person can act on.

    The parser only knows the character position where it gave up, so we add
    two things: the last object key seen before that position (almost always
    the field whose value is broken) and a snippet of the line around it.
    """
    pos = error.pos
    keys = _JSON_KEY.findall(text[:pos])
    reason = f"broken JSON: {error.msg} (char {pos})"
    if keys:
        reason += f", while reading field '{keys[-1]}'"
    start = max(0, pos - 40)
    context = ("..." if start > 0 else "") + text[start:pos + 20]
    return reason, context


def _reject(path: Path, line_number: int, reason: str, text: str, context: str = None) -> dict:
    logger.warning("Rejected %s line %d: %s", path.name, line_number, reason)
    reject = {
        "source_file": path.name,
        "line_number": line_number,
        "reason": reason,
        "raw_text": text,  # keep the original line so it can be fixed and replayed
    }
    if context is not None:
        reject["error_context"] = context
    return reject


def write_rejects(rejects: list, path: Path) -> None:
    """Save rejected lines to their own NDJSON file (the 'quarantine')."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        for reject in rejects:
            file.write(json.dumps(reject) + "\n")
