"""Static guards on the log-metric filters in ``scripts/setup_monitoring.sh``.

The filters run in GCP, not here, so these tests check the script text for the
two mistakes that have already cost a weekend of mail:

* a bare quoted phrase in a filter (the Logging query language matches it
  case-insensitively over the WHOLE entry) without the run.googleapis.com
  stdout/stderr logName pin. Cloud Monitoring writes an activity entry each
  time an incident opens or closes, with the violating series' resource type
  and labels naming the policy and the metric, so an unpinned phrase that
  appears in either counts the alert's own open/close and re-fires it for
  ever (the Deep Prep and job-launch alerts both did).
* the scheduler alert matching every Cloud Scheduler job in the shared
  project instead of Cubie's own.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = (REPO_ROOT / "scripts" / "setup_monitoring.sh").read_text()

# The third argument of every ensure_log_metric call: the line after the
# description, a double-quoted filter.
_METRIC_CALLS = re.findall(
    r'ensure_log_metric "\$(\w+)" \\\n\s+".*?" \\\n\s+"(.*)"\n', SCRIPT
)


def test_the_parser_sees_every_log_metric():
    assert len(_METRIC_CALLS) == SCRIPT.count('ensure_log_metric "$'), (
        "a log metric call is shaped differently from the rest, so the guards "
        "below are not checking it"
    )


@pytest.mark.parametrize(
    "metric,log_filter", _METRIC_CALLS, ids=[m for m, _ in _METRIC_CALLS]
)
def test_a_bare_phrase_filter_is_pinned_to_the_run_log_stream(metric, log_filter):
    """A bare ``AND \\"…\\"`` term is a whole-entry text search; it must be pinned."""
    if 'AND \\"' not in log_filter:
        return  # field comparisons only — an activity entry cannot satisfy them
    assert "${JOB_LOG_SCOPE}" in log_filter, (
        f"${metric} filters on a bare phrase without ${{JOB_LOG_SCOPE}}, so it "
        f"can count Cloud Monitoring's activity entries about its own alert and "
        f"re-fire for ever: {log_filter}"
    )


def test_the_job_launch_metric_is_pinned():
    """The job-launch loop, by name, in case its filter ever stops being bare."""
    launch = dict(_METRIC_CALLS)["LAUNCH_METRIC"]
    assert "${JOB_LOG_SCOPE}" in launch
    assert "Cloud Run Job launch failed" in launch


def test_the_launch_phrase_is_still_what_the_code_logs():
    """The metric is only as good as the phrase matching the launchers' log lines."""
    for path in (
        "backend/jobs/launcher.py",
        "backend/routers/agents.py",
        "backend/routers/xray.py",
        "rubik_core/meeting_prep/background.py",
    ):
        assert "Cloud Run Job launch failed" in (REPO_ROOT / path).read_text(), path


def test_the_scheduler_metric_only_counts_the_apps_targets():
    sched = dict(_METRIC_CALLS)["SCHED_METRIC"]
    assert "${SCHED_TARGETS}" in sched
    assert 'SCHED_TARGETS="jsonPayload.url:\\"${SERVICE}-\\""' in SCRIPT


def test_every_app_job_is_an_allowed_scheduler_target():
    """A new Cubie Job left out of CUBIE_JOBS would have its schedule fail silently."""
    from backend.jobs import launcher
    from backend.routers.home import DEEP_PREP_JOB_NAME
    from rubik_core.platform.deployment import load_jobs
    from rubik_core.xray.config import XRAY_DEEP_JOB_NAME

    assert "load_jobs" in SCRIPT and "deploy/jobs.yaml" in SCRIPT
    assert "sync_jobs_to_service.sh" not in SCRIPT
    allowed = {job["name"] for job in load_jobs()}
    for job in (
        launcher.WORKER_JOB_NAME,
        launcher.PRIORITIES_JOB_NAME,
        DEEP_PREP_JOB_NAME,
        XRAY_DEEP_JOB_NAME,
    ):
        assert job in allowed, f"{job!r} is missing from CUBIE_JOBS"
