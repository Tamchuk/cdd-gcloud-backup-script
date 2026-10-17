"""
Exit kit: turn one stored backup run into standard files that any chemistry or
lab-data system can import, without CDD.

  python exit_kit.py --local ./backups --vault 1234 [--run RUN_ID] [--out DIR]
  python exit_kit.py --bucket BUCKET --vault 1234 [--run RUN_ID] [--out DIR]

Without --run, the newest complete run is used (as in verify_backup.py). Output
(default ./exit_kit/<run id>/, git-ignored: it holds the research data):

  README.txt            what is where, and how to import it
  compounds.sdf         one record per molecule: structure + name, synonyms, ids, properties
  batches.csv           one row per batch: molecule, salt, formula weight, custom batch fields
  assays/<protocol>/    per protocol:
    readouts.csv          one row per readout row; one column per readout ("Name [unit]")
    runs.csv              run date, person, conditions, fields, source files
    protocol.json         the full definition (readout definitions, calculations, fields)
  plates.csv            one row per well: plate, position, batch, molecule
  files/                the original source/attached files, as "<file id>-<name>"
  reference/            projects, fields, collections, searches, ... as readable JSON

Needs only the Python standard library plus ijson (no chemistry toolkit).
"""

import argparse
import csv
import gzip
import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections import Counter

import ijson

from main import EXPORT_SOURCES
from verify_backup import Source, pick_run

# Molfile for a molecule without a structure (SDF needs a molblock for every record)
EMPTY_MOLFILE = "\n  exit_kit\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END"


def safe(name, limit=60):
    """File-system-safe version of a name."""
    return re.sub(r"[^\w.-]+", "_", name or "").strip("_")[:limit] or "unnamed"


def names(items):
    """'a; b' from a list of {"name": ...} (projects, collections)."""
    return "; ".join(i.get("name", "") for i in items or [])


def well_label(row, col):
    """CDD counts wells from 0: row 0, col 0 -> A1 (rows past Z: AA, AB, ...).

    Confirmed on real data: a 1536-well plate has rows 0-31 and columns 0-47.
    """
    if row is None or col is None:
        return ""
    label, n = "", row + 1
    while n:
        n, rem = divmod(n - 1, 26)
        label = chr(65 + rem) + label
    return f"{label}{col + 1}"


