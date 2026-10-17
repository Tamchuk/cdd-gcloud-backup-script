"""
Verify that a stored CDD backup can be restored: every file is readable and
complete, the records are consistent with each other, and nothing is missing
compared with CDD itself.

Runs locally (`make verify`) or as the Cloud Run job cdd-backup-verify.

Config (environment variables, or the matching --options):
  BUCKET_NAME       Cloud Storage bucket with the backups (--bucket)
  LOCAL_DIR         or a local folder that contains backups/ (--local)
  CDD_VAULT_ID      vault id (--vault)
  CDD_API_TOKEN     optional: enables the live count check against CDD
  MAX_AGE_DAYS      optional: FAIL if the newest complete backup is older (--max-age-days)

Locally, Cloud Storage access needs `gcloud auth application-default login` once.
Without --run, the newest run that has every default export complete is checked.

Per export:
  - the .done marker exists (no marker = partial file)
  - the file decompresses and parses as JSON to the end
  - its uncompressed size equals the size in the marker
  - the number of records equals the export's own "count" (CDD's count at export time)
  - no duplicate ids
  - (with a token) the record count is not lower than CDD's count now
Across exports (only if both sides are in the run):
  - every molecule's collections exist in collections
  - every batch's molecule is in molecules
  - every readout row's batch, protocol, run and plate exist
  - every run's protocol exists
  - every attached/source file in the files list is stored, with the right size
Content: molecules without a structure and readout rows without values are
reported as warnings (they can be legitimate).

Exit code 1 if any check FAILs, which marks the Cloud Run execution failed.
"""

import argparse
import gzip
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime

import ijson
import requests

from main import (
    BASE_URL,
    DEFAULT_EXPORTS,
    EXPORT_SOURCES,
    ZIP_EXPORTS,
    _size,
    request_with_retry,
)

# Checked first, in this order, so references can be checked as we go
CHECK_ORDER = [
    "collections",
    "molecules",
    "protocols",
    "runs",
    "plates",
    "batches",
    "readout_rows",
]
# Reference field in a record -> the export that must contain that id
REF_TARGETS = {
    "collection": "collections",
    "molecule": "molecules",
    "batch": "batches",
    "protocol": "protocols",
    "run": "runs",
    "plate": "plates",
}
# The live count may be higher than the backup's because of records added since
# the backup. Up to this fraction is a warning; more is a failure.
LIVE_COUNT_TOLERANCE = 0.01
MAX_EXAMPLES = 5
# Fields the restore and exit_kit.py depend on. FAIL if one is in NO record of an
# export: that means CDD changed its export format (a field can be legitimately
# empty in some records, so a single missing value is fine).
# "a.b": field b inside object a, or inside the items of list a.
# "a.*.b": field b inside the values of a, an object keyed by ids (readouts).
# Keep in sync with what exit_kit.py reads.
EXPECTED_FIELDS = {
    "molecules": ["id", "name", "molfile", "smiles", "inchi_key", "synonyms", "projects",
                  "collections", "collections.id", "source_files.id", "source_files.name"],
    "batches": ["id", "name", "molecule", "molecule.id", "molecule.name", "batch_fields",
                "salt_name", "formula_weight"],
    "readout_rows": ["id", "type", "protocol", "molecule", "batch", "run", "readouts",
                     "readouts.*.value", "well", "well.plate", "well.row", "well.col"],
    "protocols": ["id", "name", "readout_definitions", "readout_definitions.id",
                  "readout_definitions.name", "readout_definitions.unit_label", "runs"],
    "runs": ["id", "protocol", "run_date", "person", "run_fields", "source_files",
             "source_files.id", "source_files.name"],
    "plates": ["id", "name", "wells", "wells.row", "wells.col", "wells.batch"],
    "files": ["id", "name", "object", "bytes"],
}  # fmt: skip


