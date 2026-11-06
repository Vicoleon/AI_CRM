#!/usr/bin/env bash
#
# setup_monitoring.sh — Cloud Monitoring baseline for Cubie (service "cubie").
#
# Closes SOC 2 finding H-3 (no uptime checks / infra alerting). Creates, if
# absent (idempotent — safe to re-run; a log-based metric whose live filter
# drifted from this file is repaired in place, policies are never edited):
#   1. An uptime check hitting GET /health every 60s.
#   2. Alert: /health uptime check failing.
#   3. Alert: Cloud Run 5xx error rate elevated.
#   4. Alert: Cloud SQL connections approaching the 100 cap (the 53300
#      exhaustion class — see docs/ENGINEERING_NOTES.md).
#   5. Alert: any Cubie Cloud Scheduler job returning a non-200 (a job silently
#      not running is otherwise only noticed by a user with an empty briefing).
#      Other teams' jobs share this project and are filtered out by target.
#   6. Alert: a cubie-priorities-batch shard producing zero briefings (H-6).
#   7. Alert: ANY Cloud Run Job execution failing. Without it nothing watches
#      this: a Job that exits non-zero has already returned 202 to the caller,
#      so a circular import can kill every Smart Teams job for weeks and the
#      only signal is a user reporting a stuck screen.
#   8. Alert: a Job LAUNCH failing, which #7 cannot see — no execution exists
#      to fail, and the work silently degrades to the CPU-throttled in-process
#      fallback.
#   9-13. Alert: work somebody asked for that never produced a result —
#      Smart Teams scoring, Deep Prep delivery, Deep X-Ray runs, and the two
#      halves of a guide's RAG write. #7 and #8 catch a job that crashed or
#      never launched; these catch the third shape, a job that finished (or
#      died) without writing the result. All are fed by the nightly
#      stale-work sweep. #12 is the only one of these that is a PRIVACY
#      defect rather than a missing feature.
#
# All alerts notify the EXISTING notification channels resolved by address, in
# the order ALERT_EMAILS lists them. Nothing is hardcoded to a numeric channel
# id, and nothing deployment-specific has a default.
#
# Usage:  PROJECT=<gcp project> SQL_INSTANCE=<cloud sql instance> \
#         HOST=<service host> ALERT_EMAILS="ops@acme.example …" \
#           bash scripts/setup_monitoring.sh            # apply
#         DRY_RUN=1 … bash scripts/setup_monitoring.sh  # print, create nothing
#
set -euo pipefail

PROJECT="${PROJECT:?Set PROJECT (the GCP project id)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-cubie}"
SQL_INSTANCE="${SQL_INSTANCE:?Set SQL_INSTANCE (the Cloud SQL instance name)}"
HOST="${HOST:?Set HOST (the public host of the service, no scheme)}"
read -r -a ALERT_EMAILS <<<"${ALERT_EMAILS:?Set ALERT_EMAILS (space-separated notification channel addresses)}"
DRY_RUN="${DRY_RUN:-0}"

DB_RESOURCE="${PROJECT}:${SQL_INSTANCE}"
API="https://monitoring.googleapis.com/v3/projects/${PROJECT}"

log() { printf '  %s\n' "$*"; }

# ── Resolve notification channels by email (must already exist) ──────────────
NOTIF_JSON=""   # JSON array fragment: "proj/.../channels/ID", "..."
for email in "${ALERT_EMAILS[@]}"; do
  name="$(gcloud beta monitoring channels list --project "$PROJECT" \
      --filter="labels.email_address='${email}'" --format='value(name)' | head -1)"
  if [[ -z "$name" ]]; then
    echo "WARNING: no notification channel for ${email} — skipping it." >&2
    continue
  fi
  NOTIF_JSON+="${NOTIF_JSON:+, }\"${name}\""
  log "notify → ${email}  (${name##*/})"
done
if [[ -z "$NOTIF_JSON" ]]; then
  echo "ERROR: no notification channels resolved; aborting." >&2
  exit 1
fi

policy_exists() {  # $1 = displayName
  local n
  n="$(gcloud alpha monitoring policies list --project "$PROJECT" \
        --filter="displayName='$1'" --format='value(name)' | head -1)"
  [[ -n "$n" ]]
}

