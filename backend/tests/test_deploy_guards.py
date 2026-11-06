"""scripts/lib/deploy_guards.sh — three checks a deploy must pass before it
can say it worked.

Without them, a months-old checkout (an Alembic head far behind the live
database, whole packages absent) can be deployed to the service again and
again, with deploy.sh printing "Deployment Complete!" every time. Traffic
pinned to an older revision is then the only thing between that image and an
outage — and the same pin silently swallows a legitimate deploy from main.

Each guard is exercised the way it fails in production: the pure decision
functions get their inputs directly (no gcloud, no curl, no network), and the
git guard runs against a throwaway repository with a fake origin.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "deploy_guards.sh"


def _bash(
    snippet: str, *, cwd: Path | None = None, env: dict | None = None, stdin: str = ""
):
    """Source the library and run `snippet`; return (returncode, stdout, stderr)."""
    import os

    full_env = {**os.environ, **(env or {})}
    proc = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; {snippet}'],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_the_library_parses():
    rc, _out, err = _bash("true")
    assert rc == 0, err


# ── guard 2: the tree must know the live database revision ───────────────────


def test_a_tree_that_knows_the_live_revision_passes(tmp_path):
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "zc00000000073_anything.py").write_text("# migration\n")
    rc, _o, _e = _bash(f'_tree_knows_revision zc00000000073 "{versions}"')
    assert rc == 0


def test_a_tree_behind_the_live_database_is_detected(tmp_path):
    """The stale-checkout shape: the live DB is at zc73 and the tree stops at zc8.
    That image would boot with AUTO_MIGRATE=true against a revision it has
    never heard of."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "zc00000000008_old.py").write_text("# migration\n")
    rc, _o, _e = _bash(f'_tree_knows_revision zc00000000073 "{versions}"')
    assert rc != 0


def test_an_empty_or_missing_revision_never_passes_by_accident(tmp_path):
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "zc00000000073_x.py").write_text("")
    rc, _o, _e = _bash(f'_tree_knows_revision "" "{versions}"')
    assert rc != 0
    rc, _o, _e = _bash(f'_tree_knows_revision zc00000000073 "{tmp_path / "nope"}"')
    assert rc != 0


def test_the_health_payload_is_read_from_the_migrations_block():
    health = json.dumps(
        {"status": "ok", "migrations": {"db_revision": "zc00000000074"}}
    )
    rc, out, _e = _bash("_health_db_revision", stdin=health)
    assert rc == 0 and out.strip() == "zc00000000074"


def test_a_health_payload_without_a_revision_yields_empty_not_garbage():
    """Empty means 'cannot compare — warn and continue'. Anything else would
    be compared against the tree and could refuse a good deploy."""
    for payload in ('{"status":"ok"}', "not json at all", ""):
        rc, out, _e = _bash("_health_db_revision", stdin=payload)
        assert rc == 0 and out.strip() == "", payload


# ── guard 3: the new revision must actually be serving ───────────────────────


def _service_json(latest: str, traffic: list[dict]) -> str:
    return json.dumps(
        {"status": {"latestReadyRevisionName": latest, "traffic": traffic}}
    )


def test_latest_at_100_percent_is_serving():
    js = _service_json(
        "cubie-00608-t29",
        [
            {
                "revisionName": "cubie-00608-t29",
                "percent": 100,
                "latestRevision": True,
            }
        ],
    )
    rc, out, _e = _bash("_traffic_serving_latest", stdin=js)
    assert rc == 0 and out.strip() == "OK"


def test_a_healthy_revision_with_traffic_pinned_elsewhere_is_detected():
    """Revision 00608 built and healthy; traffic pinned to 00604 with
    latestRevision unset. A deploy that lands in this state has not shipped."""
    js = _service_json(
        "cubie-00608-t29",
        [{"revisionName": "cubie-00604-9dl", "percent": 100}],
    )
    rc, out, _e = _bash("_traffic_serving_latest", stdin=js)
    assert rc == 0
    assert out.strip() == "PINNED cubie-00604-9dl"


def test_a_partial_split_is_not_serving():
    """A canary at 50% is still 'not fully serving' — the deploy must say so
    rather than declare victory."""
    js = _service_json(
        "cubie-00608-t29",
        [
            {"revisionName": "cubie-00604-9dl", "percent": 50},
            {
                "revisionName": "cubie-00608-t29",
                "percent": 50,
                "latestRevision": True,
            },
        ],
    )
    rc, out, _e = _bash("_traffic_serving_latest", stdin=js)
    assert out.strip().startswith("PINNED")


