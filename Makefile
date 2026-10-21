# Makefile for the CDD backup Cloud Run job. Run `make help` for the targets.
# Put your settings in config.mk (see config.mk.example), or pass them on the
# command line: make deploy REGION=europe-west1 ...
# No secrets live here: the CDD token is stored in Secret Manager (`make setup` asks for it).

# An EXPORTS variable left in your shell (e.g. from a test) would silently win over the
# default "back up everything": ignore it. config.mk and `make deploy EXPORTS=...` still work.
ifeq ($(origin EXPORTS),environment)
$(warning Ignoring EXPORTS=$(EXPORTS) from your shell; set it in config.mk if you want it)
undefine EXPORTS
endif

-include config.mk

# Before the defaults below: was PROJECT_ID set in config.mk (or on the command line)?
PROJECT_ID_SOURCE := $(origin PROJECT_ID)

SHELL := /bin/bash
.DEFAULT_GOAL := help
.NOTPARALLEL:

PROJECT_ID    ?= $(shell gcloud config get-value project 2>/dev/null)
REGION        ?=
BUCKET_NAME   ?=
CDD_VAULT_ID  ?=
EXPORTS       ?=# empty = everything (DEFAULT_EXPORTS in main.py)
SECRET_NAME   ?= cdd-api-token
JOB_NAME      ?= cdd-backup
SA_NAME       ?= cdd-backup-sa
TASK_TIMEOUT  ?= 12h# ~8 bulk exports, each allowed up to 2 h by main.py
MEMORY        ?= 2Gi# attached files are decoded in memory, one at a time
# The verification job (verify_backup.py): same image, runs after the backup
VERIFY_JOB_NAME ?= $(JOB_NAME)-verify
MAX_AGE_DAYS    ?= 2# fail if the newest complete backup is older (Mon after a failed Mon backup: Fri's is ~3 days)
VERIFY_MEMORY   ?= 2Gi# record ids kept for the link checks (grows with the vault)

GCLOUD          = gcloud --project $(PROJECT_ID)
SA              = $(SA_NAME)@$(PROJECT_ID).iam.gserviceaccount.com
PROJECT_NUMBER  = $(shell gcloud projects describe $(PROJECT_ID) --format='value(projectNumber)')
COMPUTE_SA      = $(PROJECT_NUMBER)-compute@developer.gserviceaccount.com
ME              = $(shell gcloud config get-value account 2>/dev/null)

.PHONY: help config check-config setup apis bucket service-account secret secret-if-missing iam builder-role \
        deploy deploy-verify run run-verify executions logs list-backups local verify exit-kit lint \
        schedule schedule-verify schedule-run schedule-pause schedule-resume alerts

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# `make require-REGION` fails with a clear message if REGION is empty.
require-%:
	@$(if $($*),:,{ echo "Missing setting: $* (set it in config.mk or on the command line)" >&2; exit 1; })

# Values from config.mk.example that must be replaced before anything touches the cloud
EXAMPLE_VALUES := my-gcp-project-id my-org-cdd-backups 1234 backups-owner@example.com you@example.com

# Runs before every cloud command: stops on example values, and says which project and
# bucket are about to be used (there may be a test and a real one).
check-config: require-PROJECT_ID
	@bad=""; for v in $(PROJECT_ID) $(BUCKET_NAME) $(CDD_VAULT_ID) $(ALERT_EMAIL); do \
	  case " $(EXAMPLE_VALUES) " in *" $$v "*) bad="$$bad $$v";; esac; done; \
	if [ -n "$$bad" ]; then echo "config.mk still has example value(s):$$bad. Edit config.mk first." >&2; exit 1; fi
	@echo ">> Project $(PROJECT_ID)$(if $(filter-out file command line,$(PROJECT_ID_SOURCE)), (gcloud's active project: not set in config.mk)), bucket $(or $(BUCKET_NAME),-)"

