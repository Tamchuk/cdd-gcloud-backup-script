# CDD Vault backup

Twice-weekly (Sun + Wed 02:00, Asia/Jerusalem) backup of Collaborative Drug Discovery (CDD) Vault data to Google Cloud Storage.
A Python script (`main.py`) runs as a **Cloud Run Job**, triggered by **Cloud Scheduler**,
and streams CDD data (async exports plus small reference lists, gzip-compressed JSON) into a bucket. Failures email the owner
via Cloud Monitoring alerts.

## Files

| File | Purpose |
|---|---|
| `main.py` | The whole script: start export -> poll -> stream to GCS, gzip on the fly |
| `Dockerfile` | Required. Without it, `--source .` falls back to buildpacks, which start a web server and the job dies with exit code 127. Runs as non-root user `app` |
| `requirements.txt` | `google-cloud-storage`, `requests` (no Secret Manager client: the token is injected as an env var) |
| `Makefile` | Setup, deploy, run, logs, schedule, alerts. `make help` lists targets |
| `config.mk` | Your settings (REGION, BUCKET_NAME, CDD_VAULT_ID, ...). **Not committed.** See `config.mk.example`. Make evaluates it, so treat it as code |
| `alerts/*.json` | Cloud Monitoring alert policy templates. `__PLACEHOLDERS__` are filled in by `make alerts` |
| `.dockerignore`, `.gcloudignore` | `.gcloudignore` controls what `--source .` **uploads**; `.dockerignore` only controls the image build context. Keep secrets and `config.mk` out of both |

## How it works (and why)