create_policy() {  # $1 = displayName ; stdin = policy JSON (without name/records)
  local title="$1" file
  if policy_exists "$title"; then
    log "SKIP  (exists): $title"; cat >/dev/null; return 0
  fi
  file="$(mktemp)"; cat >"$file"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY-RUN would create policy: $title"; rm -f "$file"; return 0
  fi
  # A just-created log-based metric can take a bit to register a descriptor;
  # a policy referencing it 400s with "Cannot find metric" until then. Retry.
  local attempt err
  err="$(mktemp)"
  for attempt in 1 2 3 4 5; do
    if gcloud alpha monitoring policies create --project "$PROJECT" --policy-from-file="$file" >/dev/null 2>"$err"; then
      log "CREATED policy: $title"; rm -f "$file" "$err"; return 0
    fi
    if grep -q "Cannot find metric" "$err"; then
      log "  metric not registered yet; retrying in 30s ($attempt/5)…"; sleep 30; continue
    fi
    cat "$err" >&2; rm -f "$file" "$err"; return 1
  done
  echo "ERROR: policy '$title' still failing after retries (metric descriptor lag) — re-run in a few minutes." >&2
  rm -f "$file" "$err"; return 1
}

# ── Log-based metrics: create if absent, REPAIR the filter if it drifted ─────
# The original create-or-skip shape could add a metric but never fix one, so a
# defective filter survived every re-run of this file. Now the live filter is
# compared with the one written here and updated when they differ.
ensure_log_metric() {  # $1 = metric name ; $2 = description ; $3 = log filter
  local name="$1" desc="$2" filter="$3" live
  if live="$(gcloud logging metrics describe "$name" --project "$PROJECT" --format='value(filter)' 2>/dev/null)"; then
    if [[ "$live" == "$filter" ]]; then
      log "SKIP  (exists): log metric $name"; return 0
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
      log "DRY-RUN would UPDATE log metric $name (filter drift)"
      log "    live: $live"
      log "    want: $filter"
      return 0
    fi
    gcloud logging metrics update "$name" --project "$PROJECT" --log-filter="$filter" >/dev/null
    log "UPDATED log metric: $name (filter repaired)"; return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY-RUN would create log metric: $name"; return 0
  fi
  gcloud logging metrics create "$name" --project "$PROJECT" \
    --description="$desc" --log-filter="$filter" >/dev/null
  log "CREATED log metric: $name"
}

# ── Why every job-token metric is pinned to the job's OWN log stream ─────────
# "Cubie Deep Prep delivery stale" once re-fired itself daily for days with no
# stale Deep Prep anywhere. Its metric filter was
# `resource.type="cloud_run_job" AND "DEEP_PREP_DELIVERY_STALE"`, and a bare
# quoted string in the Logging query language is a CASE-INSENSITIVE substring
# match over the whole entry. Each time the policy opened or closed an incident,
# Cloud Monitoring wrote its own activity entry — resource.type cloud_run_job,
# inherited from the violating series, and a label reading "logging/user/
# cubie_deep_prep_delivery_stale … returned to normal". That label contains
# `deep_prep_delivery_stale`, so the metric counted the alert's own resolution,
# which re-opened the alert, whose open event it counted too: a resolve, a
# re-open three minutes later, drifting a few minutes a day. The sweep's real
# finding had been ONE line, about rows already closed by hand.
#
# Any token that is a case-insensitive substring of its metric's name has this
# loop latent in it — four of the six job-token metrics here did. Naming around
# it is fragile; pinning the filter to the Job's stdout/stderr logName is not,
# because a Monitoring activity entry can never carry that logName. The drift
# guard in backend/tests/test_stale_work_sweep.py fails if a token metric loses
# the scope. (Cloud Run parses a JSON log line into jsonPayload but keeps the
# same logName, so this survives a switch to structured logging.)
#
# The same loop hit the SERVICE metric #8: one real ccs-sweep launch failure,
# then an alert every ~18 minutes for two days.
# Its filter was a bare "Cloud Run Job launch failed", and the activity entry
# carries the policy name "Cubie Cloud Run Job launch failed" (a label, under
# resource.type cloud_run_revision). The logName is the same run.googleapis.com
# stdout/stderr for a Service as for a Job, so the one scope covers both, and
# backend/tests/test_monitoring_filters.py fails if ANY bare-text filter in
# this file is missing it.
JOB_LOG_SCOPE="logName=(\"projects/${PROJECT}/logs/run.googleapis.com%2Fstderr\" OR \"projects/${PROJECT}/logs/run.googleapis.com%2Fstdout\")"

# ── 1. Uptime check on /health ───────────────────────────────────────────────
UPTIME_TITLE="Cubie /health"
CHECK_ID="$(gcloud monitoring uptime list-configs --project "$PROJECT" \
    --filter="displayName='${UPTIME_TITLE}'" --format='value(name)' 2>/dev/null | head -1 || true)"