config: ## Show the settings in use. Run this first: it tells you which project you're about to change
	@echo "PROJECT_ID   = $(PROJECT_ID)$(if $(filter-out file command line,$(PROJECT_ID_SOURCE)),   <- not in config.mk: gcloud's active project)"
	@echo "REGION       = $(REGION)"
	@echo "BUCKET_NAME  = $(BUCKET_NAME)"
	@echo "CDD_VAULT_ID = $(CDD_VAULT_ID)"
	@echo "ALERT_EMAIL  = $(ALERT_EMAIL)"
	@echo "EXPORTS      = $(or $(EXPORTS),(default: everything except eln, users))"
	@echo "SCHEDULE     = $(SCHEDULE)  /  verification: $(VERIFY_SCHEDULE)  ($(TIME_ZONE))"
	@$(MAKE) --no-print-directory check-config >/dev/null && echo "OK: no example values left"

# --- One-time setup (safe to re-run) ---------------------------------------

setup: apis bucket service-account secret-if-missing iam builder-role ## All one-time setup (asks for the CDD token if none is stored yet)

apis: check-config ## Enable the Google Cloud APIs this project needs
	$(GCLOUD) services enable run.googleapis.com cloudbuild.googleapis.com \
	  artifactregistry.googleapis.com secretmanager.googleapis.com \
	  cloudscheduler.googleapis.com monitoring.googleapis.com

bucket: check-config require-REGION require-BUCKET_NAME ## Create the backup bucket if it doesn't exist
	@$(GCLOUD) storage buckets describe gs://$(BUCKET_NAME) >/dev/null 2>&1 \
	  && echo "Bucket gs://$(BUCKET_NAME) already exists" \
	  || $(GCLOUD) storage buckets create gs://$(BUCKET_NAME) --location=$(REGION) \
	       --uniform-bucket-level-access --public-access-prevention

service-account: check-config ## Create the job's runtime service account if it doesn't exist
	@$(GCLOUD) iam service-accounts describe $(SA) >/dev/null 2>&1 \
	  && echo "Service account $(SA) already exists" \
	  || $(GCLOUD) iam service-accounts create $(SA_NAME)

secret: check-config ## Store the CDD token in Secret Manager (prompts; adds a new version if it exists)
	@read -rsp "CDD API token: " T; echo; \
	if $(GCLOUD) secrets describe $(SECRET_NAME) >/dev/null 2>&1; then \
	  printf '%s' "$$T" | $(GCLOUD) secrets versions add $(SECRET_NAME) --data-file=-; \
	else \
	  printf '%s' "$$T" | $(GCLOUD) secrets create $(SECRET_NAME) --data-file=-; \
	fi

# Used by `setup`: `iam` needs the secret to exist, but re-running setup must not ask
# for the token again (to replace the token: `make secret`).
secret-if-missing: check-config
	@$(GCLOUD) secrets describe $(SECRET_NAME) >/dev/null 2>&1 \
	  && echo "Secret $(SECRET_NAME) already exists (to replace the token: make secret)" \
	  || $(MAKE) --no-print-directory secret

iam: check-config require-BUCKET_NAME ## Give the job's service account its permissions, and let you deploy it
	@$(GCLOUD) secrets describe $(SECRET_NAME) >/dev/null 2>&1 \
	  || { echo "Secret $(SECRET_NAME) doesn't exist yet: run make secret first" >&2; exit 1; }
	$(GCLOUD) secrets add-iam-policy-binding $(SECRET_NAME) \
	  --member=serviceAccount:$(SA) --role=roles/secretmanager.secretAccessor --format=none
	$(GCLOUD) storage buckets add-iam-policy-binding gs://$(BUCKET_NAME) \
	  --member=serviceAccount:$(SA) --role=roles/storage.objectCreator --format=none
	@# Read access (no delete) so the run summary can compare with the previous backup
	$(GCLOUD) storage buckets add-iam-policy-binding gs://$(BUCKET_NAME) \
	  --member=serviceAccount:$(SA) --role=roles/storage.objectViewer --format=none
	$(GCLOUD) iam service-accounts add-iam-policy-binding $(SA) \
	  --member=user:$(ME) --role=roles/iam.serviceAccountUser --format=none