- **Cloud Run Job, not Function/Service:** batch task, long timeouts (script allows 2 h per export), no HTTP server.
- **One export at a time:** CDD allows 3 concurrent requests per user and 30 queued exports. Sequential exports stay well inside that.
- **CDD API:** base URL is the **EU** instance (`https://eu.collaborativedrug.com/api/v1`, overridable with `CDD_BASE_URL`).
  What is backed up is the `EXPORT_SOURCES` table in `main.py`:
  - **Bulk data** is an async export started with `POST <path> {"async": true}` (the `/query` endpoints: molecules, batches,
    readout_rows, eln) or `GET <path>?async=true` (protocols, runs, plates, inventory_samples, control_layouts),
    then poll `GET /export_progress/{export_id}` until `finished`, then `GET /exports/{export_id}`.
  - **Reference data** (projects, fields, collections, searches, registration systems/forms, mapping templates,
    import parsers, inventory locations, data sets) is one plain GET each, paged when the response has `count`/`objects`.
  - Default `EXPORTS` = everything except the Vault-Administrator-only `eln` (a zip, stored as `.zip`, not gzipped) and `users`.
    Unknown names in `EXPORTS` stop the run before anything is exported.
  - Attached files are not backed up: there is no endpoint that lists them.

  Cancel: `DELETE /vaults/{id}/exports/{export_id}` (**the export's own vault id must be in the URL**).
  API docs (agent-friendly): https://docs.collaborativedrug.com/ (also `llms.txt` / `llms-full.txt` there).
- **Streaming, no temp file:** Cloud Run's writable filesystem is in-memory, so a temp file would count against RAM.
  The download is gzipped and written straight to the blob.
- **Partial uploads are kept, not deleted.** The job's service account has `roles/storage.objectCreator` and
  `roles/storage.objectViewer` (read, for the summary), no delete. A backup is complete only if its `<file>.json.gz.done` marker exists (marker holds the uncompressed byte count).
  Anything without a marker is a partial: salvage only.
- **Truncation check:** JSON must start with `{`/`[` and end with `}`/`]`; the ELN zip must start with `PK` and end with
  its end-of-central-directory record. Otherwise no marker is written and that export fails.
- **Cancel on any failure:** `run_export` cancels the CDD export on error, timeout or SIGTERM so abandoned exports don't queue behind the next run.
  `main()` turns SIGTERM into `KeyboardInterrupt` so `except BaseException` cleanup runs (Cloud Run gives ~10 s before SIGKILL).
- **Fail loudly:** all exports are attempted; if any failed the script exits 1, which marks the Cloud Run execution failed and triggers the email alert.
- **Summary email after every run:** `log_summary` prints one JSON line to stdout (`jsonPayload.event="backup_summary"`)
  with each export's size vs the previous complete backup (the newest earlier `.done` marker; no extra size history is stored).
  Status is `WARNING` if an export got smaller, failed, or previous sizes couldn't be read. The log-based alert
  `alerts/backup-summary.json` emails it. A shrink doesn't change the exit code; a failed run sends both the failed alert and the summary.
- **Poll tolerance:** up to 5 consecutive failed status checks are tolerated; unexpected statuses show up in the logs.
- **Deploy with `--max-retries 0`** so a failed run doesn't immediately start a second export behind the first. The next scheduled run is the retry.
- **`--task-timeout` must exceed** (number of exports x 2 h) plus download time. Default in the Makefile is 12h.

## Commands

```bash
make help            # list targets
make setup           # APIs, bucket, service account, IAM, builder role (idempotent)
make secret          # prompts for the CDD token and stores it in Secret Manager
make deploy          # build from source and deploy the job (no schedule)
make run             # execute the job once and wait
make logs            # last week of job logs
make list-backups    # list objects in the bucket
make schedule        # Cloud Scheduler trigger, Sun + Wed 02:00 (do this only after a good manual run).
                     # Its name stays cdd-backup-weekly: the trigger-failed alert filters on that name
make alerts          # email channel + three alert policies: failed, trigger failed, summary (needs ALERT_EMAIL)
make local           # run main.py locally into ./backups (needs CDD_API_TOKEN exported)
make lint            # ruff check + ruff format --check
```

Local test without GCP: `CDD_API_TOKEN=... CDD_VAULT_ID=... EXPORTS=molecules LOCAL_OUTPUT_DIR=./backups python main.py`

## Code conventions

- Python 3.14 (uses `datetime.UTC`, parenthesized `with`). Lint/format with Ruff.
- Keep it a single simple script: no classes beyond the `Config` dataclass, no storage abstraction, no retry library.
- Config comes only from environment variables. The token is never logged and never put in files or command lines.

## GCP identities (three different ones, easy to confuse)

1. **You** (deployer): needs `roles/iam.serviceAccountUser` on `cdd-backup-sa` to deploy a job that runs as it.
2. **Default compute service account** (`<project-number>-compute@developer.gserviceaccount.com`): does the **build** for `--source` deploys; needs `roles/run.builder` (project-wide, the broadest grant here).
3. **`cdd-backup-sa`** (runtime): `secretmanager.secretAccessor` on the one secret, `storage.objectCreator` + `storage.objectViewer` on the one bucket. Nothing else.

Plus `cdd-backup-scheduler-sa` with `roles/run.invoker` on the job only.

## Gotchas already hit

- No Dockerfile -> buildpacks -> exit 127.
- `--set-env-vars` splits on commas; `EXPORTS=a,b` needs a custom separator (`^@^...`, already in the Makefile).
- Bucket location can't be changed after creation. Keep bucket and job in a region your data-residency rules allow.
- CDD returns **404** (not 400) for unsupported query parameters, e.g. `page_size` on `/searches`. Only send documented parameters.
- Placeholder text left in a command (e.g. literal `PROJECT_ID`) produces misleading permission errors.
- The backup only contains what the token's user can see: that user must be a member of **all** projects in the vault.

## Unverified / open items

- Full default run confirmed on Cloud Run 2026-10-15 (~75 min, ~280 MB stored; readout_rows alone ~53 min).
  All exports worked except `searches`: CDD answers **404 "Illegal parameter(s): page_size"** to any parameter an
  endpoint doesn't support. Fixed: reference GETs now send no parameters unless the response is paged. Not yet re-run on Cloud Run.
  Still only mock-tested: paging of reference data (no list was big enough) and the ELN zip.
  **Not yet seen in practice: a cancel (DELETE) returning 2xx.**
- Summary alert verified 2026-10-15 (OK and WARNING emails received; subject is
  `[ALERT - No severity] CDD backup <status>`). An 18-line (~1.3 KB) summary arrived complete; the exact
  limit is still unknown, so keep the lines short.
- Failed-execution alert verified 2026-10-15 with a deliberate failure
  (`gcloud run jobs execute cdd-backup --update-env-vars EXPORTS=doesnotexist`): email "Failed execution on <project>" arrived.
  An unknown export name now stops the run at startup (failed alert, no summary); to test a failed export with a summary,
  use an export the token can't read, e.g. `EXPORTS=users` without admin rights.
- Not yet decided/done: ELN and users (need a Vault Administrator token; add `eln,users` to `EXPORTS`); attached files; multiple vaults (`CDD_VAULT_IDS`) or a warning for unlisted vaults; a restore test; bucket lifecycle/retention rules (e.g. Nearline after 30 days, delete after N days); Data Access audit logs; disabling old secret versions after token rotation.
- Pending small improvements: `.gcloudignore`, an explicit `PROJECT_ID` in `config.mk` (the Makefile otherwise defaults to the active gcloud project), `.DEFAULT_GOAL := help`.
- A paused or deleted Scheduler job produces no error log, so no alert fires. Check periodically that the schedule is enabled.

## Rules for Claude working in this repo

- `iam`, `builder-role`, `schedule`, `alerts`, `secret` and `deploy` change real cloud resources. Show the project and the command, and get confirmation before running them.
- Never print, log, echo or commit the CDD token. Never create service account keys.
- Don't broaden IAM roles to make an error go away; find out which identity is missing which permission first.
- Don't change the `.done` marker convention or add delete permission without discussing it.
