"""The SPA catch-all never serves a file from outside ``frontend/dist``.

``serve_frontend`` (``backend/main.py``) answers every path the API routes leave,
and it used to hand ``os.path.join(dist, full_path)`` straight to
``FileResponse``. The path arrives percent-decoded, so every one of these walked
out of ``dist`` — and in the container ``/app`` one level up holds the whole
backend source tree:

* ``/../secret.txt`` — the review finding (``curl --path-as-is``);
* ``/%2e%2e/secret.txt`` and ``/..%2fsecret.txt`` — no ``--path-as-is`` needed,
  and a front end that collapses ``..`` segments never sees one to collapse;
* ``//app/backend/main.py`` — a doubled slash makes ``full_path`` absolute, and
  ``os.path.join`` then drops ``dist`` altogether: any file, no dots at all.

Two traps for whoever touches this next:

* ``TestClient`` cannot send a literal ``..``: httpx collapses dot segments
  before the request leaves, so ``client.get("/../secret.txt")`` asks for
  ``/secret.txt`` and passes against the vulnerable handler too. The traversal
  cases therefore go through ``_raw_get``, which hands the app the target the
  way uvicorn does — percent-decoded, and nothing else.
* ``os.path.realpath`` raises on a NUL byte where ``os.path.isfile`` quietly
  said no, so a containment check without a guard turns ``/%00`` into a 500.
"""

from __future__ import annotations

import asyncio
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.main import mount_frontend

SPA = b"<!doctype html><title>Cubie</title>"
ADDON = b"<!doctype html><title>Cubie in Meet</title>"
ASSET = b"console.log('cubie')"
SECRET = b"TOP-SECRET"


@pytest.fixture
def dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(SPA)
    (dist / "meet-addon.html").write_bytes(ADDON)
    (dist / "assets" / "app.js").write_bytes(ASSET)
    # What a traversal could reach: a file one level up, a sibling whose name
    # merely starts with "dist", and a symlink inside dist that points out.
    (tmp_path / "secret.txt").write_bytes(SECRET)
    (tmp_path / "dist-evil").mkdir()
    (tmp_path / "dist-evil" / "x.txt").write_bytes(SECRET)
    (dist / "leak").symlink_to(tmp_path / "secret.txt")
    return dist


@pytest.fixture
def app(dist):
    app = FastAPI()
    mount_frontend(app, str(dist))
    return app


def _raw_get(app, target: str) -> tuple[int, bytes]:
    """GET ``target`` exactly as written, the way uvicorn delivers it."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(target),
        "raw_path": target.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    response = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
        elif message["type"] == "http.response.body":
            response["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return response["status"], response["body"]


def test_serves_built_assets_and_falls_back_to_the_spa(app):
    client = TestClient(app)
    assert client.get("/assets/app.js").content == ASSET
    # docs/ENGINEERING_NOTES.md: without this fallback /login 404s on a refresh.
    assert client.get("/login").content == SPA
    assert client.get("/").content == SPA


def test_meet_addon_side_panel_gets_its_own_page(app):
    response = TestClient(app).get("/meet-addon/side-panel?meet_sdk=1")
    assert response.status_code == 200
    assert response.content == ADDON


def test_api_paths_404_as_json(app):
    response = TestClient(app).get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("/../secret.txt", id="dot-dot"),
        pytest.param("/assets/../../secret.txt", id="dot-dot-from-a-subdir"),
        pytest.param("/%2e%2e/secret.txt", id="encoded-dots"),
        pytest.param("/..%2fsecret.txt", id="encoded-slash"),
        pytest.param("/{tmp}/secret.txt", id="absolute-via-double-slash"),
        pytest.param("/../dist-evil/x.txt", id="sibling-sharing-the-prefix"),
        pytest.param("/leak", id="symlink-out-of-dist"),
        pytest.param("/%00", id="nul-byte-is-a-miss-not-a-500"),
    ],
)
def test_nothing_outside_dist_is_ever_served(app, tmp_path, target):
    status, body = _raw_get(app, target.format(tmp=tmp_path))
    assert SECRET not in body
    # A refused path is just an unknown route: the SPA shell, like any other.
    assert (status, body) == (200, SPA)


def test_a_dist_reached_through_a_symlink_still_serves(dist, tmp_path):
    # Containment compares RESOLVED paths on both sides. Resolve only the
    # requested file and a dist behind a symlink refuses every file it has.
    link = tmp_path / "dist-link"
    link.symlink_to(dist)
    linked = FastAPI()
    mount_frontend(linked, str(link))
    assert TestClient(linked).get("/assets/app.js").content == ASSET