builder-role: check-config ## Let the default compute account build from source (roles/run.builder)
	$(GCLOUD) projects add-iam-policy-binding $(PROJECT_ID) \
	  --member=serviceAccount:$(COMPUTE_SA) --role=roles/run.builder --condition=None --format=none

# --- Deploy and run ----------------------------------------------------------

deploy: check-config require-REGION require-BUCKET_NAME require-CDD_VAULT_ID ## Build and deploy the Cloud Run job (no schedule)
	@test -f Dockerfile || { echo "No Dockerfile here: run make from the project folder" >&2; exit 1; }
	@# "^@^" makes @ the separator between env vars, because EXPORTS itself contains commas
	$(GCLOUD) run jobs deploy $(JOB_NAME) \
	  --source . \
	  --region $(REGION) \
	  --service-account $(SA) \
	  --set-secrets CDD_API_TOKEN=$(SECRET_NAME):latest \
	  --set-env-vars "^@^CDD_VAULT_ID=$(CDD_VAULT_ID)@BUCKET_NAME=$(BUCKET_NAME)$(if $(EXPORTS),@EXPORTS=$(EXPORTS))" \
	  --task-timeout $(TASK_TIMEOUT) \
	  --memory $(MEMORY) \
	  --max-retries 0

deploy-verify: check-config require-REGION require-BUCKET_NAME require-CDD_VAULT_ID ## Build and deploy the verification job (no schedule)
	@test -f Dockerfile || { echo "No Dockerfile here: run make from the project folder" >&2; exit 1; }
	@# Same image and service account as the backup (it only reads the bucket and CDD)
	$(GCLOUD) run jobs deploy $(VERIFY_JOB_NAME) \
	  --source . \
	  --region $(REGION) \
	  --service-account $(SA) \
	  --set-secrets CDD_API_TOKEN=$(SECRET_NAME):latest \
	  --set-env-vars "CDD_VAULT_ID=$(CDD_VAULT_ID),BUCKET_NAME=$(BUCKET_NAME),MAX_AGE_DAYS=$(MAX_AGE_DAYS)" \
	  --command python --args verify_backup.py \
	  --task-timeout 1h \
	  --memory $(VERIFY_MEMORY) \
	  --max-retries 0

run: check-config require-REGION ## Execute the job once and wait for it to finish
	$(GCLOUD) run jobs execute $(JOB_NAME) --region $(REGION) --wait

run-verify: check-config require-REGION ## Execute the verification job once and wait
	$(GCLOUD) run jobs execute $(VERIFY_JOB_NAME) --region $(REGION) --wait

executions: check-config require-REGION ## List recent executions of the job
	$(GCLOUD) run jobs executions list --job $(JOB_NAME) --region $(REGION)

LOG_LIMIT ?= 100

logs: check-config ## Last week of logs of both jobs, newest first (JOB=backup or JOB=verify for one; LOG_LIMIT=100)
	@# jsonPayload.message is the backup summary (logged as JSON); everything else is textPayload
	@# (textPayload:* OR jsonPayload.message:*) drops Cloud Run's own events, which have no text
	$(GCLOUD) logging read 'resource.type="cloud_run_job" AND resource.labels.job_name=($(if $(filter verify,$(JOB)),,"$(JOB_NAME)") $(if $(JOB),,OR) $(if $(filter backup,$(JOB)),,"$(VERIFY_JOB_NAME)")) AND (textPayload:* OR jsonPayload.message:*)' \
	  --limit $(LOG_LIMIT) --freshness 7d --format='value(timestamp,resource.labels.job_name,textPayload,jsonPayload.message)'

list-backups: check-config require-BUCKET_NAME ## List the files in the backup bucket
	$(GCLOUD) storage ls -l "gs://$(BUCKET_NAME)/backups/**"

# --- Local development ------------------------------------------------------

# These run on your computer. `uv run` uses the project's own Python 3.14 and packages
# (installing them the first time), so no virtual environment needs activating.

