# CDD Vault backup

Automatic backup of a [CDD Vault](https://www.collaborativedrug.com/) to a Google Cloud Storage bucket, with a
daily check that the backup is complete and can be read back.

- **Backup** (Cloud Run job `cdd-backup`): every Mon-Fri at 02:00 (Asia/Jerusalem), so each Sun-Thu workday is
  backed up the following night. Takes about 80 minutes.
- **Verification** (Cloud Run job `cdd-backup-verify`): Mon-Fri at 06:00. Reads the newest backup and checks it.
- **Exit-kit check** (Cloud Run job `cdd-backup-exit-kit`): the 1st of each month at 08:00. Converts the newest
  backup into standard files (see "Restoring, or moving away from CDD"), checks them, and throws them away: it
  proves the conversion still works on the current data.
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
| **CDD backup OK** | Backup finished. A table with each export's number of records and size, and the change since the previous backup | Nothing |
| **CDD backup WARNING** | An export failed, has **fewer records** than last time, or couldn't be compared | Read the WARNING lines. Fewer records can be real (records deleted in CDD) or a problem (e.g. the backup user lost access to a project). A smaller size with the same records is shown in the table but isn't a warning |
| **cdd-backup failed** | The backup job failed | Look at the summary email from the same night and `make logs JOB=backup`. The next scheduled run retries |
| **cdd-backup-verify failed** | The newest backup failed a check, or no recent backup exists ("age") | `make logs JOB=verify`, look for FAIL lines |
| **cdd-backup-exit-kit failed** | The monthly conversion into SDF/CSV failed or didn't match the backup. The backup itself is fine | `make logs JOB=exit-kit`; reproduce with `make exit-kit`, fix `exit_kit.py`, `make deploy-exit-kit` |
| **... trigger failed** | Cloud Scheduler couldn't start a job | Check the schedule in the Cloud Console, or re-run `make schedule` / `make schedule-verify` / `make schedule-exit-kit` |
| **... alert recovered** | Sent about 10 minutes after each "failed" email | Nothing (it does not mean the problem is fixed) |

**No email at all for a few days is also a problem**: it can mean the schedules or the alerts stopped. Check the
Cloud Scheduler page now and then.

## Getting started on a new computer

You need access to the Google Cloud project (next section). Then the simplest way is **Google Cloud Shell**
(the `>_` button at the top of the Cloud Console): it already has `gcloud`, `make` and `git`, logged in as you.

```bash
git clone <this repo's URL> && cd <repo folder>
cp config.mk.example config.mk     # then edit config.mk: project, region, bucket, vault id, email
make config                        # shows the settings in use; check the project is the right one
make help                          # lists all commands
make logs                          # recent logs of all jobs, newest first (JOB=backup|verify|exit-kit)
```

**Run `make config` at the start of every session.** It shows which Google Cloud project, bucket and vault the
commands will change, and every command that touches the cloud also prints `>> Project ..., bucket ...` first.
If it says the project is "not in config.mk", set `PROJECT_ID` there: otherwise commands go to whatever project
gcloud happens to be pointed at. Commands refuse to run while config.mk still has the example values.

Everything except local development works with only `gcloud`, `make` and `git`, because the jobs are built and
run in Google Cloud. On your own computer, install the [gcloud CLI](https://cloud.google.com/sdk/docs/install)
and run `gcloud auth login`. Some commands use `gcloud alpha`/`beta`, which gcloud offers to install the first time.

**For local development and the exit kit** (`make verify`, `make exit-kit`, `make lint`, `make local`) you also need
[uv](https://docs.astral.sh/uv/). Nothing else: the first run installs Python 3.14 and the packages by itself, and
no virtual environment needs activating. `make verify` and `make exit-kit` also need
`gcloud auth application-default login` once per computer.

`EXPORTS` set in your shell is ignored (with a warning): put it in config.mk, or on the command line
(`make deploy EXPORTS=molecules`), so a leftover from a test can't change what the nightly backup includes.

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
make config           # right project, bucket, vault and email? Fix config.mk first if not
make setup            # APIs, bucket, image repository, service account, CDD token, permissions
                      # (needs project owner rights).
                      # The first time, it asks you to paste the CDD API token: NOTHING APPEARS while you
                      # paste or type (that's normal), then press Enter. Re-running setup doesn't ask again
make alerts           # the emails (uses ALERT_EMAIL). Before the first run, so you hear about it if it fails
make deploy           # build and deploy the backup job (a few minutes)
make run              # one backup now (~80 min), then check the summary email
make deploy-verify    # build and deploy the verification job
make run-verify       # check that backup (~5 min); `make logs JOB=verify` should end with 0 failed
make deploy-exit-kit  # build and deploy the monthly exit-kit check
make run-exit-kit     # try it once (~5 min); `make logs JOB=exit-kit` should end with "All checks passed."
make schedule         # Mon-Fri 02:00 backups
make schedule-verify  # Mon-Fri 06:00 verifications
make schedule-exit-kit  # 1st of each month, 08:00
```

**`make run` keeps the terminal busy for about 80 minutes.** The backup runs in Google Cloud: closing the terminal
or losing the connection doesn't stop it (`make executions` shows its state, `make logs JOB=backup` its progress).
Don't start a second one while it runs.

Then, in the Cloud Console (bucket > Protection, and bucket > Lifecycle):
- a **retention policy**: nothing in the bucket can be deleted or overwritten before it is N days old, not even by
  an owner. **Don't lock it** until everything works: a locked policy can never be shortened or removed.
- a **lifecycle rule** deleting objects after the number of days of backups you want to keep (longer than the
  retention period; it can't delete anything younger than that anyway).

## Routine tasks

**Replace the CDD token** (e.g. when it expires or the backup user changes): `make secret` and paste the new one.
It is stored as a new version of the same secret; the next run uses it, no redeploy needed. Then disable the old
version in Secret Manager.

**Run a backup or check now:** `make run` / `make run-verify` / `make run-exit-kit`.

**Pause the backups** (e.g. during CDD maintenance): `make schedule-pause`, later `make schedule-resume`.
The verification will fail on "age" while paused; that's expected.

**Change what is backed up:** set `EXPORTS` in `config.mk` (comma-separated names, see `config.mk.example`) and
`make deploy`. With a Vault Administrator token, add `eln,users`.

**Change the schedule:** edit `SCHEDULE` / `VERIFY_SCHEDULE` in `config.mk`, then `make schedule` /
`make schedule-verify`. If backups become less frequent than daily, raise `MAX_AGE_DAYS` and `make deploy-verify`.

**After changing the code:** `make lint`, then `make verify` and `make exit-kit` on a recent backup: both must
end with 0 failed. Then `make deploy`, `make deploy-verify` and `make deploy-exit-kit` (all three jobs use the
same code).

## Restoring, or moving away from CDD

**The exit kit** turns a backup run into standard files that any chemistry or lab-data system can import,
without CDD:

```bash
gcloud auth application-default login   # once per computer (also needs uv, see "Getting started")
make exit-kit                           # newest complete run; or: make exit-kit RUN=2026-10-15T172516Z
```

It writes `exit_kit/<run>/` (git-ignored: **it contains the research data, keep it private**):
`compounds.sdf` (structures with names, ids, InChIKeys), `batches.csv`, one folder per assay protocol with
`readouts.csv` / `runs.csv` / `protocol.json`, `plates.csv`, the original files, and the rest as JSON.
A `README.txt` inside explains every file. A full vault takes about 2 minutes plus the download.

**It checks its own output**: after writing, it reads the files back and compares them with the backup
(record counts for every file, a few thousand sampled values and links, plate labels, file sizes). It ends
with "All checks passed." or exits with an error listing what differs: then don't rely on that output.

The monthly **exit-kit check** job runs exactly this in Google Cloud on the newest backup and discards the
output, so you know before an emergency that the conversion still works on your current data. Its output isn't
kept on purpose: the JSON backup is the master copy, and in an emergency you'd convert the newest backup anyway.

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
with Cloud Run time inside the free allowance. Every deploy stores a new container image (~40-200 MB) in the
`cloud-run-source-deploy` repository; `make setup` gives it a cleanup policy (`registry-cleanup.json`: keep the
newest 5 per job, delete older ones after 30 days). That repository is shared with anything else in the project
deployed from source in the same region, and the policy applies to those images too.

## More detail

`CLAUDE.md` has the technical notes: how each part works and why, what has been tested, and known gaps.
