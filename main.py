"""
CDD Vault backup: exports data from Collaborative Drug Discovery and streams it,
gzip-compressed, to Google Cloud Storage (or a local folder when testing).

Config (environment variables):
  CDD_API_TOKEN     required  CDD API token (from Secret Manager in Cloud Run)
  CDD_VAULT_ID      required  Numeric ID of the vault to back up
  BUCKET_NAME       Cloud Storage bucket (omit when testing locally)
  LOCAL_OUTPUT_DIR  if set, save files here instead of Cloud Storage
  EXPORTS           comma-separated list of exports (default: DEFAULT_EXPORTS below,
                    i.e. everything in EXPORT_SOURCES except the admin-only ones)
  CDD_BASE_URL      API base URL (default: https://eu.collaborativedrug.com/api/v1)

Each export is saved as "<name>.json.gz" ("<name>.zip" for ELN). After it is
fully stored, a small "<name>.json.gz.done" marker is written. A backup file
WITHOUT its .done marker is a partial: usable for salvage only.

Attached/source files are downloaded in every run to "<run>/files/<id>-<name>",
so each run folder is self-contained. "<run>/vault-<id>-files.json.gz" lists
them and is written (with its marker) only once all files are stored. The job needs create and read permission
on the bucket (roles/storage.objectCreator + roles/storage.objectViewer), no delete.

At the end of every run one JSON summary line (event "backup_summary") is logged:
size per export compared to the previous complete backup, with a WARNING status
if anything got smaller or failed. A Cloud Monitoring alert emails it.

Note: the backup only contains what the token's user can see. Make sure the
backup user is a member of ALL projects in the vault.

API reference: https://docs.collaborativedrug.com/
"""

import base64
import glob
import gzip
import json
import logging
import os
import posixpath
import re
import signal
import sys
import time
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime

import ijson
import requests

# Direct all log formatters to use UTC (gmtime) instead of local time
logging.Formatter.converter = time.gmtime
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cdd-backup")

BASE_URL = os.environ.get(
    "CDD_BASE_URL", "https://eu.collaborativedrug.com/api/v1"
).rstrip("/")
POLL_INTERVAL_SECONDS = 10  # CDD suggests polling every 5-10 seconds
MAX_POLL_ERRORS = 5  # consecutive failed status checks tolerated before giving up
HEARTBEAT_SECONDS = 5 * 60  # log a progress line at most this often while waiting
EXPORT_TIMEOUT_SECONDS = 2 * 60 * 60  # give up on a single export after 2 hours
CHUNK_SIZE = 8 * 1024 * 1024
FAILED_STATUSES = {"failed", "error", "canceled", "cancelled"}

# What can be backed up: name -> (how, path under /vaults/{id}/).
#   "post": async export via POST <path> {"async": true}
#   "get":  async export via GET <path>?async=true (same thing for endpoints without /query)
#   "sync": small reference data, one plain GET (paged if the response has count/objects)
EXPORT_SOURCES = {
    # Reference data: small, but needed to understand or rebuild the vault
    "projects": ("sync", "projects"),
    "fields": ("sync", "fields"),
    "collections": ("sync", "collections"),
    "searches": ("sync", "searches"),
    "registration_systems": ("sync", "registration_systems"),
    "registration_forms": ("sync", "registration_forms"),
    "mapping_templates": ("sync", "mapping_templates"),
    "import_parsers": ("sync", "import_parsers"),
    "inventory_locations": ("sync", "inventory_locations"),
    "data_sets": ("sync", "data_sets"),
    "users": ("sync", "users"),  # Vault Administrators only
    # Bulk data
    "molecules": ("post", "molecules/query"),
    "batches": ("post", "batches/query"),
    "readout_rows": ("post", "readout_rows/query"),  # assay results
    "protocols": ("get", "protocols"),
    "runs": ("get", "runs"),
    "plates": ("get", "plates"),
    "inventory_samples": ("get", "inventory_samples"),  # needs inventory enabled
    "control_layouts": ("get", "control_layouts"),
    "eln": ("post", "eln/entries/query"),  # a zip; Vault Administrators only
    # Attached and source files referenced by the records above, all of them in
    # every run (so each run folder is self-contained). Always runs last
    "files": ("files", "files"),
}
ADMIN_ONLY = {"users", "eln"}
ZIP_EXPORTS = {"eln"}
DEFAULT_EXPORTS = [n for n in EXPORT_SOURCES if n not in ADMIN_ONLY]
# Exports whose records list "source_files"/"attached_files" ({"id", "name"})
FILE_REF_EXPORTS = ["molecules", "batches", "protocols", "runs"]
FILE_REF_KEYS = {"source_files", "attached_files"}