def test_a_latest_revision_marker_counts_even_without_a_revision_name():
    """`--to-latest` produces a traffic entry with latestRevision=true and no
    revisionName. That is the healthy default and must read as OK."""
    js = _service_json("cubie-00608-t29", [{"percent": 100, "latestRevision": True}])
    rc, out, _e = _bash("_traffic_serving_latest", stdin=js)
    assert out.strip() == "OK"


# ── guard 1: the tree must be origin/main ────────────────────────────────────


@pytest.fixture()
def repo_with_origin(tmp_path):
    """A clone whose origin/main is a real bare remote, so `git fetch origin main`
    and `origin/main` resolve without a network."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    (work / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "one"],
        check=True,
        env={**subprocess.os.environ, **env},
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "-u", "origin", "main"], check=True
    )
    return work, env


def test_a_clean_tree_equal_to_origin_main_passes(repo_with_origin):
    work, env = repo_with_origin
    rc, out, err = _bash("guard_tree_is_main", cwd=work, env=env)
    assert rc == 0, err
    assert "equals origin/main" in out


def test_a_detached_head_at_origin_main_passes(repo_with_origin):
    """Content is compared, not the SHA: a worktree detached AT origin/main is
    exactly main and must be deployable."""
    work, env = repo_with_origin
    subprocess.run(
        ["git", "-C", str(work), "checkout", "-q", "--detach", "origin/main"],
        check=True,
    )
    rc, _o, err = _bash("guard_tree_is_main", cwd=work, env=env)
    assert rc == 0, err


def test_a_dirty_tree_is_refused_with_exit_4(repo_with_origin):
    """`--source .` uploads uncommitted work. This is the SOC 2 C-4 hole."""
    work, env = repo_with_origin
    (work / "scratch.docx").write_text("not for prod")
    rc, _o, err = _bash("guard_tree_is_main", cwd=work, env=env)
    assert rc == 4
    assert "DIRTY" in err and "scratch.docx" in err


def test_a_tree_behind_origin_main_is_refused_with_exit_4(repo_with_origin):
    """The stale-checkout case. Push a newer commit to origin, leave the clone
    on the old one."""
    work, env = repo_with_origin
    other = work.parent / "other"
    subprocess.run(
        ["git", "clone", "-q", str(work.parent / "origin.git"), str(other)], check=True
    )
    (other / "b.txt").write_text("two\n")
    subprocess.run(["git", "-C", str(other), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-q", "-m", "two"],
        check=True,
        env={**subprocess.os.environ, **env},
    )
    subprocess.run(
        ["git", "-C", str(other), "push", "-q", "origin", "main"], check=True
    )

    rc, _o, err = _bash("guard_tree_is_main", cwd=work, env=env)
    assert rc == 4
    assert "NOT origin/main" in err and "behind by 1" in err


def test_the_override_is_honoured_and_announced(repo_with_origin):
    """Deliberately testing a branch in dev is allowed — loudly."""
    work, env = repo_with_origin
    (work / "scratch.txt").write_text("wip")
    rc, out, _e = _bash(
        "guard_tree_is_main", cwd=work, env={**env, "DEPLOY_ALLOW_UNMERGED": "1"}
    )
    assert rc == 0
    assert "DEPLOY_ALLOW_UNMERGED=1" in out


# ── the wiring ───────────────────────────────────────────────────────────────


def test_release_controller_guards_before_build_and_checks_traffic_before_jobs():
    text = (_LIB.parents[2] / "rubik_core/platform/deployment.py").read_text()
    deploy = text.split("def deploy(*, dry_run: bool)", 1)[1].split("def migrate()", 1)[
        0
    ]
    assert deploy.index("state = read_control()") < deploy.index("check_freezes(state)")
    assert deploy.index("check_freezes(state)") < deploy.index("sha = check_tree()")
    assert deploy.index("sha = check_tree()") < deploy.index('label="Cloud Build"')
    assert deploy.index("latestReadyRevisionName") < deploy.index(
        "for job in deployed_jobs:"
    )


# ── guard 7: never deploy inside the declared deploy freeze ──────────────────
#
# CUBIE_DEPLOY_FREEZE="<DOW> <HH:MM>-<HH:MM> <tz>"; empty means no freeze. A
# set window blocks inside it and in the 20 minutes before it, so a deploy
# cannot still be rolling out when the meeting starts.

_SCRIPTS = _LIB.parents[1]
FREEZE = "TUE 08:00-12:00 America/Costa_Rica"


def _fake_bin(tmp_path: Path, now: str, freeze: str = FREEZE) -> dict:
    """PATH with a `date` frozen at `now` ("<iso weekday> <HH> <MM>") and a
    `gcloud` that records it was called — so nothing real is ever touched."""
    import os

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "date").write_text(f'#!/bin/bash\necho "{now}"\n')
    (bindir / "gcloud").write_text(
        f'#!/bin/bash\ntouch "{tmp_path}/gcloud_was_called"\nexit 1\n'
    )
    for f in ("date", "gcloud"):
        (bindir / f).chmod(0o755)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "CUBIE_DEPLOY_FREEZE": freeze}
    # A caller's shell must not leak an override or a parent marker into a test.
    env["DEPLOY_FREEZE_OVERRIDE_REASON"] = ""
    env["_DEPLOY_FREEZE_CLEARED_BY_PARENT"] = ""
    return env


@pytest.mark.parametrize(
    ("spec", "parsed"),
    [
        (FREEZE, "2 480 720 America/Costa_Rica"),
        ("fri 16:30-18:00 UTC", "5 990 1080 UTC"),
        ("SUN 00:00-23:59 Europe/Madrid", "7 0 1439 Europe/Madrid"),
    ],
)
def test_a_freeze_window_is_parsed(spec, parsed):
    rc, out, _e = _bash(f'_parse_freeze "{spec}"')
    assert rc == 0 and out.strip() == parsed


@pytest.mark.parametrize(
    "spec",
    [
        "TUE 08:00-12:00",  # no time zone
        "TUESDAY 08:00-12:00 UTC",
        "TUE 8:00-12:00 UTC",
        "TUE 12:00-08:00 UTC",  # ends before it starts
        "TUE 08:00-24:00 UTC",
        "TUE 08:00-12:00 Not/AZone",  # date would silently use UTC
        "TUE 08:00-12:00 ../../etc/passwd",
        "TUE 08:00-12:00 UTC and more",
    ],
)
def test_a_malformed_freeze_window_is_rejected(spec):
    rc, out, _e = _bash(f'_parse_freeze "{spec}"')
    assert rc != 0 and out.strip() == ""


@pytest.mark.parametrize(
    ("dow", "minutes", "frozen"),
    [
        (2, 7 * 60 + 39, False),  # Tue 07:39 — before the lead window
        (2, 7 * 60 + 40, True),  # Tue 07:40 — a deploy now lands at ~08:00
        (2, 8 * 60, True),  # the window opens
        (2, 9 * 60 + 50, True),
        (2, 11 * 60 + 59, True),
        (2, 12 * 60, False),  # the window closes
        (1, 9 * 60, False),  # Monday
        (3, 9 * 60, False),  # Wednesday
    ],
)
def test_the_freeze_window_and_its_lead(dow, minutes, frozen):
    rc, _o, _e = _bash(f"_in_freeze_window {dow} {minutes} 2 480 720")
    assert (rc == 0) is frozen


@pytest.mark.parametrize(
    ("dow", "minutes", "frozen"),
    [
        (7, 23 * 60 + 49, False),  # Sunday 23:49 — before the lead
        (7, 23 * 60 + 50, True),  # Sunday 23:50 — 20 min before Monday 00:10
        (1, 0, True),
        (1, 59, True),
        (1, 60, False),
    ],
)
def test_a_lead_that_reaches_back_past_midnight_still_blocks(dow, minutes, frozen):
    rc, _o, _e = _bash(f"_in_freeze_window {dow} {minutes} 1 10 60")
    assert (rc == 0) is frozen


@pytest.mark.parametrize("freeze", ["", "   "])
def test_an_empty_freeze_never_blocks(tmp_path, freeze):
    """No window declared: any moment of any day is open."""
    for now in ("2 09 30", "2 07 45", "7 23 59", "1 00 00"):
        rc, out, err = _bash(
            "guard_not_during_freeze",
            env=_fake_bin(tmp_path, now, freeze=freeze),
        )
        assert rc == 0, err
        assert "No deploy freeze declared" in out


def test_an_unset_freeze_never_blocks(tmp_path):
    env = _fake_bin(tmp_path, "2 09 30")
    del env["CUBIE_DEPLOY_FREEZE"]
    rc, _o, err = _bash(
        "unset CUBIE_DEPLOY_FREEZE; guard_not_during_freeze",
        env=env,
    )
    assert rc == 0, err


@pytest.mark.parametrize("now", ["2 09 30", "2 08 00", "2 11 59"])
def test_the_guard_refuses_inside_the_window(tmp_path, now):
    rc, _out, err = _bash(
        "guard_not_during_freeze; echo still-running",
        env=_fake_bin(tmp_path, now),
    )
    assert rc == 7
    assert "deploy freeze" in err and FREEZE in err
    assert "DEPLOY_FREEZE_OVERRIDE_REASON" in err  # names the break-glass


@pytest.mark.parametrize("now", ["2 07 40", "2 07 59"])
def test_the_guard_refuses_in_the_20_minutes_before_the_window(tmp_path, now):
    rc, _out, err = _bash("guard_not_during_freeze", env=_fake_bin(tmp_path, now))
    assert rc == 7
    assert "20 minutes before" in err


@pytest.mark.parametrize("now", ["2 07 39", "2 12 00", "3 09 30", "1 09 30"])
def test_the_guard_passes_outside_the_window(tmp_path, now):
    rc, out, _e = _bash(
        "guard_not_during_freeze",
        env=_fake_bin(tmp_path, now),
    )
    assert rc == 0
    assert "Outside the deploy freeze" in out


def test_a_malformed_freeze_refuses_rather_than_guessing(tmp_path):
    """A declared window the guard cannot read is not a reason to deploy into it."""
    rc, _out, err = _bash(
        "guard_not_during_freeze",
        env=_fake_bin(tmp_path, "3 09 30", freeze="TUE 08:00-12:00 Not/AZone"),
    )
    assert rc == 7
    assert "not a window this guard can read" in err


def test_forged_parent_marker_cannot_bypass_freeze(tmp_path):
    """An environment variable from a caller cannot authorize a deployment."""
    env = _fake_bin(tmp_path, "2 07 45")
    env["_DEPLOY_FREEZE_CLEARED_BY_PARENT"] = "1"
    rc, _o, _e = _bash("guard_not_during_freeze", env=env)
    assert rc == 7


def test_break_glass_needs_a_reason_and_says_so(tmp_path):
    env = _fake_bin(tmp_path, "2 10 00")
    env["DEPLOY_FREEZE_OVERRIDE_REASON"] = "the service is down for the meeting"
    rc, out, _e = _bash("guard_not_during_freeze", env=env)
    assert rc == 0
    assert "the service is down for the meeting" in out


@pytest.mark.parametrize("script", ["platform/deploy.sh", "platform/bootstrap_gcp.sh"])
def test_every_deploy_script_requires_explicit_env_before_gcloud(tmp_path, script):
    import os

    proc = subprocess.run(
        ["bash", str(_SCRIPTS / script)],
        cwd=str(tmp_path),  # no .env here, nothing to build
        env={**os.environ, **_fake_bin(tmp_path, "2 10 00")},
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 2, proc.stderr
    assert "--env" in proc.stderr
    assert not (tmp_path / "gcloud_was_called").exists()


# ── no deployment's values are baked into the infra scripts ──────────────────

_DEPLOYMENT_VARS = (
    "GCP_PROJECT_ID",
    "CLOUDSQL_INSTANCE",
    "GCS_BUCKET_NAME",
    "GOOGLE_WORKSPACE_DOMAIN",
    "RUNTIME_SA",
    "RUNTIME_SA_NAME",
    "PROJECT",
    "SQL_INSTANCE",
    "HOST",
    "ALERT_EMAILS",
)


@pytest.mark.parametrize("script", ["platform/deploy.sh", "platform/bootstrap_gcp.sh"])
def test_an_infra_script_has_no_default_project_or_instance(tmp_path, script):
    """Outside any freeze, a script given none of the deployment's values stops
    on the first one it needs, before gcloud: nothing falls back to a baked-in
    project, bucket, instance or service account."""
    import os

    env = {k: v for k, v in os.environ.items() if k not in _DEPLOYMENT_VARS}
    env.update(_fake_bin(tmp_path, "3 10 00", freeze=""))
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("APP_MODE=gcp\n")
    proc = subprocess.run(
        ["bash", str(_SCRIPTS / script), "--env", str(empty_env), "--dry-run"],
        cwd=str(tmp_path),  # no .env here, nothing to build
        env={**env, "CUBIE_PLATFORM_PYTHON": sys.executable},
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode != 0, proc.stdout
    assert "GCP_PROJECT_ID" in proc.stderr, proc.stderr
    assert not (tmp_path / "gcloud_was_called").exists()
