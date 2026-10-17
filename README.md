# CDD Vault backup

Automatic backup of a [CDD Vault](https://www.collaborativedrug.com/) to a Google Cloud Storage bucket, with a
daily check that the backup is complete and can be read back.

- **Backup** (Cloud Run job `cdd-backup`): every Mon-Fri at 02:00 (Asia/Jerusalem), so each Sun-Thu workday is
  backed up the following night. Takes about 80 minutes.
- **Verification** (Cloud Run job `cdd-backup-verify`): Mon-Fri at 06:00. Reads the newest backup and checks it.
- **Emails**: a summary after every backup, and an alert whenever something fails.

Nothing needs to be done day to day. Read the emails; act when one says FAILED or WARNING.

## What is backed up

| Data | Where in the run folder |
|---|---|
| Molecules, batches, assay results (readout rows), protocols, runs, plates, inventory samples, control layouts | `vault-<id>-<name>.json.gz` |
| Reference data: projects, fields, collections, saved searches, registration systems/forms, mapping templates, import parsers, inventory locations, data sets | `vault-<id>-<name>.json.gz` |
| Attached and source files (e.g. instrument files, registration SDFs) | `files/<file id>-<name>` plus the list `vault-<id>-files.json.gz` |

**Not backed up:** the ELN and the list of users (both need a Vault Administrator token; see "Changing what is
backed up"), and anything the token's CDD user can't see. **That user must be a member of every project in the
vault**: when a new project is created in CDD, add the backup user to it, or that project is silently left out.

Each run gets its own folder, `backups/<date>T<time>Z/` (UTC), containing everything needed for that point in
time. Every file has a small `.done` file next to it once it is complete; **a file without its `.done` is a partial
and should not be trusted**. Old runs are deleted by the bucket's lifecycle rule (set in the Cloud Console).

The `.json.gz` files are gzip-compressed JSON: `gunzip -c file.json.gz | head -c 2000` shows the start. Bulk
exports look like `{"count": N, "objects": [...]}`.

## The emails, and what to do

| Email | Meaning | What to do |
|---|---|---|
| **CDD backup OK** | Backup finished. Lists each export's size and the change since the previous backup | Nothing |
| **CDD backup WARNING** | Something failed, got smaller, or couldn't be compared | Read the WARNING lines. "Smaller" can be real (data deleted in CDD) or a problem (e.g. the backup user lost access to a project) |
| **cdd-backup failed** | The backup job failed | Look at the summary email from the same night and `make logs JOB=backup`. The next scheduled run retries |
| **cdd-backup-verify failed** | The newest backup failed a check, or no recent backup exists ("age") | `make logs JOB=verify`, look for FAIL lines |
| **... trigger failed** | Cloud Scheduler couldn't start a job | Check the schedule in the Cloud Console, or re-run `make schedule` / `make schedule-verify` |
| **... alert recovered** | Sent about 10 minutes after each "failed" email | Nothing (it does not mean the problem is fixed) |

**No email at all for a few days is also a problem**: it can mean the schedules or the alerts stopped. Check the
Cloud Scheduler page now and then.

## Getting started on a new computer

You need access to the Google Cloud project (next section). Then the simplest way is **Google Cloud Shell**
(the `>_` button at the top of the Cloud Console): it already has `gcloud`, `make` and `git`, logged in as you.

```bash
git clone <this repo's URL> && cd <repo folder>
cp config.mk.example config.mk     # then edit config.mk: project, region, bucket, vault id, email
make help                          # lists all commands
make logs                          # recent logs of both jobs
```

Everything except local development works with only `gcloud`, `make` and `git`, because the jobs are built and
run in Google Cloud. On your own computer, install the [gcloud CLI](https://cloud.google.com/sdk/docs/install)
and run `gcloud auth login` and `gcloud config set project <project id>`. Some commands use `gcloud alpha`/`beta`,
which gcloud offers to install the first time.

**For local development** (`make local`, `make verify`, `make lint`) you also need Python 3.14 and
[uv](https://docs.astral.sh/uv/): `uv sync` installs everything (uv can install Python 3.14 too). `make verify`
additionally needs `gcloud auth application-default login` once.

## Access you need

Ask a project owner for these on the Google Cloud project (role names as in the Cloud Console IAM page; if a
command fails with a permission error, the message names the missing permission):

- **To operate** (deploy, run, schedules, logs): Cloud Run Source Developer (`roles/run.sourceDeveloper`),
  Service Account User on `cdd-backup-sa` (`make iam` grants this to whoever runs it), Service Usage Consumer,
  Cloud Scheduler Admin, Logs Viewer.
- **For alerts:** Monitoring Editor. **To rotate the CDD token:** Secret Manager Admin on the `cdd-api-token` secret.
- **To read backups:** Storage Object Viewer on the bucket.
- **First-time setup of a new project** (`make setup`): Owner, or an admin who runs it for you.

You also need the CDD side: which CDD user owns the API token, and an admin who can add that user to projects.

## Setting up from scratch (new project or bucket)

In this order; each step can be re-run safely:

```bash
make setup            # APIs, bucket, service account, permissions (needs project owner rights)
make secret           # paste the CDD API token (it isn't shown or stored anywhere else)
make deploy           # build and deploy the backup job
make run              # one backup now (~80 min); check the summary email
make deploy-verify    # build and deploy the verification job
make run-verify       # check that backup (~5 min); `make logs JOB=verify` should end with 0 failed
make schedule         # Mon-Fri 02:00 backups
make schedule-verify  # Mon-Fri 06:00 verifications
make alerts           # email channel and alert policies (uses ALERT_EMAIL from config.mk)
```

Then, in the Cloud Console, give the bucket a **retention policy** (prevents deleting backups, even by
accident, for N days) and a **lifecycle rule** deleting objects after the number of days you want to keep.

## Routine tasks

**Rotate the CDD token** (e.g. when it expires or the backup user changes): `make secret` and paste the new one.
The next run uses it; no redeploy needed. Then disable the old version in Secret Manager.

**Run a backup or check now:** `make run` / `make run-verify`.

**Pause the backups** (e.g. during CDD maintenance): `make schedule-pause`, later `make schedule-resume`.
The verification will fail on "age" while paused; that's expected.

**Change what is backed up:** set `EXPORTS` in `config.mk` (comma-separated names, see `config.mk.example`) and
`make deploy`. With a Vault Administrator token, add `eln,users`.

**Change the schedule:** edit `SCHEDULE` / `VERIFY_SCHEDULE` in `config.mk`, then `make schedule` /
`make schedule-verify`. If backups become less frequent than daily, raise `MAX_AGE_DAYS` and `make deploy-verify`.

**After changing the code:** `make lint`, then `make verify` and `make exit-kit` on a recent backup: both must
end with 0 failed (the exit kit is rarely used, so this is when it gets tested). Then `make deploy` and
`make deploy-verify` (both jobs use the same code).

## Restoring, or moving away from CDD

**The exit kit** turns a backup run into standard files that any chemistry or lab-data system can import,
without CDD:

```bash
gcloud auth application-default login   # once per computer (needs Python 3.14 + `uv sync`, see above)
make exit-kit                           # newest complete run; or: make exit-kit RUN=2026-10-15T172516Z
```

It writes `exit_kit/<run>/` (git-ignored: **it contains the research data, keep it private**):
`compounds.sdf` (structures with names, ids, InChIKeys), `batches.csv`, one folder per assay protocol with
`readouts.csv` / `runs.csv` / `protocol.json`, `plates.csv`, the original files, and the rest as JSON.
A `README.txt` inside explains every file. A full vault takes about 2 minutes plus the download.

**It checks its own output**: after writing, it reads the files back and compares them with the backup
(record counts for every file, a few thousand sampled values and links, plate labels, file sizes). It ends
with "All checks passed." or exits with an error listing what differs: then don't rely on that output.

`make exit-kit CHECK_STRUCTURES=1` also reads every structure with RDKit, an independent chemistry toolkit
(downloaded on first use; a few more minutes), and lists molecules whose InChIKey differs from CDD's in
`structure_differences.csv`. Expect a small share: on 2026-10-16, 53 of 253,788 (stereo/isotope layers read
differently, or exotic structures such as metal complexes). Look at those by hand before a migration.

If CDD is gone, that folder is what you take to the new system: SDF for the registry, the CSVs for assay
results.

**Back into CDD** (e.g. a deleted project): import `compounds.sdf` and the CSVs through CDD's normal import.
CDD assigns new ids, and protocols must be recreated in the web app first (the API can't create them).
A step-by-step procedure will be in `RESTORE.md` after the first restore drill into a CDD test vault.

**What the backup can't give back:** the ELN and the user list (need a Vault Administrator token), projects
the backup user isn't a member of, CDD's change history, and CDD's own tools (curve fitting, plots, SAR views;
the inputs and the fit results are kept).

## Costs

About $1-2 per month in total for one vault (mostly storage: ~410 MB per run, kept for the lifecycle period),
with Cloud Run time inside the free allowance. Old container images from deploys accumulate in Artifact Registry;
a cleanup policy there keeps that near zero.

## More detail

`CLAUDE.md` has the technical notes: how each part works and why, what has been tested, and known gaps.