def field_names(record):
    """The record's fields, plus one level down in EXPECTED_FIELDS' notation."""
    for key, value in record.items():
        yield key
        if isinstance(value, dict) and value:
            first = next(iter(value.values()))
            if all(k.isdigit() for k in value) and isinstance(first, dict):
                yield from (f"{key}.*.{k}" for k in first)  # e.g. readouts by id
            else:
                yield from (f"{key}.{k}" for k in value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            yield from (f"{key}.{k}" for k in value[0])


results = []  # (level, name, message); level is PASS, WARN or FAIL


def report(level, name, message):
    results.append((level, name, message))
    print(f"{level:4}  {name}: {message}", flush=True)


# --- Reading the backup (local folder or Cloud Storage) -----------------------


class Source:
    def __init__(self, local, bucket):
        self.local = local
        if not local:
            from google.cloud import storage

            self.bucket = storage.Client().bucket(bucket)

    def list_sizes(self):
        """{path: size} of everything under backups/, relative to the bucket or folder."""
        if self.local:
            return {
                os.path.relpath(p, self.local).replace(os.sep, "/"): os.path.getsize(p)
                for d, _, files in os.walk(os.path.join(self.local, "backups"))
                for p in (os.path.join(d, f) for f in files)
            }
        return {b.name: b.size for b in self.bucket.list_blobs(prefix="backups/")}

    def open(self, path):
        """Binary stream of a stored object, as stored (still gzipped)."""
        if self.local:
            return open(os.path.join(self.local, path), "rb")
        return self.bucket.blob(path).open("rb")

    def read_text(self, path):
        with self.open(path) as f:
            return f.read().decode()


def pick_run(sizes, vault_prefix, run_id):
    """(run id, {name: path}) of the chosen run; includes partial files, to report them."""
    runs = {}  # run_id -> {name: path}
    for path in sizes:
        parts = path.split("/")
        # backups/<run>/<file>; attached files are one level deeper and skipped here
        if len(parts) != 3 or not parts[2].startswith(vault_prefix):
            continue
        filename = parts[2].removeprefix(vault_prefix)
        if filename.endswith(".done"):
            continue
        name = filename.removesuffix(".json.gz").removesuffix(".zip")
        runs.setdefault(parts[1], {})[name] = path

    if not runs:
        sys.exit("No backups found")
    if run_id:
        if run_id not in runs:
            sys.exit(f"Run {run_id} not found. Runs: {', '.join(sorted(runs))}")
    else:
        # Newest run where every default export is complete (has its marker), so
        # a run that is still in progress isn't picked
        full = [
            r
            for r in sorted(runs)
            if all(
                name in runs[r] and runs[r][name] + ".done" in sizes
                for name in DEFAULT_EXPORTS
            )
        ]
        run_id = full[-1] if full else sorted(runs)[-1]
    return run_id, runs[run_id]


# --- Checks ------------------------------------------------------------------


def _id(value):
    """A reference is either a plain id or an object with an "id"."""
    return value.get("id") if isinstance(value, dict) else value


def check_refs(name, field, refs, known, target):
    """refs: Counter of referenced ids. known: ids of the target export, or None."""
    if known is None:
        report(
            "WARN",
            name,
            f"{field} -> {target}: not checked ({target} missing or unreadable)",
        )
        return
    missing = [i for i in refs if i not in known]
    if missing:
        rows = sum(refs[i] for i in missing)
        report(
            "FAIL",
            name,
            f"{rows:,} records point to {len(missing):,} {target} not in the backup"
            f" (e.g. {', '.join(map(str, missing[:MAX_EXAMPLES]))})",
        )
    else:
        report("PASS", name, f"all {field} references found in {target}")


def header_count(source, path):
    """The export's own record count; it comes first: {"count":N,"objects":[...]}."""
    with source.open(path) as raw, gzip.GzipFile(fileobj=raw) as gz:
        head = gz.read(200).decode(errors="replace")
    match = re.match(r'\s*\{\s*"count"\s*:\s*(\d+)', head)
    return int(match.group(1)) if match else None


def verify_bulk(source, name, path, marker_bytes, ids, sizes):
    """Check one {"count":N,"objects":[...]} export; fills ids[name] with its record ids.

    Returns the number of records.
    """
    count = header_count(source, path)
    seen = Counter()
    refs = {}  # field -> Counter of referenced ids
    no_structure = no_values = 0
    bad_files = []  # files list: (id, problem)
    fields = Counter()  # field name -> number of records that have it

    def ref(field, value):
        value = _id(value)
        if value is not None:
            refs.setdefault(field, Counter())[value] += 1

    # Streamed: the big exports are over 1 GB uncompressed
    with source.open(path) as raw, gzip.GzipFile(fileobj=raw) as gz:
        for item in ijson.items(gz, "objects.item", use_float=True):
            seen[item.get("id")] += 1
            fields.update(field_names(item))
            if name == "molecules":
                if not (item.get("smiles") or item.get("molfile")):
                    no_structure += 1
                for collection in item.get("collections") or []:
                    ref("collection", collection)
            elif name == "batches":
                ref("molecule", item.get("molecule"))
            elif name == "runs":
                ref("protocol", item.get("protocol"))
            elif name == "readout_rows":
                for field in ("batch", "protocol", "run"):
                    ref(field, item.get(field))
                ref("plate", (item.get("well") or {}).get("plate"))
                if not item.get("readouts"):
                    no_values += 1
            elif name == "files":
                stored = sizes.get(item["object"])
                if stored is None:
                    bad_files.append((item["id"], "missing"))
                elif stored != item["bytes"]:
                    bad_files.append(
                        (item["id"], f"{stored} bytes, expected {item['bytes']}")
                    )
        gz.read()  # the rest after the array, so tell() is the full size
        size = gz.tell()

    records = sum(seen.values())
    if size != marker_bytes:
        report("FAIL", name, f"{_size(size)} read, marker says {_size(marker_bytes)}")
    if count is None:
        report(
            "WARN", name, f"{records:,} records, but no count in the file to compare"
        )
    elif records != count:
        report("FAIL", name, f"{records:,} records, but the export says {count:,}")
    else:
        report(
            "PASS",
            name,
            f"{records:,} records, matches the export's count ({_size(size)})",
        )

    if records:
        gone = [f for f in EXPECTED_FIELDS.get(name, []) if not fields[f]]
        if gone:
            report(
                "FAIL",
                name,
                f"field(s) {', '.join(gone)} missing in every record: CDD may have changed"
                " its export format (restores and exit_kit.py depend on them)",
            )
        elif name in EXPECTED_FIELDS:
            report(
                "PASS",
                name,
                f"all {len(EXPECTED_FIELDS[name])} expected fields present",
            )

    dups = [i for i, n in seen.items() if n > 1]
    if dups:
        report(
            "FAIL", name, f"{len(dups):,} duplicate ids (e.g. {dups[:MAX_EXAMPLES]})"
        )
    if no_structure:
        report(
            "WARN",
            name,
            f"{no_structure:,} molecules have no structure (SMILES/molfile)",
        )
    if no_values:
        report("WARN", name, f"{no_values:,} readout rows have no readout values")
    if name == "files":
        if bad_files:
            examples = ", ".join(f"{i} {p}" for i, p in bad_files[:MAX_EXAMPLES])
            report(
                "FAIL",
                name,
                f"{len(bad_files):,} files not stored correctly ({examples})",
            )
        else:
            report("PASS", name, f"all {records:,} listed files stored, sizes match")

    ids[name] = set(seen)
    for field, counter in refs.items():
        target = REF_TARGETS[field]
        check_refs(name, field, counter, ids.get(target), target)
    return records


def verify_reference(source, name, path, marker_bytes, ids):
    """Small reference data: readable, and paged lists complete. Fills ids[name] for lists."""
    with source.open(path) as raw, gzip.GzipFile(fileobj=raw) as gz:
        data = gz.read()
    if len(data) != marker_bytes:
        report(
            "FAIL", name, f"{_size(len(data))} read, marker says {_size(marker_bytes)}"
        )
        return
    parsed = json.loads(data)
    if isinstance(parsed, dict) and isinstance(parsed.get("objects"), list):
        objects, count = parsed["objects"], parsed.get("count")
        if count is not None and len(objects) != count:
            report(
                "FAIL",
                name,
                f"{len(objects):,} records, but the response says {count:,}",
            )
            return
    elif isinstance(parsed, list):
        objects = parsed
    else:
        report("PASS", name, f"readable (one object, {_size(len(data))})")
        return
    ids[name] = {o.get("id") for o in objects if isinstance(o, dict)}
    report("PASS", name, f"readable, {len(objects):,} records")


def verify_zip(source, name, path, marker_bytes):
    with source.open(path) as f:
        data = f.read()  # ELN zips: read fully, zipfile needs random access
    if len(data) != marker_bytes:
        report(
            "FAIL", name, f"{_size(len(data))} read, marker says {_size(marker_bytes)}"
        )
        return
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        bad = z.testzip()
        if bad:
            report("FAIL", name, f"corrupt entry in zip: {bad}")
        else:
            report("PASS", name, f"zip readable, {len(z.namelist()):,} files")


def live_count(session, vault_url, name):
    """CDD's current record count for a bulk export (asks for one record).

    page_size is documented for all of these endpoints (CDD rejects parameters an
    endpoint doesn't support, e.g. on /searches, but that is reference data).
    """
    how, path = EXPORT_SOURCES[name]
    url = f"{vault_url}/{path}"
    if how == "post":
        resp = request_with_retry(session, "POST", url, json={"page_size": 1})
    else:
        resp = request_with_retry(session, "GET", url, params={"page_size": 1})
    return resp.json()["count"]


def check_live(session, vault_url, name, backup_count):
    try:
        live = live_count(session, vault_url, name)
    except Exception as exc:
        report("WARN", name, f"could not get CDD's current count: {exc!r}")
        return
    if backup_count >= live:
        extra = (
            f" ({backup_count - live:,} deleted since?)" if backup_count > live else ""
        )
        report(
            "PASS", name, f"backup has {backup_count:,}, CDD has {live:,} now{extra}"
        )
        return
    missing = live - backup_count
    level = "WARN" if missing <= live * LIVE_COUNT_TOLERANCE else "FAIL"
    report(
        level,
        name,
        f"backup has {missing:,} fewer than CDD now ({backup_count:,} vs {live:,}):"
        " added since the backup, or missing",
    )


def check_age(run_id, max_age_days):
    taken = datetime.strptime(run_id, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - taken).total_seconds() / 86400
    level = "FAIL" if age_days > max_age_days else "PASS"
    report(
        level,
        "age",
        f"newest complete backup is {age_days:.1f} days old (limit {max_age_days:g})",
    )


def main():
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bucket", default=env("BUCKET_NAME"))
    parser.add_argument("--local", default=env("LOCAL_DIR"))
    parser.add_argument("--vault", default=env("CDD_VAULT_ID"))
    parser.add_argument("--run", help="run id, e.g. 2026-10-15T121000Z")
    parser.add_argument(
        "--max-age-days", type=float, default=float(env("MAX_AGE_DAYS") or 0) or None
    )
    args = parser.parse_args()
    if not (args.bucket or args.local) or not args.vault:
        parser.error("set --bucket (or --local) and --vault, or their env variables")

    source = Source(args.local, args.bucket)
    sizes = source.list_sizes()
    run_id, files = pick_run(sizes, f"vault-{args.vault}-", args.run)
    print(f"Verifying run {run_id} of vault {args.vault} ({len(files)} files)\n")

    if args.max_age_days and not args.run:
        check_age(run_id, args.max_age_days)
    for name in [n for n in DEFAULT_EXPORTS if n not in files]:
        report("WARN", name, "not in this run")

    session = None
    if env("CDD_API_TOKEN"):
        session = requests.Session()
        session.headers.update({"X-CDD-Token": env("CDD_API_TOKEN")})
    else:
        report("WARN", "live", "no CDD_API_TOKEN: counts not compared with CDD")
    vault_url = f"{BASE_URL}/vaults/{args.vault}"

    ids = {}
    order = [n for n in CHECK_ORDER if n in files] + [
        n for n in EXPORT_SOURCES if n in files and n not in CHECK_ORDER
    ]
    for name in order:
        path = files[name]
        if path + ".done" not in sizes:
            report("FAIL", name, "no .done marker: partial file")
            continue
        try:
            marker_bytes = int(source.read_text(path + ".done").split()[0])
            how = EXPORT_SOURCES[name][0]
            if name in ZIP_EXPORTS:
                verify_zip(source, name, path, marker_bytes)
            elif how == "sync":
                verify_reference(source, name, path, marker_bytes, ids)
            else:
                records = verify_bulk(source, name, path, marker_bytes, ids, sizes)
                if session and how in ("post", "get"):
                    check_live(session, vault_url, name, records)
        except Exception as exc:  # unreadable file: report and go on with the rest
            report("FAIL", name, f"could not read: {exc!r}")

    totals = Counter(level for level, _, _ in results)
    print(
        f"\n{run_id}: {totals['PASS']} passed, {totals['WARN']} warnings, {totals['FAIL']} failed"
    )
    sys.exit(1 if totals["FAIL"] else 0)


if __name__ == "__main__":
    main()
