"""
CDD Vault backup: exports data from Collaborative Drug Discovery and streams it
to Google Cloud Storage (or a local folder when testing).

Config (environment variables):
  CDD_API_TOKEN     required  CDD API token (from Secret Manager in Cloud Run)
  CDD_VAULT_ID      required  Numeric ID of the vault to back up
  BUCKET_NAME       Cloud Storage bucket (omit when testing locally)
  LOCAL_OUTPUT_DIR  if set, save files here instead of Cloud Storage
  EXPORTS           comma-separated list of exports (default: molecules,batches)
  CDD_BASE_URL      API base URL (default: https://eu.collaborativedrug.com/api/v1)

Each successful export is followed by a small "<file>.done" marker object. A
backup file WITHOUT its .done marker is a partial: usable for salvage only.
The job needs only create permission on the bucket (roles/storage.objectCreator).

NOTE: Verify the export endpoints and response fields against the CDD Vault API
docs for your account. The async flow (start -> poll progress -> download) is
the documented pattern, but field names can differ between export types.
"""

import logging
import os
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cdd-backup")

BASE_URL = os.environ.get(
    "CDD_BASE_URL", "https://eu.collaborativedrug.com/api/v1"
).rstrip("/")
POLL_INTERVAL_SECONDS = 15
HEARTBEAT_SECONDS = 5 * 60  # log a progress line at most this often while waiting
EXPORT_TIMEOUT_SECONDS = 2 * 60 * 60  # give up on a single export after 2 hours
MIN_FILE_BYTES = 100  # anything smaller is treated as a failed/empty export
CHUNK_SIZE = 8 * 1024 * 1024


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

        exports = os.environ.get("EXPORTS", "molecules,batches").split(",")
        return cls(
            token,
            vault_id,
            [e.strip() for e in exports if e.strip()],
            bucket,
            local_dir,
        )


def _handle_sigterm(signum, frame):
    """Cloud Run sends SIGTERM on timeout/cancel, then SIGKILL ~10s later.

    Raising KeyboardInterrupt (not Exception) lets cleanup code run, and makes
    sure `except Exception` handlers don't swallow it.
    """
    raise KeyboardInterrupt("SIGTERM received")


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
            wait = int(resp.headers.get("Retry-After", 2**attempt))
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
    log.info("[%s] Starting export", name)
    resp = request_with_retry(
        session,
        "GET",
        f"{cfg.vault_url}/{name}",
        params={
            "async": "true",
            # TODO: REMOVE THIS!
            "modified_after": "2026-06-27T00:00:00Z",
        },
    )
    export_id = resp.json()["id"]
    log.info("[%s] Export started (id=%s)", name, export_id)
    return export_id


def wait_for_export(session, cfg, name, export_id):
    """Poll until the export is finished. Raises if it fails or takes too long."""
    started = time.time()
    last_status = None
    last_log = started

    while True:
        progress = request_with_retry(
            session, "GET", f"{cfg.vault_url}/export_progress/{export_id}"
        ).json()
        status = progress.get("status")

        # Log on status changes plus a periodic heartbeat, not on every poll
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
        if status in ("failed", "error"):
            raise RuntimeError(f"[{name}] Export failed: {progress}")
        if now - started > EXPORT_TIMEOUT_SECONDS:
            raise TimeoutError(f"[{name}] Export did not finish in time")
        time.sleep(POLL_INTERVAL_SECONDS)


def download_export(session, cfg, name, export_id, object_name):
    """Stream the finished export straight to its destination, then write the .done marker."""
    log.info("[%s] Downloading", name)
    total = 0
    with (
        request_with_retry(
            session, "GET", f"{cfg.vault_url}/exports/{export_id}", stream=True
        ) as dl,
        open_destination(cfg, object_name) as out,
    ):
        for chunk in dl.iter_content(chunk_size=CHUNK_SIZE):
            out.write(chunk)
            total += len(chunk)
        if total < MIN_FILE_BYTES:
            raise RuntimeError(f"[{name}] Export suspiciously small ({total} bytes)")

    # Only written once the file is fully stored: its presence means "complete".
    with open_destination(cfg, object_name + ".done") as marker:
        marker.write(f"{total} bytes\n".encode())
    log.info("[%s] Done: %.1f MB", name, total / 1e6)


def cancel_export(session, export_id):
    """Best-effort cancel of a CDD export. Must never raise: we may be mid-shutdown."""
    try:
        # Short timeout: after SIGTERM we only have ~10 seconds in total.
        resp = session.delete(f"{BASE_URL}/exports/{export_id}", timeout=5)
        log.info("Cancel export %s -> HTTP %s", export_id, resp.status_code)
    except Exception:
        log.warning("Could not cancel export %s", export_id, exc_info=True)


def run_export(session, cfg, name, object_name):
    export_id = start_export(session, cfg, name)
    try:
        wait_for_export(session, cfg, name, export_id)
        download_export(session, cfg, name, export_id, object_name)
    except BaseException:
        # Errors, timeouts and SIGTERM: don't leave a queue behind for the next run
        cancel_export(session, export_id)
        raise


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)

    cfg = Config.from_env()
    session = requests.Session()
    session.headers.update({"X-CDD-Token": cfg.token})

    # One folder per run, e.g. backups/2025-06-01T020000Z/vault-1234-molecules.gz
    run_id = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    failures = []

    for name in cfg.exports:
        object_name = f"backups/{run_id}/vault-{cfg.vault_id}-{name}.gz"
        try:
            run_export(session, cfg, name, object_name)
        except (
            Exception
        ):  # KeyboardInterrupt (SIGTERM) is NOT caught: it aborts the run
            log.exception("[%s] FAILED", name)
            failures.append(name)  # keep going so one failure doesn't skip the rest

    if failures:
        log.error("Backup finished with failures: %s", ", ".join(failures))
        sys.exit(1)  # non-zero exit => Cloud Run marks the execution failed

    log.info("Backup complete: %d export(s)", len(cfg.exports))


if __name__ == "__main__":
    main()