CHECK_ID="${CHECK_ID##*/}"
if [[ -z "$CHECK_ID" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY-RUN would create uptime check: ${UPTIME_TITLE} (https://${HOST}/health)"
    CHECK_ID="DRY_RUN_CHECK_ID"
  else
    TOKEN="$(gcloud auth print-access-token)"
    CHECK_ID="$(curl -sS -X POST "${API}/uptimeCheckConfigs" \
        -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
        -d "{
          \"displayName\": \"${UPTIME_TITLE}\",
          \"monitoredResource\": {\"type\": \"uptime_url\",
            \"labels\": {\"host\": \"${HOST}\", \"project_id\": \"${PROJECT}\"}},
          \"httpCheck\": {\"path\": \"/health\", \"port\": 443, \"useSsl\": true, \"validateSsl\": true},
          \"period\": \"60s\", \"timeout\": \"30s\"
        }" | sed -n 's/.*"name": *"\([^"]*\)".*/\1/p' | head -1)"
    CHECK_ID="${CHECK_ID##*/}"
    log "CREATED uptime check: ${UPTIME_TITLE} (${CHECK_ID})"
  fi
else
  log "SKIP  (exists): uptime check ${UPTIME_TITLE} (${CHECK_ID})"
fi

# ── 2. Uptime failing ────────────────────────────────────────────────────────
# duration 300s: page only when 2+ regions fail CONTINUOUSLY for 5 minutes.
# Brief instance stalls (a heavy request pinning the event loop for a minute)
# must not open an incident — with 60s this flapped ~4x/hour and buried the
# inbox. The 30s probe timeout above absorbs the same class of blip.
create_policy "Cubie /health uptime failing" <<JSON
{
  "displayName": "Cubie /health uptime failing",
  "combiner": "OR",
  "conditions": [{
    "displayName": "/health uptime check failing from multiple regions",
    "conditionThreshold": {
      "filter": "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.\"check_id\"=\"${CHECK_ID}\"",
      "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_NEXT_OLDER", "crossSeriesReducer": "REDUCE_COUNT_FALSE", "groupByFields": ["resource.label.host"]}],
      "comparison": "COMPARISON_GT", "thresholdValue": 1, "duration": "300s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "GET https://${HOST}/health is failing from 2+ probe regions. The service is down or unhealthy. Check Cloud Run logs and the serving revision; /health also reports git_sha and session_backend."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 3. Cloud Run 5xx rate ────────────────────────────────────────────────────
create_policy "Cubie Cloud Run 5xx elevated" <<JSON
{
  "displayName": "Cubie Cloud Run 5xx elevated",
  "combiner": "OR",
  "conditions": [{
    "displayName": "${SERVICE} returned >5 5xx responses in 5m",
    "conditionThreshold": {
      "filter": "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"${SERVICE}\" AND metric.label.\"response_code_class\"=\"5xx\"",
      "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 5, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "${SERVICE} served more than 5 HTTP 5xx responses in 5 minutes. The global handler logs these as 'Unhandled server error'. Correlate with a recent deploy (/health git_sha) and Cloud SQL health (see the connections alert)."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 4. Cloud SQL connections near cap ────────────────────────────────────────
create_policy "Cubie Cloud SQL connections near cap" <<JSON
{
  "displayName": "Cubie Cloud SQL connections near cap",
  "combiner": "OR",
  "conditions": [{
    "displayName": "${SQL_INSTANCE} backends > 80 (cap 100) for 5m",
    "conditionThreshold": {
      "filter": "metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\" AND resource.type=\"cloudsql_database\" AND resource.label.\"database_id\"=\"${DB_RESOURCE}\"",
      "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_MAX", "crossSeriesReducer": "REDUCE_MAX"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 80, "duration": "300s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "Postgres backends on ${SQL_INSTANCE} are above 80 of the 100 max_connections cap — the connection-exhaustion (SQLSTATE 53300) class. Invariant (docs/ENGINEERING_NOTES.md): instances x (pool+overflow) must stay under the cap; the cubie-priorities-batch Job opens its own pools. Check max-instances and any batch run overlapping peak."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 5. Cloud Scheduler job failures (via log-based metric) ───────────────────
# The built-in cloudscheduler.googleapis.com/job/attempt_count metric is not
# present in this project's metrics scope, so a policy referencing it is
# rejected. Detect failures from the execution logs instead — the same
# log-based-metric approach the Fathom/Gemini/HubSpot alerts already use.
#
# Scoped to jobs whose target is Cubie: the Service's own URL, or the Jobs API
# run endpoint of a Cubie Job (the Loose Ends scan schedules cubie-worker
# directly). Unscoped, it matches every scheduler job in the project, and one
# other team's job failing every morning sends more mail under "Cubie" than all
# of Cubie's own jobs together.
# A new Cubie Job that Cloud Scheduler runs directly goes in CUBIE_JOBS.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM_PYTHON="${CUBIE_PLATFORM_PYTHON:-$ROOT/.venv/bin/python}"
if [[ "$PLATFORM_PYTHON" != /* ]]; then PLATFORM_PYTHON="$ROOT/$PLATFORM_PYTHON"; fi
if [[ ! -x "$PLATFORM_PYTHON" ]]; then PLATFORM_PYTHON="python3"; fi
JOB_NAMES="$(cd "$ROOT" && "$PLATFORM_PYTHON" -c 'from rubik_core.platform.deployment import load_jobs; print("\n".join(job["name"] for job in load_jobs()))')" || {
  echo "ERROR: cannot read deploy/jobs.yaml; refusing incomplete Job monitoring." >&2
  exit 1
}
CUBIE_JOBS=()
while IFS= read -r job; do
  [ -z "$job" ] || CUBIE_JOBS+=("$job")
done <<<"$JOB_NAMES"
SCHED_TARGETS="jsonPayload.url:\"${SERVICE}-\""
for job in "${CUBIE_JOBS[@]}"; do
  SCHED_TARGETS+=" OR jsonPayload.url:\"/jobs/${job}:run\""
done
SCHED_METRIC="cubie_scheduler_job_failed"
ensure_log_metric "$SCHED_METRIC" \
  "Cubie Cloud Scheduler job executions that failed (severity>=ERROR)" \
  "resource.type=\"cloud_scheduler_job\" AND severity>=ERROR AND (${SCHED_TARGETS})"

create_policy "Cubie Cloud Scheduler job failed" <<JSON
{
  "displayName": "Cubie Cloud Scheduler job failed",
  "combiner": "OR",
  "conditions": [{
    "displayName": "A scheduler job execution failed (log severity >= ERROR)",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${SCHED_METRIC}\" AND resource.type=\"cloud_scheduler_job\"",
      "aggregations": [{"alignmentPeriod": "600s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM", "groupByFields": ["resource.label.job_id"]}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "A Cloud Scheduler job (priorities-run-all, availability-refresh-ooo, transcript-sync, etc.) returned a non-200. Its schedule did not run successfully. Jobs authenticate with X-Scheduler-Key; a 401/403 means the key drifted, a 5xx means the endpoint failed. Re-run manually after fixing."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 6. Priorities batch produced no briefings (dead-man's switch, H-6) ───────
# Replaces the (confusing, paused) ingestion-council quality gate with a
# notification-only signal: batch_runner.py logs a distinctive
# PRIORITIES_BATCH_DEGRADED ERROR line when a shard generates zero successful
# briefings. Count those lines and alert the ALERT_EMAILS channels.
BATCH_METRIC="cubie_priorities_batch_degraded"
ensure_log_metric "$BATCH_METRIC" \
  "Priorities batch shard produced 0 successful briefings (H-6)" \
  "resource.type=\"cloud_run_job\" AND ${JOB_LOG_SCOPE} AND \"PRIORITIES_BATCH_DEGRADED\""

create_policy "Cubie priorities batch produced no briefings" <<JSON
{
  "displayName": "Cubie priorities batch produced no briefings",
  "combiner": "OR",
  "conditions": [{
    "displayName": "A priorities batch shard generated 0 successful briefings",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${BATCH_METRIC}\" AND resource.type=\"cloud_run_job\"",
      "aggregations": [{"alignmentPeriod": "3600s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "The morning AI-Priorities batch (Cloud Run Job) finished but a shard produced ZERO successful briefings — affected users open Cubie to an empty morning, and the Job may still have exited 0 (a success-shaped failure). Check batch_runner logs for the run_id + per-user failures, the Cloud Run Job execution, and Vertex/HubSpot health. This is the H-6 replacement for the retired quality-gate council."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── The Cubie Job roster, for scoping #7 ────────────────────────────────────
# The same manifest above owns every Job name, so this alert cannot quietly
# stop covering a Job somebody adds there later.
#
# Scoping is load-bearing, not tidiness. The GCP project may also run other
# products' Jobs, which can fail most days and carry their own alert policies.
# An unscoped cloud_run_job filter would then page the alert channels daily
# about another product's job — an alert storm, and the fastest way to teach
# everyone to ignore this alert before it ever catches anything.
if [[ "${#CUBIE_JOBS[@]}" -eq 0 ]]; then
  echo "ERROR: deploy/jobs.yaml has no Job names. Refusing to create an" >&2
  echo "       UNSCOPED job-failure alert — it would page on every other" >&2
  echo "       product's Jobs in this project." >&2
  exit 1
fi
JOB_ONE_OF=""
for j in "${CUBIE_JOBS[@]}"; do JOB_ONE_OF+="${JOB_ONE_OF:+, }\\\"${j}\\\""; done
log "job-failure alert scoped to: ${CUBIE_JOBS[*]}"

# ── 7. Cloud Run Job execution failed ───────────────────────────────────────
# THE gap that lets a Smart Teams outage run for weeks. Every durable
# background task is a Cloud Run Job execution (backend/jobs/launcher.py), and
# without this alert a failed one tells nobody: the HTTP response carrying 202
# has already gone back to the browser, the UI is showing its optimistic
# "working on it" state, and nothing watches failedCount. A circular import that
# makes every Smart Teams job exit 1 on startup loses people their 30-minute
# interview results and fails the nightly skill sweep night after night, all
# silently.
#
# Uses the built-in metric rather than a log-based one on purpose: it counts
# process OUTCOMES, so it fires even when the job dies before reaching any of
# its own error handling. That is exactly what happened — a module-level
# ImportError beats main(), so smart_teams_score_job never wrote its own
# "failed" audit record and the audit trail was empty too. (The built-in
# Cloud Scheduler metric is NOT in this project's scope, which is why #5 is
# log-based; these job metrics ARE — verified against live
# completed_execution_count series before this was written.)
#
# Execution-level, not task-attempt-level: a task that fails and succeeds on
# retry is noise, a whole execution giving up is not.
create_policy "Cubie Cloud Run Job execution failed" <<JSON
{
  "displayName": "Cubie Cloud Run Job execution failed",
  "combiner": "OR",
  "conditions": [{
    "displayName": "A Cloud Run Job execution finished in state failed",
    "conditionThreshold": {
      "filter": "metric.type=\"run.googleapis.com/job/completed_execution_count\" AND resource.type=\"cloud_run_job\" AND metric.label.\"result\"=\"failed\" AND resource.label.\"job_name\"=one_of(${JOB_ONE_OF})",
      "aggregations": [{"alignmentPeriod": "600s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM", "groupByFields": ["resource.label.job_name"]}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "A Cloud Run Job execution FAILED. Somebody's background work did not happen, and the endpoint that dispatched it already returned 202 — so unless this alert is acted on, the only other signal is a user reporting that a result never arrived.\n\n**Find it** (the incident names the job):\n\n    gcloud run jobs executions list --job=<job> --region=us-central1 --project=${PROJECT} --format='value(metadata.name,metadata.creationTimestamp,status.succeededCount,status.failedCount)'\n\n**Read why**, by execution name:\n\n    gcloud logging read 'resource.type=\"cloud_run_job\" AND labels.\"run.googleapis.com/execution_name\"=\"<execution>\"' --project=${PROJECT} --limit=50 --format='value(timestamp,severity,textPayload)'\n\n**Re-run it** — the jobs are idempotent, and re-running is how a lost result is recovered:\n\n    gcloud run jobs execute <job> --region=us-central1 --project=${PROJECT} --tasks=1 --args='-m,<module>,--<flag>,<id>'\n\n**Reading the exit code.** 1 = a real error (retryable). 2 = a DETERMINISTIC refusal from a Smart Teams job (unknown id, or sections still outstanding) — re-running will refuse identically, so fix the input rather than retrying. 3 = EXIT_OUTAGE from the skill sweep, i.e. an upstream provider was down, not our bug.\n\n**If the traceback is an ImportError at module level**, it is the import-cycle class: an import cycle that the API and the test suite cannot see because both import rubik_core.database.models first. backend/tests/test_database_import_order.py is the guard; run it."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 8. Cloud Run Job LAUNCH failed (the hole in #7) ─────────────────────────
# #7 counts executions that fail — so it is structurally blind to work whose
# execution never existed. When launch_module_execution() raises (IAM, the job
# not deployed, a bad override), submit_background_work degrades to an
# in-process FastAPI BackgroundTask, which Cloud Run CPU-throttles to
# near-zero the instant the response returns. The work then dies half-done
# with no execution to show for it and no failure anywhere: the exact silent
# class this whole file exists to end. The launcher already logs it loudly;
# this turns that log line into a page.
LAUNCH_METRIC="cubie_job_launch_failed"
ensure_log_metric "$LAUNCH_METRIC" \
  "A Cloud Run Job launch failed; work fell back to the CPU-throttled in-process path (backend/jobs/launcher.py)" \
  "resource.type=\"cloud_run_revision\" AND ${JOB_LOG_SCOPE} AND \"Cloud Run Job launch failed\""

create_policy "Cubie Cloud Run Job launch failed" <<JSON
{
  "displayName": "Cubie Cloud Run Job launch failed",
  "combiner": "OR",
  "conditions": [{
    "displayName": "submit_background_work fell back to an in-process BackgroundTask",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${LAUNCH_METRIC}\" AND resource.type=\"cloud_run_revision\"",
      "aggregations": [{"alignmentPeriod": "600s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "${SERVICE} could not LAUNCH a Cloud Run Job and fell back to an in-process BackgroundTask. That fallback is correct off-GCP and wrong here: Cloud Run throttles the service's CPU to near-zero once the response returns, so the task is starved and may never finish — while the caller was told 202.\n\nNote this is invisible to the 'Job execution failed' alert: no execution was created, so there is nothing there to fail.\n\nUsual causes: the Job is not deployed (scripts/platform/deploy.sh), the runtime SA lost run.jobs.run / run.jobs.runWithOverrides (roles/run.developer), or a malformed container override. Find the reason in the service log:\n\n    gcloud logging read 'resource.type=\"cloud_run_revision\" AND \"Cloud Run Job launch failed\" AND NOT logName:\"monitoring.googleapis.com\"' --project=${PROJECT} --freshness=2d --limit=20 --format='value(timestamp,textPayload,jsonPayload.message)'\n\nThe NOT logName clause drops this alert's own open/close entries, which name the policy and so match the phrase.\n\nAfter fixing, re-dispatch the affected work by hand — the throttled fallback leaves no execution to retry."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 9. Smart Teams scoring stale (dead-man's switch) ────────────────────────
# #7 and #8 watch the MECHANISM: a Job that crashed, a Job that never launched.
# Neither can see a run that exited 0 having written nothing — the
# success-shaped failure. From the person's side all three are identical: the
# endpoint returned 202 and the screen says their answers are being scored.
#
# So this one watches the OUTCOME instead. The nightly Smart Teams sweep
# (04:00) checks for interviews somebody finished answering that never became a
# profile, and logs SMART_TEAMS_SCORING_STALE with each interview id.
#
# Deliberately NOT wired to the router's `delayed` branch, which computes the
# same fact: SmartTeams.tsx polls for ~7.5 minutes and the staleness threshold
# is 20, so the automatic poll stops while the state is still `pending` and
# `delayed` is only computed when a human returns and presses the button. An
# alert on that branch would fire only for people who chased their result and
# stay silent for anyone who gave up — the opposite of a dead-man's switch.
#
# The token is a literal on both sides. backend/tests/
# test_smart_teams_stale_scoring_alert.py asserts this file still greps for the
# one the job logs, because a rename on either side switches the alert off with
# nothing going red.
STALE_METRIC="cubie_smart_teams_scoring_stale"
ensure_log_metric "$STALE_METRIC" \
  "A completed Work Style Interview never became a profile (SMART_TEAMS_SCORING_STALE)" \
  "resource.type=\"cloud_run_job\" AND ${JOB_LOG_SCOPE} AND \"SMART_TEAMS_SCORING_STALE\""

# alignmentPeriod 86400s: the producing sweep runs once a night, so a day is the
# natural bucket and a backlog of six people reports as one incident rather than
# six pages.
create_policy "Cubie Smart Teams scoring stale" <<JSON
{
  "displayName": "Cubie Smart Teams scoring stale",
  "combiner": "OR",
  "conditions": [{
    "displayName": "A completed Work Style Interview has no profile",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${STALE_METRIC}\" AND resource.type=\"cloud_run_job\"",
      "aggregations": [{"alignmentPeriod": "86400s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "Somebody answered the full Work Style Interview and it never became a profile. Their answers are safe — only the scoring step is missing, and they do NOT need to retake anything.\n\nThis is the outcome-level backstop: it fires whether the job crashed, never launched, or exited 0 having written nothing. Because the page stops polling after ~7.5 minutes, the person may have no idea anything is wrong yet — treat this as the primary signal, not a confirmation.\n\n**Get the interview ids** — the nightly sweep logs one line per person:\n\n    gcloud logging read 'resource.type=\"cloud_run_job\" AND \"SMART_TEAMS_SCORING_STALE\"' --project=${PROJECT} --freshness=2d --limit=50 --format='value(timestamp,textPayload)'\n\n**Re-score each one** (idempotent — a second run writes no second version):\n\n    gcloud run jobs execute cubie-worker --region=${REGION} --project=${PROJECT} --tasks=1 --args='-m,backend.jobs.smart_teams_score_job,--interview-id,<id>'\n\n**Then confirm** the execution logged 'scored into profile', and tell the person — their page will show the profile on next load.\n\n**Why it happened** is a separate question from recovering the result; recover first. Check whether 'Cubie Cloud Run Job execution failed' or 'Cubie Cloud Run Job launch failed' also fired for the same window: if neither did, the job exited 0 without writing, which is a code bug rather than an infrastructure one."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 10-11. The other two features the nightly sweep covers ──────────────────
# Same shape as #9 and the same producing job
# (backend/jobs/stale_work_sweep_job.py), one policy per feature so each can
# carry the re-dispatch command that actually recovers it — a generic "some
# background work is stale" page would send whoever opens it hunting.
#
# Each threshold belongs to the feature, not to this file:
#   Deep Prep   — reliability.stale_work.DEEP_PREP_STALE_MINUTES (30)
#   Deep X-Ray  — xray.config.DEEP_RUN_TTL_MINUTES (20), the SAME constant
#                 X-Ray's 409 guard already uses to treat a run as dead.
# backend/tests/test_stale_work_sweep.py fails if a token the sweep can emit
# has no policy here, which is the only thing standing between "we added a
# feature" and "that feature is quietly uncovered".

DEEP_PREP_METRIC="cubie_deep_prep_delivery_stale"
ensure_log_metric "$DEEP_PREP_METRIC" \
  "A Deep Prep never left queued/running (DEEP_PREP_DELIVERY_STALE)" \
  "resource.type=\"cloud_run_job\" AND ${JOB_LOG_SCOPE} AND \"DEEP_PREP_DELIVERY_STALE\""

create_policy "Cubie Deep Prep delivery stale" <<JSON
{
  "displayName": "Cubie Deep Prep delivery stale",
  "combiner": "OR",
  "conditions": [{
    "displayName": "A Deep Prep run never left queued/running",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${DEEP_PREP_METRIC}\" AND resource.type=\"cloud_run_job\"",
      "aggregations": [{"alignmentPeriod": "86400s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "Somebody asked for a Deep Prep and the PDF was never delivered. The row is still \`queued\` or \`running\` well past the point where that is plausible.\n\nWhy it goes unnoticed: \`meeting_prep_runs.delivery_status\` is written \`queued\` by the request and the ONLY thing that ever moves it is the job. A job that dies before its first status write leaves the row exactly as the request left it — indefinitely, and with the requester told 202.\n\n**Get the run ids** (the sweep logs one line each, with ready-made args):\n\n    gcloud logging read 'resource.type=\"cloud_run_job\" AND \"DEEP_PREP_DELIVERY_STALE\"' --project=${PROJECT} --freshness=2d --limit=50 --format='value(timestamp,textPayload)'\n\n**Re-dispatch** using the \`rerun=\` args from that line:\n\n    gcloud run jobs execute cubie-worker --region=${REGION} --project=${PROJECT} --tasks=1 --args='<rerun args>'\n\nA re-run writes to the SAME row, so it recovers the request the person made rather than creating a second one. If \`--event-id\` shows as a placeholder the calendar event is gone from the row and the person needs to ask again — say so rather than guessing an id."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

XRAY_METRIC="cubie_xray_deep_run_stale"
ensure_log_metric "$XRAY_METRIC" \
  "A deep Customer X-Ray run stalled pending/running (XRAY_DEEP_RUN_STALE)" \
  "resource.type=\"cloud_run_job\" AND ${JOB_LOG_SCOPE} AND \"XRAY_DEEP_RUN_STALE\""

create_policy "Cubie Deep X-Ray run stale" <<JSON
{
  "displayName": "Cubie Deep X-Ray run stale",
  "combiner": "OR",
  "conditions": [{
    "displayName": "A deep X-Ray snapshot stalled in pending/running",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${XRAY_METRIC}\" AND resource.type=\"cloud_run_job\"",
      "aggregations": [{"alignmentPeriod": "86400s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "A deep Customer X-Ray somebody requested stalled: the \`xray_snapshots\` row is still \`pending\`/\`running\` past DEEP_RUN_TTL_MINUTES with no \`progress\` write since.\n\nX-Ray already half-knew about this class. Its 409 guard treats a pending row older than that same TTL as dead so it stops blocking fresh runs — a reaper whose own comment notes a detached run 'has no other watchdog'. It stepped over the corpse silently; this is the watchdog.\n\n**Get the snapshot ids** (the sweep logs one line each, with ready-made args):\n\n    gcloud logging read 'resource.type=\"cloud_run_job\" AND \"XRAY_DEEP_RUN_STALE\"' --project=${PROJECT} --freshness=2d --limit=50 --format='value(timestamp,textPayload)'\n\n**Re-dispatch** using the \`rerun=\` args from that line:\n\n    gcloud run jobs execute cubie-worker --region=${REGION} --project=${PROJECT} --tasks=1 --args='<rerun args>'\n\nThe run resumes the SAME snapshot row rather than starting a new version, so the requester gets the X-Ray they asked for. Note the sweep does NOT reap the row: a stuck row is the only record of what happened, and marking it failed would make the feature look healthy while destroying the evidence."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 12. A retracted guide still in the corpus (PRIVACY) ─────────────────────
# The one alert in this file that is a privacy defect rather than a missing
# result. Smart Teams' only RAG write is an approved "How to work with me"
# guide; retracting one dispatches --purge. That dispatch is fire-once from
# `unpublish` with no retry and no other backstop, so a purge that never ran
# leaves the guide out of the app and still answerable in Chat — the subject
# has been told it is gone.
#
# The guide job writes SMART_TEAMS_GUIDE_PURGE_FAILED to audit_logs when it RUNS
# and fails. It writes nothing when it never runs (a stale Job image is enough),
# so an empty audit trail is not evidence nothing was lost. This alert is fed by
# the row state instead.
GUIDE_PURGE_METRIC="cubie_guide_purge_stale"
ensure_log_metric "$GUIDE_PURGE_METRIC" \
  "PRIVACY: an unpublished Smart Teams guide is still in the RAG corpus (SMART_TEAMS_GUIDE_PURGE_STALE)" \
  "resource.type=\"cloud_run_job\" AND ${JOB_LOG_SCOPE} AND \"SMART_TEAMS_GUIDE_PURGE_STALE\""

create_policy "Cubie Smart Teams guide purge stale (privacy)" <<JSON
{
  "displayName": "Cubie Smart Teams guide purge stale (privacy)",
  "combiner": "OR",
  "conditions": [{
    "displayName": "An unpublished guide is still in the RAG corpus",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${GUIDE_PURGE_METRIC}\" AND resource.type=\"cloud_run_job\"",
      "aggregations": [{"alignmentPeriod": "86400s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "**Privacy defect — triage this ahead of the other stale-work alerts.** Somebody retracted their \"How to work with me\" guide, the app stopped showing it, and it is STILL in the companywide RAG corpus, so Chat can still quote it. The subject has been told it is gone.\n\n**Get the document ids** (the sweep logs one line each, with ready-made args):\n\n    gcloud logging read 'resource.type=\"cloud_run_job\" AND \"SMART_TEAMS_GUIDE_PURGE_STALE\"' --project=${PROJECT} --freshness=2d --limit=50 --format='value(timestamp,textPayload)'\n\n**Purge it** using the rerun args from that line:\n\n    gcloud run jobs execute cubie-worker --region=${REGION} --project=${PROJECT} --tasks=1 --args='<rerun args>'\n\n**Then confirm removal, do not assume it.** The job clears \`rag_ingested\` only on a proven delete, so the row flipping to false IS the proof; if it still says true the purge did not happen. Read the note in \`guide.purge_guide_vectors\` before trusting a local pass — off-GCP, \`delete_document_vectors\` returns True without proving anything.\n\n**Did the job run at all?** Check \`GET /api/admin/audit?action=SMART_TEAMS_GUIDE_PURGE_FAILED\`. An audit row means it ran and failed (read the error). NO audit row means it never ran — a dispatch that never happened or a job dead on import — and since \`--purge\` is fire-once from \`unpublish\` with no retry, nothing would ever have retried it."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

# ── 13. A published guide that never reached the corpus ─────────────────────
# The other half, and the milder one: the app read works, so this is a feature
# half-shipped rather than a leak. The person sees "Cubie is still copying it
# into Chat's knowledge base" indefinitely.
GUIDE_INGEST_METRIC="cubie_guide_ingest_stale"
ensure_log_metric "$GUIDE_INGEST_METRIC" \
  "A published Smart Teams guide never reached the RAG corpus (SMART_TEAMS_GUIDE_INGEST_STALE)" \
  "resource.type=\"cloud_run_job\" AND ${JOB_LOG_SCOPE} AND \"SMART_TEAMS_GUIDE_INGEST_STALE\""

create_policy "Cubie Smart Teams guide ingest stale" <<JSON
{
  "displayName": "Cubie Smart Teams guide ingest stale",
  "combiner": "OR",
  "conditions": [{
    "displayName": "A published guide is not in the RAG corpus",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${GUIDE_INGEST_METRIC}\" AND resource.type=\"cloud_run_job\"",
      "aggregations": [{"alignmentPeriod": "86400s", "perSeriesAligner": "ALIGN_DELTA", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s", "trigger": {"count": 1}
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "documentation": {"mimeType": "text/markdown", "content": "Somebody approved and published their \"How to work with me\" guide and it never reached the companywide corpus, so Chat cannot use it. Not a privacy problem — the in-app read works — but the feature is half shipped and the person is looking at \"Cubie is still copying it into Chat's knowledge base\" indefinitely.\n\n**Get the document ids** (the sweep logs one line each, with ready-made args):\n\n    gcloud logging read 'resource.type=\"cloud_run_job\" AND \"SMART_TEAMS_GUIDE_INGEST_STALE\"' --project=${PROJECT} --freshness=2d --limit=50 --format='value(timestamp,textPayload)'\n\n**Re-ingest** using the rerun args from that line:\n\n    gcloud run jobs execute cubie-worker --region=${REGION} --project=${PROJECT} --tasks=1 --args='<rerun args>'\n\nA plain re-run is enough and no separate purge is needed: \`RagManager.update_document\` deletes any ragFile at the same path before writing, so a re-ingest also replaces a stale earlier copy. Measured at ~25s.\n\n**Do not read \"Chat knew about it\" as proof the flag is right.** Chat can answer from other paths or an older copy; \`rag_ingested\` tracks this corpus write specifically. As with the purge alert, check \`GET /api/admin/audit?action=SMART_TEAMS_GUIDE_INGEST_FAILED\` to tell a job that ran and failed from one that never ran."},
  "enabled": true,
  "notificationChannels": [${NOTIF_JSON}]
}
JSON

echo
echo "Done. Alerts route to: ${ALERT_EMAILS[*]}"