class Kit:
    """Reads one backup run (via verify_backup.Source) and writes the exit kit."""

    def __init__(self, source, files, out):
        self.source, self.files, self.out = source, files, out
        self.counts = Counter()  # what was written, for the final report
        self.molecule_names = {}  # molecule id -> name
        self.batches = {}  # batch id -> (batch name, molecule id)
        self.runs = {}  # run id -> run record
        self.plates = {}  # plate id -> plate name
        self.file_paths = {}  # file id -> path inside the kit

    def records(self, name):
        """Stream the records of a bulk export ({"count": N, "objects": [...]})."""
        with (
            self.source.open(self.files[name]) as raw,
            gzip.GzipFile(fileobj=raw) as gz,
        ):
            yield from ijson.items(gz, "objects.item", use_float=True)

    def load_json(self, name):
        with (
            self.source.open(self.files[name]) as raw,
            gzip.GzipFile(fileobj=raw) as gz,
        ):
            return json.load(gz)

    def path(self, *parts):
        p = os.path.join(self.out, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    # --- Files first, so the tables can point to them -------------------------

    def copy_files(self):
        if "files" not in self.files:
            print("  no files list in this run: files/ left empty")
            return
        for item in self.load_json("files")["objects"]:
            target = f"files/{item['id']}-{safe(item['name'], 100)}"
            with (
                self.source.open(item["object"]) as src,
                open(self.path(target), "wb") as dst,
            ):
                shutil.copyfileobj(src, dst)
            self.file_paths[item["id"]] = target
            self.counts["files"] += 1

    def file_list(self, record):
        """'files/1-a.csv; files/2-b.pdf' for a record's source and attached files."""
        refs = (record.get("source_files") or []) + (record.get("attached_files") or [])
        return "; ".join(
            self.file_paths.get(f["id"], f"(missing {f['id']}) {f.get('name')}")
            for f in refs
        )

    # --- Compounds and batches -------------------------------------------------

    def write_compounds(self):
        tags = [
            ("CDD_MOLECULE_ID", lambda m: m["id"]),
            ("NAME", lambda m: m.get("name")),
            ("SYNONYMS", lambda m: "; ".join(m.get("synonyms") or [])),
            ("INCHIKEY", lambda m: m.get("inchi_key")),
            ("SMILES", lambda m: m.get("smiles")),
            ("FORMULA", lambda m: m.get("formula")),
            ("MOLECULAR_WEIGHT", lambda m: m.get("molecular_weight")),
            ("IUPAC_NAME", lambda m: m.get("iupac_name")),
            ("REGISTRATION_TYPE", lambda m: m.get("registration_type")),
            ("PROJECTS", lambda m: names(m.get("projects"))),
            ("COLLECTIONS", lambda m: names(m.get("collections"))),
            ("OWNER", lambda m: m.get("owner")),
            ("CREATED", lambda m: m.get("created_at")),
            ("SOURCE_FILES", self.file_list),
        ]
        with open(self.path("compounds.sdf"), "w", newline="\n") as sdf:
            for m in self.records("molecules"):
                self.molecule_names[m["id"]] = m.get("name")
                molfile = (m.get("molfile") or EMPTY_MOLFILE).rstrip()
                sdf.write(molfile + "\n")
                for tag, get in tags:
                    value = get(m)
                    if value not in (None, ""):
                        # One line per value: SDF data items end at a blank line
                        sdf.write(f"> <{tag}>\n{str(value).replace(chr(10), ' ')}\n\n")
                sdf.write("$$$$\n")
                self.counts["compounds"] += 1
                if not m.get("molfile"):
                    self.counts["compounds without structure"] += 1

    def write_batches(self):
        # Batch fields differ per vault: collect the column names in a first pass
        field_names = []
        for b in self.records("batches"):
            for key in b.get("batch_fields") or {}:
                if key not in field_names:
                    field_names.append(key)
        columns = [
            "batch_id", "batch_name", "molecule_id", "molecule_name", "salt_name",
            "formula_weight", "projects", "owner", "created", "source_files",
        ] + field_names  # fmt: skip
        with open(self.path("batches.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for b in self.records("batches"):
                molecule = b.get("molecule") or {}
                self.batches[b["id"]] = (b.get("name"), molecule.get("id"))
                fields = b.get("batch_fields") or {}
                writer.writerow(
                    [
                        b["id"],
                        b.get("name"),
                        molecule.get("id"),
                        molecule.get("name"),
                        b.get("salt_name"),
                        b.get("formula_weight"),
                        names(b.get("projects")),
                        b.get("owner"),
                        b.get("created_at"),
                        self.file_list(b),
                    ]  # fmt: skip
                    + [fields.get(k) for k in field_names]
                )
                self.counts["batches"] += 1

    # --- Plates ----------------------------------------------------------------

    def write_plates(self):
        columns = ["plate_id", "plate_name", "well", "row", "col", "batch_id",
                   "batch_name", "molecule_name", "plate_volume", "plate_concentration"]  # fmt: skip
        with open(self.path("plates.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for p in self.records("plates"):
                self.plates[p["id"]] = p.get("name")
                for w in p.get("wells") or []:
                    batch_name, molecule_id = self.batches.get(
                        w.get("batch"), (None, None)
                    )
                    writer.writerow(
                        [
                            p["id"],
                            p.get("name"),
                            well_label(w.get("row"), w.get("col")),
                            w.get("row"),
                            w.get("col"),
                            w.get("batch"),
                            batch_name,
                            self.molecule_names.get(molecule_id),
                            p.get("volume"),
                            p.get("concentration"),
                        ]  # fmt: skip
                    )
                    self.counts["plate wells"] += 1
                self.counts["plates"] += 1

    # --- Assays: protocols, runs, readout rows -----------------------------------

    def write_assays(self):
        protocols = {p["id"]: p for p in self.records("protocols")}
        self.runs = {r["id"]: r for r in self.records("runs")}
        folders, writers, open_files, readout_columns = {}, {}, [], {}

        for pid, p in protocols.items():
            folder = f"assays/{pid}-{safe(p.get('name'))}"
            folders[pid] = folder
            with open(self.path(folder, "protocol.json"), "w") as f:
                json.dump(p, f, indent=2)
            self.write_runs(
                folder, [r for r in self.runs.values() if r.get("protocol") == pid]
            )
            # One column per readout definition, in the protocol's order
            readout_columns[pid] = [
                (
                    d["id"],
                    f"{d['name']} [{d['unit_label']}]"
                    if d.get("unit_label")
                    else d["name"],
                )
                for d in p.get("readout_definitions") or []
            ]
            self.counts["protocols"] += 1

        unknown_readouts = Counter()
        for row in self.records("readout_rows"):
            pid = row.get("protocol")
            if pid not in writers:
                folder = folders.get(pid) or f"assays/{pid}-unknown_protocol"
                f = open(self.path(folder, "readouts.csv"), "w", newline="")
                open_files.append(f)
                writers[pid] = csv.writer(f)
                writers[pid].writerow(
                    ["readout_row_id", "type", "molecule_id", "molecule_name", "batch_id",
                     "batch_name", "run_id", "run_date", "plate_id", "plate_name", "well"]
                    + [label for _, label in readout_columns.get(pid, [])]
                    + ["outliers", "fit_parameters", "other_readouts"]
                )  # fmt: skip
            readouts = row.get("readouts") or {}
            known = {str(i) for i, _ in readout_columns.get(pid, [])}
            other = {k: v for k, v in readouts.items() if k not in known}
            unknown_readouts.update(other.keys())
            well = row.get("well") or {}
            run = self.runs.get(row.get("run")) or {}
            batch_name = self.batches.get(row.get("batch"), (None,))[0]
            writers[pid].writerow(
                [
                    row["id"],
                    row.get("type"),
                    row.get("molecule"),
                    self.molecule_names.get(row.get("molecule")),
                    row.get("batch"),
                    batch_name,
                    row.get("run"),
                    run.get("run_date"),
                    well.get("plate"),
                    self.plates.get(well.get("plate")),
                    well_label(well.get("row"), well.get("col")),
                ]  # fmt: skip
                + [
                    self.readout_value(readouts.get(str(i)))
                    for i, _ in readout_columns.get(pid, [])
                ]
                + [
                    "; ".join(
                        label
                        for i, label in readout_columns.get(pid, [])
                        if (readouts.get(str(i)) or {}).get("outlier")
                    ),  # fmt: skip
                    json.dumps(row["batch_run_fit_parameters"])
                    if row.get("batch_run_fit_parameters")
                    else "",
                    json.dumps(other) if other else "",
                ]
            )
            self.counts["readout rows"] += 1
        for f in open_files:
            f.close()
        if unknown_readouts:
            print(f"  note: {sum(unknown_readouts.values()):,} readout values have no definition in their"
                  " protocol; kept as JSON in the other_readouts column")  # fmt: skip

    @staticmethod
    def readout_value(readout):
        """'>10' style text: the modifier (if any) joined to the value."""
        if not readout:
            return ""
        value = readout.get("value")
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return f"{readout.get('modifier') or ''}{'' if value is None else value}"

    def write_runs(self, folder, runs):
        field_names = []
        for r in runs:
            for key in r.get("run_fields") or {}:
                if key not in field_names:
                    field_names.append(key)
        with open(self.path(folder, "runs.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["run_id", "run_date", "person", "place", "conditions", "project",
                             "created", "source_files"] + field_names)  # fmt: skip
            for r in runs:
                fields = r.get("run_fields") or {}
                writer.writerow(
                    [
                        r["id"],
                        r.get("run_date"),
                        r.get("person"),
                        r.get("place"),
                        r.get("conditions"),
                        (r.get("project") or {}).get("name"),
                        r.get("created_at"),
                        self.file_list(r),
                    ]  # fmt: skip
                    + [fields.get(k) for k in field_names]
                )
                self.counts["runs"] += 1

    # --- Everything else: readable JSON ----------------------------------------

    def write_reference(self):
        done = {
            "molecules",
            "batches",
            "plates",
            "protocols",
            "runs",
            "readout_rows",
            "files",
        }
        for name in self.files:
            if (
                name in done
                or EXPORT_SOURCES.get(name, ("",))[0] == ""
                or self.files[name].endswith(".zip")
            ):
                continue
            with open(self.path("reference", f"{name}.json"), "w") as f:
                json.dump(self.load_json(name), f, indent=2)
            self.counts["reference files"] += 1
        for name, path in self.files.items():
            if path.endswith(".zip"):  # ELN export: copied as it is
                with (
                    self.source.open(path) as src,
                    open(self.path(f"{name}.zip"), "wb") as dst,
                ):
                    shutil.copyfileobj(src, dst)


# --- Self-check: read the output back and compare it with the backup -----------
# Deliberately independent of the writing code above: plain CSV/SDF parsing and
# the expected values taken straight from the backup's JSON.

SAMPLE_RATE = 0.002  # share of records compared field by field (~2,500 readout rows)


def read_sdf(path):
    """Yield (molblock, {tag: value}) for each record of an SDF file."""
    with open(path) as f:
        block, tags, tag = [], {}, None
        in_block = True
        for line in f:
            line = line.rstrip("\n")
            if line == "$$$$":
                yield "\n".join(block), tags
                block, tags, tag, in_block = [], {}, None, True
            elif in_block:
                block.append(line)
                in_block = line != "M  END"
            elif line.startswith("> <"):
                tag = line[3 : line.index(">", 3)]
            elif tag and line:
                tags[tag] = line
            else:
                tag = None


def sampled(where, n, what, bad, bad_what=None):
    """Message for a sample comparison: identical, or which records differ."""
    if not bad:
        return f"{where}: {n:,} sampled {what} identical"
    return f"{where}: {len(bad):,} sampled {bad_what or what} DIFFER from the backup (e.g. ids {bad[:5]})"


def expected_cell(value):
    """How a backup value must look in a CSV cell (None -> empty)."""
    if value is None:
        return ""
    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)


def check_output(kit):
    """Compare the written kit with the backup. Prints PASS/FAIL lines, returns the FAIL count."""
    fails = 0

    def check(ok, what):
        nonlocal fails
        print(f"  {'PASS' if ok else 'FAIL'}  {what}")
        fails += not ok

    rng = random.Random(1)

    # compounds.sdf: one record per molecule; sampled names, InChIKeys, molfiles identical
    expected, total = {}, 0
    for m in kit.records("molecules"):
        total += 1
        if rng.random() < SAMPLE_RATE:
            expected[str(m["id"])] = m
    found, bad = 0, []
    for molblock, tags in read_sdf(kit.path("compounds.sdf")):
        found += 1
        m = expected.get(tags.get("CDD_MOLECULE_ID"))
        if m is None:
            continue
        if (
            tags.get("NAME", "") != (m.get("name") or "")
            or tags.get("INCHIKEY", "") != (m.get("inchi_key") or "")
            or (m.get("molfile") and molblock != m["molfile"].rstrip())
        ):
            bad.append(m["id"])
    check(
        found == total,
        f"compounds.sdf: {found:,} records, backup has {total:,} molecules",
    )
    check(not bad, sampled("compounds.sdf", len(expected), "molecules", bad))

    # batches.csv: one row per batch; sampled molecule links and custom fields identical
    expected, total = {}, 0
    for b in kit.records("batches"):
        total += 1
        if rng.random() < SAMPLE_RATE:
            expected[str(b["id"])] = b
    found, bad = 0, []
    with open(kit.path("batches.csv"), newline="") as f:
        for row in csv.DictReader(f):
            found += 1
            b = expected.get(row["batch_id"])
            if b is None:
                continue
            wanted = {
                "batch_name": b.get("name"),
                "molecule_id": (b.get("molecule") or {}).get("id"),
            }
            wanted.update(b.get("batch_fields") or {})
            if any(
                row.get(k) != expected_cell(v) for k, v in wanted.items()
            ):  # all CSV cells are text
                bad.append(b["id"])
    check(found == total, f"batches.csv: {found:,} rows, backup has {total:,} batches")
    check(not bad, sampled("batches.csv", len(expected), "batches", bad))

    # readouts.csv: rows per protocol equal; sampled values and links identical
    labels, folders = {}, {}
    for p in kit.records("protocols"):
        labels[p["id"]] = {
            str(d["id"]): f"{d['name']} [{d['unit_label']}]"
            if d.get("unit_label")
            else d["name"]
            for d in p.get("readout_definitions") or []
        }
    for entry in os.listdir(kit.path("assays")):
        folders[int(entry.split("-")[0])] = entry
    per_protocol, expected = Counter(), {}
    for r in kit.records("readout_rows"):
        per_protocol[r.get("protocol")] += 1
        if rng.random() < SAMPLE_RATE:
            expected[str(r["id"])] = r
    found, bad, compared = Counter(), [], 0
    for pid, entry in folders.items():
        path = kit.path("assays", entry, "readouts.csv")
        if not os.path.exists(path):
            continue
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                found[pid] += 1
                r = expected.get(row["readout_row_id"])
                if r is None:
                    continue
                # Expected cell text, straight from the backup
                wanted = {
                    "batch_id": expected_cell(r.get("batch")),
                    "run_id": expected_cell(r.get("run")),
                    "molecule_id": expected_cell(r.get("molecule")),
                }
                for def_id, readout in (r.get("readouts") or {}).items():
                    if def_id in labels.get(pid, {}):
                        value = readout.get("value")
                        modifier = readout.get("modifier") or ""
                        wanted[labels[pid][def_id]] = (
                            "" if value is None else modifier + expected_cell(value)
                        )
                compared += len(wanted)
                if any(row.get(k) != v for k, v in wanted.items()):
                    bad.append(r["id"])
    differ = [p for p in per_protocol | found if found[p] != per_protocol[p]]
    check(
        not differ,
        f"readouts.csv: {sum(found.values()):,} rows in {len(found)} protocols, backup has "
        f"{sum(per_protocol.values()):,}"
        + (f"; counts differ for protocols {differ[:5]}" if differ else ""),
    )
    check(not bad, sampled("readouts.csv", compared, "values and links", bad, "rows"))

    # plates.csv: every well, a label for each, no label twice on a plate
    total = sum(len(p.get("wells") or []) for p in kit.records("plates"))
    with open(kit.path("plates.csv"), newline="") as f:
        rows = list(csv.DictReader(f))
    labels_seen = Counter((r["plate_id"], r["well"]) for r in rows)
    check(len(rows) == total, f"plates.csv: {len(rows):,} wells, backup has {total:,}")
    check(all(r["well"] for r in rows) and max(labels_seen.values(), default=1) == 1,
          "plates.csv: every well has a label, none repeats on a plate")  # fmt: skip

    # files/: every listed file present with its size
    if "files" in kit.files:
        listed = kit.load_json("files")["objects"]
        bad = []
        for item in listed:
            path = os.path.join(
                kit.out, f"files/{item['id']}-{safe(item['name'], 100)}"
            )
            if not os.path.exists(path) or os.path.getsize(path) != item["bytes"]:
                bad.append(item["id"])
        check(
            not bad,
            f"files/: {len(listed) - len(bad):,} of {len(listed):,} files present, sizes match",
        )
    return fails


def check_structures(sdf_path):
    """Optional: read every structure with RDKit (an independent chemistry toolkit).

    RDKit isn't a project dependency: it runs through `uv run --with rdkit` in a
    throwaway environment. Writes structure_differences.csv next to the SDF.
    """
    script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "check_structures.py"
    )
    report = os.path.join(os.path.dirname(sdf_path), "structure_differences.csv")
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--with",
        "rdkit",
        "python",
        script,
        sdf_path,
        report,
    ]
    print(
        "  RDKit check (downloads RDKit once, a few minutes for a full vault) ...",
        flush=True,
    )
    return subprocess.run(cmd).returncode


README = """\
CDD Vault data, exported from backup run {run} of vault {vault}.
Made by exit_kit.py: standard formats, readable without CDD.

compounds.sdf
  One record per molecule ({compounds:,}): the structure (molfile) plus data fields
  CDD_MOLECULE_ID, NAME, SYNONYMS, INCHIKEY, SMILES, FORMULA, MOLECULAR_WEIGHT,
  IUPAC_NAME, REGISTRATION_TYPE, PROJECTS, COLLECTIONS, OWNER, CREATED, SOURCE_FILES.
  Import into any registration system or toolkit (RDKit, ChemAxon, Dotmatics,
  Benchling, Signals, ...). Molecules without a structure have an empty molblock.

batches.csv
  One row per batch ({batches:,}), joined to its molecule by molecule_id / molecule_name,
  with all custom batch fields as columns.

assays/<protocol id>-<protocol name>/
  protocol.json  the protocol as CDD stored it: readout definitions (name, unit, data
                 type), calculations (e.g. dose-response fits), protocol fields.
  readouts.csv   one row per readout row; one column per readout definition, titled
                 "Name [unit]". Values with a modifier read like ">10". "outliers" lists
                 readouts flagged as outliers in that row; "fit_parameters" holds curve-fit
                 parameters as JSON. "type" tells detail rows from aggregate rows.
  runs.csv       one row per run: date, person, conditions, run fields, source files.

plates.csv
  One row per well: plate, position, batch, molecule. "well" is A1 style; "row" and
  "col" are CDD's numbers, which count from 0 (row 0, col 0 = A1).

files/
  The original source and attached files, named "<CDD file id>-<file name>". The
  source_files columns above point to them.

reference/
  Everything else as readable JSON: projects, field definitions, collections, saved
  searches, registration forms/systems, inventory, control layouts, ...

Ids are CDD's ids. A new system will assign its own: keep the CDD ids in a field
if you need to trace records back to this export.
"""


def main():
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bucket", default=env("BUCKET_NAME"))
    parser.add_argument("--local", default=env("LOCAL_DIR"))
    parser.add_argument("--vault", default=env("CDD_VAULT_ID"))
    parser.add_argument("--run", help="run id, e.g. 2026-10-15T172516Z")
    parser.add_argument("--out", help="output folder (default ./exit_kit/<run id>)")
    parser.add_argument(
        "--check-structures",
        action="store_true",
        help="also read every structure with RDKit (via uv; a few minutes)",
    )
    args = parser.parse_args()
    if not (args.bucket or args.local) or not args.vault:
        parser.error("set --bucket (or --local) and --vault, or their env variables")

    source = Source(args.local, args.bucket)
    sizes = source.list_sizes()
    run_id, files = pick_run(sizes, f"vault-{args.vault}-", args.run)
    # Only complete files (with their .done marker)
    partial = [n for n, p in files.items() if p + ".done" not in sizes]
    files = {n: p for n, p in files.items() if n not in partial}
    missing = [n for n in ("molecules", "batches", "protocols", "runs", "readout_rows", "plates")
               if n not in files]  # fmt: skip
    if missing:
        sys.exit(
            f"Run {run_id} lacks complete {', '.join(missing)}: pick another with --run"
        )
    if partial:
        print(f"Skipping partial files: {', '.join(partial)}")

    out = args.out or os.path.join("exit_kit", run_id)
    if os.path.exists(out) and os.listdir(out):
        sys.exit(f"{out} already exists and isn't empty")
    print(f"Exporting run {run_id} of vault {args.vault} to {out}/")

    kit = Kit(source, files, out)
    for step, label in [
        (kit.copy_files, "files"),
        (kit.write_compounds, "compounds.sdf"),
        (kit.write_batches, "batches.csv"),
        (kit.write_plates, "plates.csv"),
        (kit.write_assays, "assays/"),
        (kit.write_reference, "reference/"),
    ]:
        print(f"  {label} ...", flush=True)
        step()

    with open(kit.path("README.txt"), "w") as f:
        f.write(README.format(run=run_id, vault=args.vault, **{
            "compounds": kit.counts["compounds"], "batches": kit.counts["batches"]}))  # fmt: skip
    print("\nWritten:")
    for what, n in kit.counts.items():
        print(f"  {what}: {n:,}")

    print("\nChecking the output against the backup:")
    fails = check_output(kit)
    if args.check_structures:
        check_structures(kit.path("compounds.sdf"))
    if fails:
        sys.exit(
            f"\n{fails} check(s) FAILED: don't rely on this output; see the FAIL lines above"
        )
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
