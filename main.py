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
fully stored, a small "<name>.json.gz.done" marker is written. A backup file WITHOUT its .done marker
is a partial: usable for salvage only. The job needs create and read permission
on the bucket (roles/storage.objectCreator + roles/storage.objectViewer), no delete.

At the end of every run one JSON summary line (event "backup_summary") is logged:
size per export compared to the previous complete backup, with a WARNING status
if anything got smaller or failed. A Cloud Monitoring alert emails it.

Note: the backup only contains what the token's user can see. Make sure the
backup user is a member of ALL projects in the vault.

API reference: https://docs.collaborativedrug.com/
"""

import glob
import gzip
import json
import logging
import os
import signal
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime

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
}
ADMIN_ONLY = {"users", "eln"}
ZIP_EXPORTS = {"eln"}
DEFAULT_EXPORTS = [n for n in EXPORT_SOURCES if n not in ADMIN_ONLY]


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
def _open_local(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as out:
        yield out
    log.info("Saved locally: %s", path)


@contextmanager
def _open_gcs(bucket_name, object_name):
    from google.cloud import (
        storage,
    )  # imported here so local testing doesn't need GCP libraries

    blob = storage.Client().bucket(bucket_name).blob(object_name)
    with blob.open("wb") as out:
        yield out
    log.info("Uploaded: gs://%s/%s", bucket_name, object_name)


def open_destination(cfg, object_name):
    if cfg.local_dir:
        return _open_local(os.path.join(cfg.local_dir, object_name))
    return _open_gcs(cfg.bucket, object_name)


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


def run_export(session, cfg, name, object_name):
    if EXPORT_SOURCES[name][0] == "sync":
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


def previous_sizes(cfg, run_id):
    """Latest complete backup before this run, per export: {name: (run_id, uncompressed bytes)}.

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

    return {
        name: (prev_run, _read_marker(cfg, path))
        for name, (prev_run, path) in latest.items()
    }


def _size(n):
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def log_summary(cfg, run_id, results, failures):
    """Log one structured summary line; a Cloud Monitoring alert emails it.

    results: {name: (uncompressed bytes, stored bytes)} for successful exports.
    """
    warnings = []
    try:
        previous = previous_sizes(cfg, run_id)
    except Exception:
        log.exception("Could not read previous backup sizes")
        previous = {}
        warnings.append("could not read previous backup sizes")

    lines = []
    for name in cfg.exports:
        if name in failures:
            lines.append(f"{name}: FAILED")
            warnings.append(f"{name} failed")
            continue
        raw, gz = results[name]
        stored_as = "zip" if name in ZIP_EXPORTS else "gz"
        line = f"{name}: {_size(raw)} ({_size(gz)} {stored_as})"
        if name in previous:
            prev_run, prev_raw = previous[name]
            diff = raw - prev_raw
            change = diff / prev_raw * 100 if prev_raw else 0.0
            # Exact bytes too: tiny changes round to 0.0%. Kept short: the alert
            # email has a size limit and there is one line per export
            line += f", {diff:+,} bytes ({change:+.1f}%) vs {prev_run[:10]}"
            if diff < 0:
                warnings.append(
                    f"{name} is {-diff:,} bytes ({-change:.1f}%) smaller"
                    f" than on {prev_run[:10]}"
                )
        else:
            line += ", no previous backup"
        lines.append(line)

    status = "WARNING" if warnings else "OK"
    # Markdown list: the alert email renders markdown, which would merge plain lines
    summary = f"Vault {cfg.vault_id}, run {run_id}: {status}\n\n" + "\n".join(
        [f"- WARNING: {w}" for w in warnings] + [f"- {line}" for line in lines]
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
        ext = ".zip" if name in ZIP_EXPORTS else ".json.gz"
        object_name = f"backups/{run_id}/vault-{cfg.vault_id}-{name}{ext}"
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
