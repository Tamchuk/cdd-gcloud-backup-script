# Makefile for the CDD backup Cloud Run job. Run `make help` for the targets.
# Put your settings in config.mk (see config.mk.example), or pass them on the
# command line: make deploy REGION=europe-west1 ...
# No secrets live here: the CDD token is stored in Secret Manager (`make secret`).

-include config.mk

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

GCLOUD          = gcloud --project $(PROJECT_ID)
SA              = $(SA_NAME)@$(PROJECT_ID).iam.gserviceaccount.com
PROJECT_NUMBER  = $(shell gcloud projects describe $(PROJECT_ID) --format='value(projectNumber)')
COMPUTE_SA      = $(PROJECT_NUMBER)-compute@developer.gserviceaccount.com
ME              = $(shell gcloud config get-value account 2>/dev/null)

.PHONY: help setup apis bucket service-account secret iam builder-role \
        deploy run executions logs list-backups local lint \
        schedule schedule-run schedule-pause schedule-resume alerts

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# `make require-REGION` fails with a clear message if REGION is empty.
require-%:
	@$(if $($*),:,{ echo "Missing setting: $* (set it in config.mk or on the command line)" >&2; exit 1; })

# --- One-time setup (safe to re-run) ---------------------------------------

setup: apis bucket service-account iam builder-role ## All one-time setup except the token (run `make secret` separately)

apis: require-PROJECT_ID ## Enable the Google Cloud APIs this project needs
	$(GCLOUD) services enable run.googleapis.com cloudbuild.googleapis.com \
	  artifactregistry.googleapis.com secretmanager.googleapis.com \
	  cloudscheduler.googleapis.com monitoring.googleapis.com

bucket: require-PROJECT_ID require-REGION require-BUCKET_NAME ## Create the backup bucket if it doesn't exist
	@$(GCLOUD) storage buckets describe gs://$(BUCKET_NAME) >/dev/null 2>&1 \
	  && echo "Bucket gs://$(BUCKET_NAME) already exists" \
	  || $(GCLOUD) storage buckets create gs://$(BUCKET_NAME) --location=$(REGION) \
	       --uniform-bucket-level-access --public-access-prevention

service-account: require-PROJECT_ID ## Create the job's runtime service account if it doesn't exist
	@$(GCLOUD) iam service-accounts describe $(SA) >/dev/null 2>&1 \
	  && echo "Service account $(SA) already exists" \
	  || $(GCLOUD) iam service-accounts create $(SA_NAME)

secret: require-PROJECT_ID ## Store the CDD token in Secret Manager (prompts; adds a new version if it exists)
	@read -rsp "CDD API token: " T; echo; \
	if $(GCLOUD) secrets describe $(SECRET_NAME) >/dev/null 2>&1; then \
	  printf '%s' "$$T" | $(GCLOUD) secrets versions add $(SECRET_NAME) --data-file=-; \
	else \
	  printf '%s' "$$T" | $(GCLOUD) secrets create $(SECRET_NAME) --data-file=-; \
	fi

iam: require-PROJECT_ID require-BUCKET_NAME ## Give the job's service account its permissions, and let you deploy it
	$(GCLOUD) secrets add-iam-policy-binding $(SECRET_NAME) \
	  --member=serviceAccount:$(SA) --role=roles/secretmanager.secretAccessor --format=none
	$(GCLOUD) storage buckets add-iam-policy-binding gs://$(BUCKET_NAME) \
	  --member=serviceAccount:$(SA) --role=roles/storage.objectCreator --format=none
	@# Read access (no delete) so the run summary can compare with the previous backup
	$(GCLOUD) storage buckets add-iam-policy-binding gs://$(BUCKET_NAME) \
	  --member=serviceAccount:$(SA) --role=roles/storage.objectViewer --format=none
	$(GCLOUD) iam service-accounts add-iam-policy-binding $(SA) \
	  --member=user:$(ME) --role=roles/iam.serviceAccountUser --format=none

builder-role: require-PROJECT_ID ## Let the default compute account build from source (roles/run.builder)
	$(GCLOUD) projects add-iam-policy-binding $(PROJECT_ID) \
	  --member=serviceAccount:$(COMPUTE_SA) --role=roles/run.builder --condition=None --format=none

# --- Deploy and run ----------------------------------------------------------

deploy: require-PROJECT_ID require-REGION require-BUCKET_NAME require-CDD_VAULT_ID ## Build and deploy the Cloud Run job (no schedule)
	@test -f Dockerfile || { echo "No Dockerfile here: run make from the project folder" >&2; exit 1; }
	@# "^@^" makes @ the separator between env vars, because EXPORTS itself contains commas
	$(GCLOUD) run jobs deploy $(JOB_NAME) \
	  --source . \
	  --region $(REGION) \
	  --service-account $(SA) \
	  --set-secrets CDD_API_TOKEN=$(SECRET_NAME):latest \
	  --set-env-vars "^@^CDD_VAULT_ID=$(CDD_VAULT_ID)@BUCKET_NAME=$(BUCKET_NAME)$(if $(EXPORTS),@EXPORTS=$(EXPORTS))" \
	  --task-timeout $(TASK_TIMEOUT) \
	  --max-retries 0

run: require-PROJECT_ID require-REGION ## Execute the job once and wait for it to finish
	$(GCLOUD) run jobs execute $(JOB_NAME) --region $(REGION) --wait

executions: require-PROJECT_ID require-REGION ## List recent executions of the job
	$(GCLOUD) run jobs executions list --job $(JOB_NAME) --region $(REGION)

logs: require-PROJECT_ID ## Show the last week of job logs (newest first)
	$(GCLOUD) logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="$(JOB_NAME)"' \
	  --limit 100 --freshness 7d --format='value(timestamp,textPayload)'

