# CDD Vault backup

Workday backup (Mon-Fri 02:00 Asia/Jerusalem, i.e. after each Sun-Thu workday) of Collaborative Drug Discovery (CDD) Vault data to Google Cloud Storage.
A Python script (`main.py`) runs as a **Cloud Run Job**, triggered by **Cloud Scheduler**,
and streams CDD data (async exports plus small reference lists, gzip-compressed JSON, plus attached/source files)
into a bucket. A second job (`verify_backup.py`, Mon-Fri 06:00) checks the newest backup can be restored, and a
third (`exit_kit.py`, the 1st of each month 08:00) checks it still converts into standard formats.
Failures email the owner via Cloud Monitoring alerts.

## Files

| File | Purpose |
|---|---|
| `README.md` | For people operating this (not Claude): emails and what to do, new computer, access, routine tasks. **Update it when commands, schedules, emails or setup steps change** |
| `main.py` | The backup: start export -> poll -> stream to GCS, gzip on the fly; then reference data and files; then the summary |
| `verify_backup.py` | Restore check of a stored run (see "Verification" below). Imports the export table and helpers from `main.py` |
| `exit_kit.py` | Converts a stored run into standard formats (SDF, CSV per protocol, plates, files, JSON) in `exit_kit/<run>/` (git/gcloud/docker-ignored: research data). For "CDD is gone" and as the basis of the restore drill. Runs locally (`make exit-kit`) and monthly as the Cloud Run job `cdd-backup-exit-kit` (`--out /tmp/exit-kit`: the app folder isn't writable, /tmp is in memory, so 8Gi + 2 CPU; no CDD token; output discarded on purpose: the JSON is the master copy). On success in Cloud Run (`CLOUD_RUN_JOB` set) it logs one JSON line (`event="exit_kit_summary"`, counts as a markdown table) that `alerts/exit-kit-summary.json` emails as "CDD exit kit OK": the monthly sign that the schedule still runs. Reuses `Source`/`pick_run` from `verify_backup.py`. Ends with `check_output`: reads its output back with plain CSV/SDF parsing and compares with the backup (counts, ~0.2% sampled values/links, plate labels, file sizes); exit 1 on any difference. Keep that check independent of the writing code |
| `check_structures.py` | Optional RDKit check of `compounds.sdf` (InChIKey vs CDD's), run by `exit_kit.py --check-structures` via `uv run --with rdkit` (RDKit is not a project dependency). Baseline 2026-10-16: 53 of 253,788 differ (stereo/isotope, exotic) |
| `Dockerfile` | Required. Without it, `--source .` falls back to buildpacks, which start a web server and the job dies with exit code 127. Runs as non-root user `app` |
| `requirements.txt` | `google-cloud-storage`, `requests`, `ijson` (streaming JSON parser) for the image; no Secret Manager client: the token is injected as an env var. Keep in sync with `pyproject.toml` (uv) |
| `Makefile` | Setup, deploy, run, logs, schedule, alerts. `make help` lists targets |
| `config.mk` | Your settings (REGION, BUCKET_NAME, CDD_VAULT_ID, ...). **Not committed.** See `config.mk.example`. Make evaluates it, so treat it as code |
| `alerts/*.json` | Cloud Monitoring alert policy templates. `__PLACEHOLDERS__` are filled in by `make alerts` |
| `registry-cleanup.json` | Artifact Registry cleanup policy for `cloud-run-source-deploy`, applied by `make registry` (part of `setup`): keep the newest 5 images per package, delete older than 30 days. `make setup` creates that repository so the first `make deploy` doesn't stop at a y/n prompt. The repo is shared with any other source deploys in the project/region. Applied to the test project 2026-10-22 (works; not a dry run) |
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
  - **Files** (export `files`, always last): there is no endpoint that lists files, so their ids come from the
    `source_files`/`attached_files` fields in the stored molecules, batches, protocols and runs. **Every run downloads
    all of them** (`GET /files/{id}` returns JSON with base64 `contents`) into `<run>/files/<id>-<name>`, so each run
    folder is self-contained and one lifecycle rule on `backups/` can delete old runs. `vault-<id>-files.json.gz`
    lists them (id, name, object, bytes) and is written, with its marker, only after all files are stored.
    Each file is decoded in memory (~3x its size; job memory 2Gi). A file too big fails the job loudly (out of memory).

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
  with a **markdown table** (renders in the alert email; tested 2026-10-22): per export the record count and its change,
  size, stored size and size change, vs the previous complete backup (the newest earlier `.done` marker). Nothing extra is
  stored: `record_count` reads the count back from the stored files (bulk exports start with `{"count":N`; small reference
  data is read whole), for this run and the previous one. Status is `WARNING` if an export has **fewer records**, failed,
  or previous sizes couldn't be read; a smaller size with the same records is only shown. Exports without a count
  (`fields`, a single object; the ELN zip) fall back to warning on a smaller size. The log-based alert
  `alerts/backup-summary.json` emails it. A warning doesn't change the exit code; a failed run sends both the failed alert
  and the summary.
- **Poll tolerance:** up to 5 consecutive failed status checks are tolerated; unexpected statuses show up in the logs.
- **Deploy with `--max-retries 0`** so a failed run doesn't immediately start a second export behind the first. The next scheduled run is the retry.
- **`--task-timeout` must exceed** (number of exports x 2 h) plus download time. Default in the Makefile is 12h.

## Verification (`verify_backup.py`, job `cdd-backup-verify`)

Reads a stored run (default: the newest run with every default export complete) and checks: markers present,
files parse to the end, sizes match markers, record count == the export's own `count`, no duplicate ids,
links (molecule -> collections, batch -> molecule, readout row -> batch/protocol/run/plate, run -> protocol,
files list -> stored files with matching sizes), molecules have structures, readout rows have values.
**Format drift:** `EXPECTED_FIELDS` lists the fields restores and `exit_kit.py` depend on; FAIL if one is absent from
*every* record of an export (CDD changed its format). Update it when `exit_kit.py` starts using a new field.
With `CDD_API_TOKEN` it also compares each bulk export with CDD's current count (`page_size=1` request; more than
1% fewer in the backup = FAIL). With `MAX_AGE_DAYS` (job: 2) it fails if the newest complete backup is older, which
also catches a paused or broken backup schedule. Exit 1 on any FAIL -> the job's failed alert. No email on success.
Same image and service account as the backup; reads the bucket with the storage library (locally that needs
`gcloud auth application-default login` once). Not covered: a real restore into CDD (plan: a yearly drill into a test
vault), data the token's user can't see, chemistry-level structure checks (RDKit: only in `exit_kit.py --check-structures`).

## Commands

```bash
make help            # list targets
make config          # settings in use (which project!). Every cloud target first runs check-config:
                     # refuses config.mk.example values, prints ">> Project ..., bucket ..."
make setup           # APIs, bucket, service account, token (asked only if no secret exists yet), IAM,
                     # builder role (idempotent). `iam` needs the secret, so it comes after the token step
make secret          # prompts for the CDD token and stores it as a new secret version (to replace the token)
make deploy          # build from source and deploy the job (no schedule)
make run             # execute the job once and wait
make logs            # last week of logs of all jobs, newest first, with the job name
                     # (JOB=backup|verify|exit-kit, LOG_LIMIT=100, LOG_DAYS=7)
make list-backups    # list objects in the bucket
make schedule        # Cloud Scheduler trigger, Mon-Fri 02:00 (do this only after a good manual run).
                     # Its name stays cdd-backup-weekly: the trigger-failed alert filters on that name
make alerts          # email channel + eight alert policies: backup failed / trigger failed / summary,
                     # exit-kit failed / exit-kit trigger failed / exit-kit OK,
                     # verify failed / verify trigger failed (needs ALERT_EMAIL; skips existing ones)
make deploy-verify   # build and deploy the verification job
make run-verify      # execute the verification job once and wait
make schedule-verify # its trigger, Mon-Fri 06:00 (4 h after the backup starts)
make deploy-exit-kit / run-exit-kit / schedule-exit-kit   # the monthly exit-kit check (1st, 08:00)
make logs JOB=exit-kit   # looks back 35 days by default (it runs monthly)
make verify          # run the verification locally (RUN=<run id> for a specific run)
make exit-kit        # convert a run to SDF/CSV/files in ./exit_kit/<run>, then self-check
                     # (RUN=<run id>; CHECK_STRUCTURES=1 adds RDKit; local only)
make local           # run main.py locally into ./backups (needs CDD_API_TOKEN exported)
make lint            # ruff check + ruff format --check (Ruff pinned in pyproject's dev group)
# verify, exit-kit, local and lint run through `uv run`: no venv activation needed
```

Local test without GCP: `CDD_API_TOKEN=... CDD_VAULT_ID=... EXPORTS=molecules LOCAL_OUTPUT_DIR=./backups python main.py`

## Code conventions

- Python 3.14 (uses `datetime.UTC`, parenthesized `with`). Lint/format with Ruff.
- Keep `main.py` a single simple script: no classes beyond the `Config` dataclass, no storage abstraction, no retry library.
  `verify_backup.py` is separate (it has a small `Source` reader class) and reuses `main.py`'s table and helpers.
- Config comes only from environment variables. The token is never logged and never put in files or command lines.

## GCP identities (three different ones, easy to confuse)

1. **You** (deployer): needs `roles/iam.serviceAccountUser` on `cdd-backup-sa` to deploy a job that runs as it.
2. **Default compute service account** (`<project-number>-compute@developer.gserviceaccount.com`): does the **build** for `--source` deploys; needs `roles/run.builder` (project-wide, the broadest grant here).
3. **`cdd-backup-sa`** (runtime of **all three** jobs): `secretmanager.secretAccessor` on the one secret, `storage.objectCreator` + `storage.objectViewer` on the one bucket. Nothing else. (The exit-kit job isn't given the token.)

Plus `cdd-backup-scheduler-sa` with `roles/run.invoker` on the three jobs only.

## Gotchas already hit

- No Dockerfile -> buildpacks -> exit 127.
- `--set-env-vars` splits on commas; `EXPORTS=a,b` needs a custom separator (`^@^...`, already in the Makefile).
- An `EXPORTS` left in a shell once deployed a molecules-only backup (Make's `?=` takes environment variables).
  The Makefile now ignores `EXPORTS` from the environment, with a warning.
- The README once had `make secret` after `make setup`, but `iam` needs the secret: setup failed on a new project.
  `setup` now creates the secret itself (`secret-if-missing`) before `iam`.
- Bucket location can't be changed after creation. Keep bucket and job in a region your data-residency rules allow.
- CDD returns **404** (not 400) for unsupported query parameters, e.g. `page_size` on `/searches`. Only send documented parameters.
- Placeholder text left in a command (e.g. literal `PROJECT_ID`) produces misleading permission errors.
- The backup only contains what the token's user can see: that user must be a member of **all** projects in the vault.
- **Plate wells count from 0** in CDD (`row`/`col`: a 1536-well plate is rows 0-31, cols 0-47). Row 0, col 0 = A1.
- Some readout values are `null` (e.g. an IC50 CDD couldn't fit): an empty cell, not "None".
- Some protocols have no `readout_definitions` key at all: always `.get(..., [])`.

## Unverified / open items

- Full default run confirmed on Cloud Run 2026-10-15 (~75 min, ~280 MB stored; readout_rows alone ~53 min).
  All exports worked except `searches`: CDD answers **404 "Illegal parameter(s): page_size"** to any parameter an
  endpoint doesn't support. Fixed: reference GETs now send no parameters unless the response is paged; confirmed on Cloud Run.
  `page_size=1` confirmed on the molecules/batches/readout_rows `/query` POSTs (used by the live count check).
  Files backup confirmed 2026-10-15 (436 files, 131.6 MB, ~5 min; run ~80 min in total, ~410 MB stored per run).
  A referenced file CDD can't return would fail `files` every run. Verification job confirmed 2026-10-15
  (`cdd-backup-verify-8csjr`: 36 passed, 0 warnings; live counts equal). Still only mock-tested: paging of
  reference data (no list was big enough) and the ELN zip.
  **Not yet seen in practice: a cancel (DELETE) returning 2xx.**
- Summary alert verified 2026-10-15 (OK and WARNING emails received; subject is
  `[ALERT - No severity] CDD backup <status>`). Summaries of ~1.3 KB (list) and ~1.5 KB (19-row fixed-width table,
  test 2026-10-22) arrived complete; the exact limit is still unknown, so keep rows short. The markdown table
  (~1.2-1.3 KB for 19 exports) renders well in the email.
- The "failed" alerts send **two** emails per failure: one when it opens, and an "alert recovered" one about 10 minutes
  later when the next metric window shows no new failure. Kept on purpose (no `notificationPrompts: ["OPENED"]`).
- Verify-failed alert verified 2026-10-15 (`cdd-backup-verify-z9s85`, run with `MAX_AGE_DAYS=0.001`): both emails arrived.
- Exit-kit job confirmed on the test project 2026-10-24 (`cdd-backup-exit-kit-9b4kk`: ~5.5 min, all 9 checks passed,
  439 files). Its failed alert verified 2026-10-24 (`cdd-backup-exit-kit-qt2kc`, run with
  `--args "exit_kit.py,--out,/tmp/exit-kit,--run,doesnotexist"`: exit 1, email arrived). "CDD exit kit OK" email
  verified 2026-10-24 (markdown table renders). Its schedule isn't set up on the test project.
- Failed-execution alert verified 2026-10-15 with a deliberate failure
  (`gcloud run jobs execute cdd-backup --update-env-vars EXPORTS=doesnotexist`): email "Failed execution on <project>" arrived.
  An unknown export name now stops the run at startup (failed alert, no summary); to test a failed export with a summary,
  use an export the token can't read, e.g. `EXPORTS=users` without admin rights.
- Not yet decided/done: ELN and users (need a Vault Administrator token; add `eln,users` to `EXPORTS`); multiple vaults
  (`CDD_VAULT_IDS`) or a warning for unlisted vaults; a restore drill into a CDD test vault (yearly); a lifecycle rule
  deleting old runs under `backups/` (a retention policy is already set on the bucket); Data Access audit logs;
  disabling old secret versions after token rotation; RDKit structure checks in the verification.
- The backup user sees only **1 project** (`projects` export). Confirm with an admin that the vault has no other
  projects, and add the backup user to every new project, otherwise that data is silently missing.
- Pending small improvement: an explicit `PROJECT_ID` in `config.mk` (the Makefile otherwise defaults to the active gcloud project).
- A paused or deleted Scheduler job produces no error log. The verification's age check (`MAX_AGE_DAYS`) covers the
  backup schedule, and the monthly "exit kit OK" email the exit-kit schedule; nothing covers the verification's own
  schedule (a daily OK email was rejected as noise). Check periodically that it is enabled.

## Rules for Claude working in this repo

- `setup` (and its parts, incl. `registry`), `schedule*`, `alerts`, `secret`, `deploy`, `deploy-verify` and `deploy-exit-kit`
  change real cloud resources (`run`, `run-verify`, `run-exit-kit` and `schedule-run` start real jobs). Show the project and the command, and get confirmation before running them.
- Never print, log, echo or commit the CDD token. Never create service account keys.
- Don't broaden IAM roles to make an error go away; find out which identity is missing which permission first.
- Don't change the `.done` marker convention or add delete permission without discussing it.
- **After any change to `main.py`, `exit_kit.py` or `verify_backup.py`, before committing:** `make lint`, then run the
  exit kit and the verification on a recent real backup (download a run into `./backups/` with
  `gcloud storage cp -r gs://<bucket>/backups/<run> backups/backups/`, then
  `python exit_kit.py --local ./backups --vault <id> --run <run>` and `python verify_backup.py --local ./backups ...`).
  Both must end with 0 failed. The exit kit is code nobody runs for months, so this is when it gets tested.
  When `exit_kit.py` starts reading a new field, add it to `EXPECTED_FIELDS` in `verify_backup.py`.