@dataclass(frozen=True)
class Config:
    token: str
    vault_id: str
    exports: list[str]
    bucket: str | None  # Cloud Storage bucket (cloud runs)
    local_dir: str | None  # local folder (testing); takes priority over bucket

    @property
    def vault_url(self):
        return f"{BASE_URL}/vaults/{self.vault_id}"

    @classmethod
    def from_env(cls):
        token = os.environ.get("CDD_API_TOKEN")
        vault_id = os.environ.get("CDD_VAULT_ID")
        if not token or not vault_id:
            raise RuntimeError("CDD_API_TOKEN and CDD_VAULT_ID must be set")

        bucket = os.environ.get("BUCKET_NAME")
        local_dir = os.environ.get("LOCAL_OUTPUT_DIR")
        if not bucket and not local_dir:
            raise RuntimeError("Set BUCKET_NAME (cloud) or LOCAL_OUTPUT_DIR (testing)")

        # An empty EXPORTS (e.g. from `make local`) also means the default
        exports = os.environ.get("EXPORTS") or ",".join(DEFAULT_EXPORTS)
        exports = [e.strip() for e in exports.split(",") if e.strip()]
        unknown = [e for e in exports if e not in EXPORT_SOURCES]
        if unknown:
            raise RuntimeError(
                f"Unknown export(s): {', '.join(unknown)}. Known: {', '.join(EXPORT_SOURCES)}"
            )
        # files reads the other exports of the run, so it goes last
        exports.sort(key=lambda e: e == "files")
        return cls(token, vault_id, exports, bucket, local_dir)


def _handle_sigterm(signum, frame):
    """Cloud Run sends SIGTERM on timeout/cancel, then SIGKILL ~10s later.

    Raising KeyboardInterrupt (not Exception) lets cleanup code run, and makes
    sure `except Exception` handlers don't swallow it.
    """
    raise KeyboardInterrupt("SIGTERM received")


def _retry_delay(resp, attempt):
    try:
        return int(resp.headers["Retry-After"])
    except KeyError, ValueError:  # missing, or an HTTP date instead of seconds
        return 2**attempt