list-backups: require-PROJECT_ID require-BUCKET_NAME ## List the files in the backup bucket
	$(GCLOUD) storage ls -l "gs://$(BUCKET_NAME)/backups/**"

# --- Local development ------------------------------------------------------

local: require-CDD_VAULT_ID ## Run main.py locally into ./backups (needs CDD_API_TOKEN exported in your shell)
	@test -n "$$CDD_API_TOKEN" || { echo "Export CDD_API_TOKEN first" >&2; exit 1; }
	CDD_VAULT_ID=$(CDD_VAULT_ID) EXPORTS=$(EXPORTS) LOCAL_OUTPUT_DIR=./backups python main.py

lint: ## Run ruff (check + format check)
	ruff check . && ruff format --check .

# --- Schedule (Sun + Wed) -----------------------------------------------------
# Run `make deploy` and a successful manual `make run` before `make schedule`.

SCHEDULER_REGION  ?= $(REGION)
SCHEDULE          ?= 0 2 * * 0,3
TIME_ZONE         ?= Asia/Jerusalem
SCHEDULER_NAME    ?= $(JOB_NAME)-weekly
SCHEDULER_SA_NAME ?= $(JOB_NAME)-scheduler-sa
SCHEDULER_SA       = $(SCHEDULER_SA_NAME)@$(PROJECT_ID).iam.gserviceaccount.com
JOB_RUN_URI        = https://$(REGION)-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$(PROJECT_ID)/jobs/$(JOB_NAME):run

schedule: require-PROJECT_ID require-REGION ## Create or update the Cloud Scheduler trigger (twice a week by default)
	@$(GCLOUD) iam service-accounts describe $(SCHEDULER_SA) >/dev/null 2>&1 \
	  || $(GCLOUD) iam service-accounts create $(SCHEDULER_SA_NAME)
	$(GCLOUD) run jobs add-iam-policy-binding $(JOB_NAME) --region $(REGION) \
	  --member=serviceAccount:$(SCHEDULER_SA) --role=roles/run.invoker --format=none
	@if $(GCLOUD) scheduler jobs describe $(SCHEDULER_NAME) --location $(SCHEDULER_REGION) >/dev/null 2>&1; then \
	  echo "Updating scheduler job $(SCHEDULER_NAME)"; ACTION=update; else ACTION=create; fi; \
	$(GCLOUD) scheduler jobs $$ACTION http $(SCHEDULER_NAME) --location $(SCHEDULER_REGION) \
	  --schedule "$(SCHEDULE)" --time-zone "$(TIME_ZONE)" \
	  --uri "$(JOB_RUN_URI)" --http-method POST \
	  --oauth-service-account-email $(SCHEDULER_SA)

schedule-run: require-PROJECT_ID ## Trigger the Scheduler job now (tests the trigger and its permissions)
	$(GCLOUD) scheduler jobs run $(SCHEDULER_NAME) --location $(SCHEDULER_REGION)

schedule-pause: require-PROJECT_ID ## Pause the scheduled trigger
	$(GCLOUD) scheduler jobs pause $(SCHEDULER_NAME) --location $(SCHEDULER_REGION)

schedule-resume: require-PROJECT_ID ## Resume the scheduled trigger
	$(GCLOUD) scheduler jobs resume $(SCHEDULER_NAME) --location $(SCHEDULER_REGION)

# --- Email alerts -------------------------------------------------------------
# 1) the job ran and failed, 2) the Scheduler could not trigger the job,
# 3) the summary after every run (sizes vs previous backup, WARNING if smaller).
# Existing channels/policies are left alone; delete them in the console to recreate.
# Uses `gcloud alpha`, which may offer to install the alpha component the first time.

ALERT_EMAIL        ?=
ALERT_CHANNEL_NAME ?= $(JOB_NAME) alerts
FAILED_ALERT_NAME   = $(JOB_NAME) failed
SCHED_ALERT_NAME    = $(JOB_NAME) trigger failed
SUMMARY_ALERT_NAME  = $(JOB_NAME) summary

# Policy definitions live in alerts/*.json. __PLACEHOLDERS__ are filled in here.
# $(call create_policy,<display name>,<template file>)
create_policy = if [ -n "$$($(GCLOUD) alpha monitoring policies list --filter='displayName="$(1)"' --format='value(name)')" ]; then echo "Policy '$(1)' already exists, skipping"; else F=$$(mktemp); sed -e "s|__DISPLAY_NAME__|$(1)|" -e "s|__JOB_NAME__|$(JOB_NAME)|g" -e "s|__SCHEDULER_NAME__|$(SCHEDULER_NAME)|g" -e "s|__CHANNEL__|$$CH|" $(2) > $$F; $(GCLOUD) alpha monitoring policies create --policy-from-file=$$F; rm -f $$F; fi

alerts: require-PROJECT_ID require-ALERT_EMAIL ## Create the email channel and the three alert policies (skips existing ones)
	@set -e; \
	CH=$$($(GCLOUD) beta monitoring channels list --filter='displayName="$(ALERT_CHANNEL_NAME)"' --format='value(name)'); \
	if [ -z "$$CH" ]; then \
	  CH=$$($(GCLOUD) beta monitoring channels create --display-name="$(ALERT_CHANNEL_NAME)" --type=email \
	    --channel-labels=email_address=$(ALERT_EMAIL) --format='value(name)'); echo "Created channel $$CH"; fi; \
	$(call create_policy,$(FAILED_ALERT_NAME),alerts/job-failed.json); \
	$(call create_policy,$(SCHED_ALERT_NAME),alerts/scheduler-failed.json); \
	$(call create_policy,$(SUMMARY_ALERT_NAME),alerts/backup-summary.json)
