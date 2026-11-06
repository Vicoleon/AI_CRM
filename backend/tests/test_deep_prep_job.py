"""Deep Prep Cloud Run Job — launcher builds the right :run request, and the
job entrypoint parses args and drives the pipeline. No network/GCP is touched.
"""

import pytest


class _FakeCreds:
    token = "tok"

    def refresh(self, _req):
        pass


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _patch_auth(monkeypatch):
    from backend.jobs import launcher

    monkeypatch.setattr(launcher, "gcp_project_id", lambda: "test-project")
    monkeypatch.setattr("google.auth.compute_engine.Credentials", _FakeCreds)
    monkeypatch.setattr(
        "google.auth.default", lambda scopes=None: (_FakeCreds(), "proj")
    )
    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: object())


def test_launcher_builds_overrides_and_posts_to_run_endpoint(monkeypatch):
    import backend.routers.home as home

    _patch_auth(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(200, {"name": "exec-1"})

    monkeypatch.setattr("requests.post", fake_post)

    out = home._launch_deep_prep_job_execution(
        run_id="r1",
        user_email="pjensen@acme.example",
        user_department="Sales",
        user_roles=["sales", "user"],
        event_id="ev1",
        focus_on="push for approval",
        force_sales=True,
    )
    assert out == {"name": "exec-1"}
    assert captured["url"].endswith("/jobs/cubie-deep-prep:run")
    args = captured["json"]["overrides"]["containerOverrides"][0]["args"]
    env = captured["json"]["overrides"]["containerOverrides"][0]["env"]
    assert env == [{"name": "CUBIE_WORKSPACE", "value": "test"}]
    # The override REPLACES the container args, so the module invocation must be
    # re-supplied first — otherwise python gets the flags and exits 2.
    assert args[:2] == ["-m", "backend.jobs.deep_prep_job"]
    # args is a flat [--flag, value, ...] list.
    assert args[args.index("--run-id") + 1] == "r1"
    assert args[args.index("--user-roles") + 1] == "sales,user"
    assert args[args.index("--event-id") + 1] == "ev1"
    assert args[args.index("--focus-on") + 1] == "push for approval"
    assert args[args.index("--force-sales") + 1] == "true"


def test_launcher_raises_502_on_error(monkeypatch):
    from fastapi import HTTPException

    import backend.routers.home as home

    _patch_auth(monkeypatch)
    monkeypatch.setattr(
        "requests.post",
        lambda url, json=None, headers=None, timeout=None: _FakeResp(
            403, {}, text="permission denied"
        ),
    )
    with pytest.raises(HTTPException) as ei:
        home._launch_deep_prep_job_execution(
            run_id="r1",
            user_email="j@acme.example",
            user_department="",
            user_roles=[],
            event_id="ev1",
            focus_on=None,
            force_sales=None,
        )
    assert ei.value.status_code == 502


def test_job_entrypoint_parses_args_and_drives_pipeline(monkeypatch):
    import backend.jobs.deep_prep_job as job

    called = {}
    monkeypatch.setattr(
        "backend.routers.home._run_deep_prep_sync",
        lambda *a: called.setdefault("args", a),
    )
    rc = job.main(
        [
            "--run-id",
            "r9",
            "--user-email",
            "pjensen@acme.example",
            "--user-department",
            "Operations",
            "--user-roles",
            "user",
            "--event-id",
            "ev9",
        ]
    )
    assert rc == 0
    a = called["args"]
    assert a[0] == "r9"
    assert a[1] == "pjensen@acme.example"
    assert a[2] == "Operations"
    assert a[3] == ["user"]  # comma-split into a list
    assert a[4] == "ev9"
    assert a[5] is None  # focus_on absent
    assert a[6] is None  # force_sales absent → None (real role)


def test_job_entrypoint_force_sales_true(monkeypatch):
    import backend.jobs.deep_prep_job as job

    called = {}
    monkeypatch.setattr(
        "backend.routers.home._run_deep_prep_sync",
        lambda *a: called.setdefault("args", a),
    )
    job.main(
        [
            "--run-id",
            "r",
            "--user-email",
            "j@acme.example",
            "--event-id",
            "e",
            "--force-sales",
            "true",
        ]
    )
    assert called["args"][6] is True