def request_with_retry(session, method, url, max_attempts=5, **kwargs):
    """HTTP request with exponential backoff on 429 and 5xx responses."""
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.request(method, url, timeout=120, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == max_attempts:
                raise
            wait = 2**attempt
            log.warning(
                "Network error (%s). Retry %d/%d in %ds",
                exc,
                attempt,
                max_attempts,
                wait,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_attempts:
                resp.raise_for_status()
            wait = _retry_delay(resp, attempt)
            log.warning(
                "HTTP %d. Retry %d/%d in %ds",
                resp.status_code,
                attempt,
                max_attempts,
                wait,
            )
            time.sleep(wait)
            continue

        resp.raise_for_status()
        return resp


# --- Output destinations ----------------------------------------------------
# Partial output is deliberately NOT deleted on failure (the job only has create
# permission, and a partial export can still be salvaged). Instead, a backup is
# only complete once download_export writes its ".done" marker.


@contextmanager
def _open_local(path, quiet):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as out:
        yield out
    if not quiet:
        log.info("Saved locally: %s", path)


@contextmanager
def _open_gcs(bucket_name, object_name, quiet):
    from google.cloud import (
        storage,
    )  # imported here so local testing doesn't need GCP libraries

    blob = storage.Client().bucket(bucket_name).blob(object_name)
    with blob.open("wb") as out:
        yield out
    if not quiet:
        log.info("Uploaded: gs://%s/%s", bucket_name, object_name)


def open_destination(cfg, object_name, quiet=False):
    """quiet: don't log each object (attached files: hundreds per run)."""
    if cfg.local_dir:
        return _open_local(os.path.join(cfg.local_dir, object_name), quiet)
    return _open_gcs(cfg.bucket, object_name, quiet)


def open_stored(cfg, object_name):
    """Read back an object this run already stored (binary, as stored)."""
    if cfg.local_dir:
        return open(os.path.join(cfg.local_dir, object_name), "rb")
    from google.cloud import storage

    return storage.Client().bucket(cfg.bucket).blob(object_name).open("rb")


def is_stored(cfg, object_name):
    if cfg.local_dir:
        return os.path.exists(os.path.join(cfg.local_dir, object_name))
    from google.cloud import storage

    return storage.Client().bucket(cfg.bucket).blob(object_name).exists()


# --- CDD export steps -------------------------------------------------------


def start_export(session, cfg, name):
    """Ask CDD to start an async export and return its id."""
    how, path = EXPORT_SOURCES[name]
    log.info("[%s] Starting export", name)
    url = f"{cfg.vault_url}/{path}"
    if how == "post":
        resp = request_with_retry(session, "POST", url, json={"async": True})
    else:
        resp = request_with_retry(session, "GET", url, params={"async": "true"})
    export_id = resp.json()["id"]
    log.info("[%s] Export started (id=%s)", name, export_id)
    return export_id


def wait_for_export(session, cfg, name, export_id):
    """Poll until the export is finished. Raises if it fails or takes too long."""
    started = time.time()
    last_status = None
    last_log = started
    poll_errors = 0

    while True:
        try:
            progress = request_with_retry(
                session, "GET", f"{cfg.vault_url}/export_progress/{export_id}"
            ).json()
            poll_errors = 0
        except requests.RequestException:
            # One flaky status check shouldn't abort an export that may be an hour in
            poll_errors += 1
            if poll_errors >= MAX_POLL_ERRORS:
                raise
            log.warning(
                "[%s] Status check failed (%d/%d), will retry",
                name,
                poll_errors,
                MAX_POLL_ERRORS,
                exc_info=True,
            )
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        status = progress.get("status")

        # Log on status changes (this also surfaces unexpected statuses) plus a
        # periodic heartbeat, not on every poll
        now = time.time()
        if status != last_status or now - last_log >= HEARTBEAT_SECONDS:
            log.info(
                "[%s] Status: %s (%d min elapsed)",
                name,
                status,
                int((now - started) // 60),
            )
            last_status, last_log = status, now

        if status == "finished":
            return
        if status in FAILED_STATUSES:
            raise RuntimeError(f"[{name}] Export failed: {progress}")
        if now - started > EXPORT_TIMEOUT_SECONDS:
            raise TimeoutError(
                f"[{name}] Export did not finish in time (last status: {status})"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def check_complete(name, head, tail):
    """Cheap truncation check on the first and last bytes of the download.

    JSON must start with { or [ and end with } or ]. A zip must start with "PK"
    and end with its end-of-central-directory record.
    """
    if name in ZIP_EXPORTS:
        if not head.startswith(b"PK") or b"PK\x05\x06" not in tail:
            raise RuntimeError(f"[{name}] Export does not look like a complete zip")
        return
    first, last = head.lstrip()[:1], tail.rstrip()[-1:]
    if first not in (b"{", b"[") or last not in (b"}", b"]"):
        raise RuntimeError(
            f"[{name}] Export does not look like complete JSON (starts {first!r}, ends {last!r})"
        )


def write_done_marker(cfg, name, object_name, raw_bytes):
    """Only written once the file is fully stored: its presence means "complete"."""
    with open_destination(cfg, object_name + ".done") as marker:
        marker.write(f"{raw_bytes} bytes uncompressed\n".encode())
    log.info("[%s] Done: %s uncompressed", name, _size(raw_bytes))


def download_export(session, cfg, name, export_id, object_name):
    """Stream the finished export, gzip it on the fly (zips as is), store it, then write the .done marker."""
    log.info("[%s] Downloading", name)
    raw_bytes = 0
    head = tail = b""

    # Exit order is reversed: gzip finishes first (writes its trailer), then the destination closes
    with (
        request_with_retry(
            session, "GET", f"{cfg.vault_url}/exports/{export_id}", stream=True
        ) as dl,
        open_destination(cfg, object_name) as out,
        nullcontext(out)
        if name in ZIP_EXPORTS
        else gzip.GzipFile(fileobj=out, mode="wb") as w,
    ):
        for chunk in dl.iter_content(chunk_size=CHUNK_SIZE):
            w.write(chunk)
            raw_bytes += len(chunk)
            if len(head) < 1024:
                head = (head + chunk[:1024])[:1024]
            tail = (tail + chunk[-1024:])[-1024:]
        check_complete(name, head, tail)

    write_done_marker(cfg, name, object_name, raw_bytes)
    return raw_bytes


def fetch_reference(session, cfg, name, object_name):
    """Small reference data: plain GET (following pages if any), stored as gzipped JSON."""
    _, path = EXPORT_SOURCES[name]
    url = f"{cfg.vault_url}/{path}"
    log.info("[%s] Fetching", name)
    # No parameters on the first call: CDD answers 404 "Illegal parameter(s)" to
    # any parameter an endpoint doesn't support (e.g. page_size on /searches)
    data = request_with_retry(session, "GET", url).json()

    # Paged responses look like {"count": N, "objects": [...]}; fetch the rest
    if isinstance(data, dict) and isinstance(data.get("objects"), list):
        objects = data["objects"]
        while len(objects) < data.get("count", 0):
            page = request_with_retry(
                session,
                "GET",
                url,
                params={"page_size": 1000, "offset": len(objects)},
            ).json()["objects"]
            if not page:
                break
            objects += page
        if len(objects) < data.get("count", 0):
            raise RuntimeError(
                f"[{name}] Got {len(objects)} of {data['count']} records"
            )

    raw = json.dumps(data).encode()
    with (
        open_destination(cfg, object_name) as out,
        gzip.GzipFile(fileobj=out, mode="wb") as gz,
    ):
        gz.write(raw)
    write_done_marker(cfg, name, object_name, len(raw))
    return len(raw)


def cancel_export(session, cfg, export_id):
    """Best-effort cancel of a CDD export. Must never raise: we may be mid-shutdown."""
    try:
        # Short timeout: after SIGTERM we only have ~10 seconds in total.
        # CDD requires the export's own vault id in the URL for DELETE.
        resp = session.delete(f"{cfg.vault_url}/exports/{export_id}", timeout=5)
        log.info("Cancel export %s -> HTTP %s", export_id, resp.status_code)
    except Exception:
        log.warning("Could not cancel export %s", export_id, exc_info=True)


# --- Attached and source files ----------------------------------------------
# There is no "list all files" endpoint: file ids come from the records this run
# already stored. All of them are downloaded every run, into the run's folder.


def referenced_files(cfg, run_dir):
    """{file_id: name} for every source/attached file in this run's stored exports."""
    found = {}

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in FILE_REF_KEYS and isinstance(child, list):
                    for f in child:
                        found[f["id"]] = f.get("name") or str(f["id"])
                else:
                    walk(child)  # e.g. protocols list their runs, which list files
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for name in FILE_REF_EXPORTS:
        object_name = f"{run_dir}/vault-{cfg.vault_id}-{name}.json.gz"
        if not is_stored(cfg, object_name + ".done"):
            # Not exported (or failed) in this run: its files can't be found
            log.warning("[files] %s not in this run, its files are skipped", name)
            continue
        with open_stored(cfg, object_name) as raw, gzip.GzipFile(fileobj=raw) as gz:
            for record in ijson.items(gz, "objects.item", use_float=True):
                walk(record)
    return found


def backup_files(session, cfg, name, object_name):
    """Download every referenced file, then write the list of files and its .done marker.

    The list ("<run>/vault-<id>-files.json.gz") is only written once every file
    is stored, so its marker means "this run's files are complete".
    """
    run_dir = posixpath.dirname(object_name)
    files = referenced_files(cfg, run_dir)
    log.info("[files] %d files referenced, downloading", len(files))

    listing, failed, total = [], [], 0
    for file_id, file_name in sorted(files.items()):
        safe = re.sub(r"[^\w.-]", "_", file_name)[:100]
        stored = f"{run_dir}/files/{file_id}-{safe}"
        try:
            data = request_with_retry(
                session, "GET", f"{cfg.vault_url}/files/{file_id}"
            ).json()
            content = base64.b64decode(data["contents"])
            with open_destination(cfg, stored, quiet=True) as out:
                out.write(content)
        except Exception:
            log.warning("[files] Could not back up file %s", file_id, exc_info=True)
            failed.append(file_id)
            continue
        listing.append(
            {"id": file_id, "name": file_name, "object": stored, "bytes": len(content)}
        )
        total += len(content)

    if failed:
        raise RuntimeError(
            f"[files] {len(failed)} of {len(files)} files failed"
            f" (e.g. {', '.join(map(str, failed[:5]))})"
        )

    # Same shape as the bulk exports, so verify_backup.py checks it the same way
    raw = json.dumps({"count": len(listing), "objects": listing}).encode()
    with (
        open_destination(cfg, object_name) as out,
        gzip.GzipFile(fileobj=out, mode="wb") as gz,
    ):
        gz.write(raw)
    write_done_marker(cfg, name, object_name, len(raw))
    log.info("[files] %d files, %s", len(listing), _size(total))
    return len(raw)


def run_export(session, cfg, name, object_name):
    """Back up one export. Returns its uncompressed size in bytes."""
    how = EXPORT_SOURCES[name][0]
    if how == "files":
        return backup_files(session, cfg, name, object_name)
    if how == "sync":
        return fetch_reference(session, cfg, name, object_name)
    export_id = start_export(session, cfg, name)
    try:
        wait_for_export(session, cfg, name, export_id)
        return download_export(session, cfg, name, export_id, object_name)
    except BaseException:
        # Errors, timeouts and SIGTERM: don't leave an orphaned export in CDD's queue
        cancel_export(session, cfg, export_id)
        raise


# --- Run summary ------------------------------------------------------------
# Compares each export with the previous complete backup, using the .done
# markers that already exist. Nothing extra is stored.


def export_object(cfg, run_id, name):
    """Where an export of a run is stored, e.g. backups/<run>/vault-1234-molecules.json.gz."""
    ext = ".zip" if name in ZIP_EXPORTS else ".json.gz"
    return f"backups/{run_id}/vault-{cfg.vault_id}-{name}{ext}"


def stored_size(cfg, object_name):
    """Compressed size of a stored backup file, in bytes."""
    if cfg.local_dir:
        return os.path.getsize(os.path.join(cfg.local_dir, object_name))
    from google.cloud import storage

    blob = storage.Client().bucket(cfg.bucket).blob(object_name)
    blob.reload()
    return blob.size


def _read_marker(cfg, path):
    """Read a .done marker ("<N> bytes uncompressed") and return N."""
    if cfg.local_dir:
        with open(path) as f:
            text = f.read()
    else:
        from google.cloud import storage

        text = storage.Client().bucket(cfg.bucket).blob(path).download_as_text()
    return int(text.split()[0])


def record_count(cfg, object_name):
    """Number of records in a stored .json.gz export, or None if it has no list.

    Read back from the stored file, so it works the same for this run and the
    previous one. Bulk exports start with their count ({"count":N,...}): only the
    first bytes are read. Small reference data is read whole.
    """
    if not object_name.endswith(".json.gz"):
        return None  # e.g. the ELN zip
    with open_stored(cfg, object_name) as raw, gzip.GzipFile(fileobj=raw) as gz:
        head = gz.read(200)
        match = re.match(rb'\s*\{\s*"count"\s*:\s*(\d+)', head)
        if match:
            return int(match.group(1))
        rest = gz.read(50_000_000)
        if gz.read(1):
            return None  # big and without a leading count: not worth reading whole
        data = json.loads(head + rest)
    if isinstance(data, dict) and isinstance(data.get("objects"), list):
        return len(data["objects"])
    return len(data) if isinstance(data, list) else None  # a single object (fields)


def files_bytes(cfg, list_object):
    """Total size of the attached files, from a run's files list."""
    with open_stored(cfg, list_object) as raw, gzip.GzipFile(fileobj=raw) as gz:
        return sum(f["bytes"] for f in json.load(gz)["objects"])


def previous_sizes(cfg, run_id):
    """Latest complete backup before this run, per export: {name: (run_id, object, uncompressed bytes)}.

    Only runs with a .done marker count, so partials are skipped. Only the
    newest marker per export is read.
    """
    if cfg.local_dir:
        paths = glob.glob(os.path.join(cfg.local_dir, "backups", "*", "*.done"))
    else:
        from google.cloud import storage

        blobs = storage.Client().list_blobs(
            cfg.bucket, prefix="backups/", match_glob="backups/*/*.done"
        )
        paths = [b.name for b in blobs]

    # backups/<run_id>/vault-<vault_id>-<name>.json.gz.done (or .zip.done)
    prefix = f"vault-{cfg.vault_id}-"
    latest = {}  # name -> (run_id, path)
    for path in paths:
        prev_run, filename = path.replace(os.sep, "/").split("/")[-2:]
        if prev_run >= run_id or not filename.startswith(prefix):
            continue
        name = (
            filename.removeprefix(prefix)
            .removesuffix(".done")
            .removesuffix(".json.gz")
            .removesuffix(".zip")
        )
        if name not in latest or prev_run > latest[name][0]:
            latest[name] = (prev_run, path)

    def object_name(path):  # relative to the bucket/folder, as open_stored expects
        path = os.path.relpath(path, cfg.local_dir) if cfg.local_dir else path
        return path.replace(os.sep, "/").removesuffix(".done")

    return {
        name: (prev_run, object_name(path), _read_marker(cfg, path))
        for name, (prev_run, path) in latest.items()
    }


def _size(n):
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def _fmt(count):
    return "" if count is None else f"{count:,}"


def _signed_size(n):
    return ("+" if n >= 0 else "-") + _size(abs(n))


def _count_or_none(cfg, object_name):
    try:
        return record_count(cfg, object_name)
    except Exception:  # a count is only for the report: never fail the summary
        log.warning("Could not count records in %s", object_name, exc_info=True)
        return None


def log_summary(cfg, run_id, results, failures):
    """Log one structured summary line; a Cloud Monitoring alert emails it.

    The summary is a markdown table (the alert email renders it): per export the
    number of records and its change, and the sizes and their change, compared
    with the previous complete backup. WARNING on fewer records or a failure.

    results: {name: (uncompressed bytes, stored bytes)} for successful exports.
    """
    warnings = []
    try:
        previous = previous_sizes(cfg, run_id)
    except Exception:
        log.exception("Could not read previous backup sizes")
        previous = {}
        warnings.append("could not read previous backup sizes")

    rows, compared_with = [], Counter()
    for name in cfg.exports:
        if name in failures:
            rows.append((name, "FAILED", "", "", "", ""))
            warnings.append(f"{name} failed")
            continue
        size, stored = results[name]
        current = export_object(cfg, run_id, name)
        count = _count_or_none(cfg, current)
        if (
            name == "files"
        ):  # its marker is the size of the list: show the files instead
            size = stored = files_bytes(cfg, current)
        if name not in previous:
            rows.append((name, _fmt(count), "new", _size(size), _size(stored), ""))
            continue

        prev_run, prev_object, prev_size = previous[name]
        compared_with[prev_run] += 1
        prev_count = _count_or_none(cfg, prev_object)
        if name == "files":
            prev_size = files_bytes(cfg, prev_object)
        when = prev_run[:10]
        if count is not None and prev_count is not None:
            change = f"{count - prev_count:+,}"
            if count < prev_count:
                warnings.append(
                    f"{name}: {prev_count - count:,} fewer records than on {when}"
                )
        else:  # no record count (e.g. a single object or a zip): judge by size
            change = ""
            if size < prev_size:
                warnings.append(
                    f"{name} is {_size(prev_size - size)} smaller than on {when}"
                )
        rows.append(
            (
                name,
                _fmt(count),
                change,
                _size(size),
                _size(stored),
                _signed_size(size - prev_size),
            )
        )

    status = "WARNING" if warnings else "OK"
    header = f"Vault {cfg.vault_id}, run {run_id}: {status}"
    if compared_with:
        header += f" (compared with {compared_with.most_common(1)[0][0]})"
    # Markdown: the alert email renders lists and tables
    summary = (
        header
        + "\n\n"
        + "".join(f"- WARNING: {w}\n" for w in warnings)
        + ("\n" if warnings else "")
        + "| Export | Records | Change | Size | Stored | Size change |\n"
        + "|:--|--:|--:|--:|--:|--:|\n"
        + "\n".join("| " + " | ".join(row) + " |" for row in rows)
    )
    # A JSON line on stdout becomes a structured log entry (jsonPayload) in Cloud Logging
    entry = {
        "severity": "WARNING" if warnings else "INFO",
        "event": "backup_summary",
        "status": status,
        "summary": summary,
        "message": summary,
    }
    print(json.dumps(entry), flush=True)


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)

    cfg = Config.from_env()
    session = requests.Session()
    session.headers.update({"X-CDD-Token": cfg.token})

    # One folder per run, e.g. backups/2025-06-01T020000Z/vault-1234-molecules.json.gz
    run_id = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    failures = []
    results = {}

    for name in cfg.exports:
        object_name = export_object(cfg, run_id, name)
        try:
            raw = run_export(session, cfg, name, object_name)
            results[name] = (raw, stored_size(cfg, object_name))
        except (
            Exception
        ):  # KeyboardInterrupt (SIGTERM) is NOT caught: it aborts the run
            log.exception("[%s] FAILED", name)
            failures.append(name)  # keep going so one failure doesn't skip the rest

    log_summary(cfg, run_id, results, failures)

    if failures:
        log.error("Backup finished with failures: %s", ", ".join(failures))
        sys.exit(1)  # non-zero exit => Cloud Run marks the execution failed

    log.info("Backup complete: %d export(s)", len(cfg.exports))


if __name__ == "__main__":
    main()