local: require-CDD_VAULT_ID ## Run main.py locally into ./backups (needs CDD_API_TOKEN exported in your shell)
	@test -n "$$CDD_API_TOKEN" || { echo "Export CDD_API_TOKEN first" >&2; exit 1; }
	CDD_VAULT_ID=$(CDD_VAULT_ID) EXPORTS=$(EXPORTS) LOCAL_OUTPUT_DIR=./backups uv run python main.py

verify: require-BUCKET_NAME require-CDD_VAULT_ID ## Check a stored backup locally (newest full run, or RUN=<run id>). Needs `gcloud auth application-default login` once
	@# Live counts vs CDD only if CDD_API_TOKEN is exported in your shell
	uv run python verify_backup.py --bucket $(BUCKET_NAME) --vault $(CDD_VAULT_ID) $(if $(RUN),--run $(RUN))

exit-kit: require-BUCKET_NAME require-CDD_VAULT_ID ## Convert a backup run to SDF/CSV/files in ./exit_kit/<run>, then check it (RUN=<run id>; CHECK_STRUCTURES=1 adds RDKit)
	uv run python exit_kit.py --bucket $(BUCKET_NAME) --vault $(CDD_VAULT_ID) $(if $(RUN),--run $(RUN)) $(if $(CHECK_STRUCTURES),--check-structures)

lint: ## Run ruff (check + format check), the version pinned in pyproject.toml
	uv run ruff check . && uv run ruff format --check .

# --- Schedule (Mon-Fri) -------------------------------------------------------
# Run `make deploy` and a successful manual `make run` before `make schedule`.
# 02:00 Mon-Fri backs up the previous day, i.e. each Sun-Thu workday.

SCHEDULER_REGION  ?= $(REGION)
SCHEDULE          ?= 0 2 * * 1-5
TIME_ZONE         ?= Asia/Jerusalem
SCHEDULER_NAME    ?= $(JOB_NAME)-weekly
SCHEDULER_SA_NAME ?= $(JOB_NAME)-scheduler-sa
SCHEDULER_SA       = $(SCHEDULER_SA_NAME)@$(PROJECT_ID).iam.gserviceaccount.com
# Verification: 4 h after the backup starts (a full backup takes ~75 min)
VERIFY_SCHEDULE        ?= 0 6 * * 1-5
VERIFY_SCHEDULER_NAME  ?= $(VERIFY_JOB_NAME)-schedule

# $(call schedule_job,<Cloud Run job>,<scheduler job name>,<cron schedule>)
# Lets the scheduler's service account run the job, then creates or updates the trigger.
define schedule_job
	@$(GCLOUD) iam service-accounts describe $(SCHEDULER_SA) >/dev/null 2>&1 \
	  || $(GCLOUD) iam service-accounts create $(SCHEDULER_SA_NAME)
	$(GCLOUD) run jobs add-iam-policy-binding $(1) --region $(REGION) \
	  --member=serviceAccount:$(SCHEDULER_SA) --role=roles/run.invoker --format=none
	@if $(GCLOUD) scheduler jobs describe $(2) --location $(SCHEDULER_REGION) >/dev/null 2>&1; then \
	  echo "Updating scheduler job $(2)"; ACTION=update; else ACTION=create; fi; \
	$(GCLOUD) scheduler jobs $$ACTION http $(2) --location $(SCHEDULER_REGION) \
	  --schedule "$(3)" --time-zone "$(TIME_ZONE)" \
	  --uri "https://$(REGION)-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$(PROJECT_ID)/jobs/$(1):run" \
	  --http-method POST \
	  --oauth-service-account-email $(SCHEDULER_SA)
endef

schedule: check-config require-REGION ## Create or update the backup's Cloud Scheduler trigger (Mon-Fri 02:00 by default)
	$(call schedule_job,$(JOB_NAME),$(SCHEDULER_NAME),$(SCHEDULE))

schedule-verify: check-config require-REGION ## Create or update the verification's trigger (4 h after each backup)
	$(call schedule_job,$(VERIFY_JOB_NAME),$(VERIFY_SCHEDULER_NAME),$(VERIFY_SCHEDULE))

schedule-run: check-config ## Trigger the Scheduler job now (tests the trigger and its permissions)
	$(GCLOUD) scheduler jobs run $(SCHEDULER_NAME) --location $(SCHEDULER_REGION)

schedule-pause: check-config ## Pause the scheduled trigger
	$(GCLOUD) scheduler jobs pause $(SCHEDULER_NAME) --location $(SCHEDULER_REGION)

schedule-resume: check-config ## Resume the scheduled trigger
	$(GCLOUD) scheduler jobs resume $(SCHEDULER_NAME) --location $(SCHEDULER_REGION)

# --- Email alerts -------------------------------------------------------------
# 1) the job ran and failed, 2) the Scheduler could not trigger the job,
# 3) the summary after every run (sizes vs previous backup, WARNING if smaller),
# 4) and 5) the same as 1) and 2) for the verification job.
# Existing channels/policies are left alone; delete them in the console to recreate.
# Uses `gcloud alpha`, which may offer to install the alpha component the first time.

ALERT_EMAIL        ?=
ALERT_CHANNEL_NAME ?= $(JOB_NAME) alerts
FAILED_ALERT_NAME   = $(JOB_NAME) failed
SCHED_ALERT_NAME    = $(JOB_NAME) trigger failed
SUMMARY_ALERT_NAME  = $(JOB_NAME) summary
VERIFY_FAILED_ALERT_NAME = $(VERIFY_JOB_NAME) failed
VERIFY_SCHED_ALERT_NAME  = $(VERIFY_JOB_NAME) trigger failed

# Policy definitions live in alerts/*.json. __PLACEHOLDERS__ are filled in here.
# backup-failed.json and verify-failed.json share the same condition (only the email
# text differs): keep their "conditions" blocks identical when changing one.
# $(call create_policy,<display name>,<template file>[,<job name>,<scheduler name>])
# The job and scheduler names default to the backup's.
create_policy = if [ -n "$$($(GCLOUD) alpha monitoring policies list --filter='displayName="$(1)"' --format='value(name)')" ]; then echo "Policy '$(1)' already exists, skipping"; else F=$$(mktemp); sed -e "s|__DISPLAY_NAME__|$(1)|" -e "s|__JOB_NAME__|$(or $(3),$(JOB_NAME))|g" -e "s|__SCHEDULER_NAME__|$(or $(4),$(SCHEDULER_NAME))|g" -e "s|__CHANNEL__|$$CH|" $(2) > $$F; $(GCLOUD) alpha monitoring policies create --policy-from-file=$$F; rm -f $$F; fi

alerts: check-config require-ALERT_EMAIL ## Create the email channel and the five alert policies (skips existing ones)
	@set -e; \
	CH=$$($(GCLOUD) beta monitoring channels list --filter='displayName="$(ALERT_CHANNEL_NAME)"' --format='value(name)'); \
	if [ -z "$$CH" ]; then \
	  CH=$$($(GCLOUD) beta monitoring channels create --display-name="$(ALERT_CHANNEL_NAME)" --type=email \
	    --channel-labels=email_address=$(ALERT_EMAIL) --format='value(name)'); echo "Created channel $$CH"; fi; \
	$(call create_policy,$(FAILED_ALERT_NAME),alerts/backup-failed.json); \
	$(call create_policy,$(SCHED_ALERT_NAME),alerts/scheduler-failed.json); \
	$(call create_policy,$(SUMMARY_ALERT_NAME),alerts/backup-summary.json); \
	$(call create_policy,$(VERIFY_FAILED_ALERT_NAME),alerts/verify-failed.json,$(VERIFY_JOB_NAME)); \
	$(call create_policy,$(VERIFY_SCHED_ALERT_NAME),alerts/scheduler-failed.json,$(VERIFY_JOB_NAME),$(VERIFY_SCHEDULER_NAME))
