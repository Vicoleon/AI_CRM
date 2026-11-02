"""scripts/pack_chrome_extension.py — the Web Store build of Cubie for Chrome.

The repo's manifest is the development one: it allows only
``http://localhost:5173``. The store build is given the deployment's one public
origin (``--origin`` or ``CUBIE_PUBLIC_ORIGIN``) and keeps that origin alone, so
no company's address is ever committed to the repo.
"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "pack_chrome_extension", REPO_ROOT / "scripts" / "pack_chrome_extension.py"
)
pack = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pack)

DEV = json.loads((REPO_ROOT / "chrome_extension" / "manifest.json").read_text())
ORIGIN = "https://cubie.acme.example"


def test_the_development_manifest_is_cubie_on_localhost_only():
    assert DEV["name"] == "Cubie for Chrome"
    assert DEV["omnibox"]["keyword"] == "cubie"
    assert DEV["host_permissions"] == ["http://localhost:5173/*"]
    for script in DEV["content_scripts"]:
        assert script["matches"] == ["http://localhost:5173/*"]


def test_the_store_build_keeps_only_the_given_origin():
    manifest = pack.store_manifest(DEV, origin=ORIGIN)
    assert manifest["host_permissions"] == [f"{ORIGIN}/*"]
    for script in manifest["content_scripts"]:
        assert script["matches"] == [f"{ORIGIN}/*"]
    assert "omnibox" not in manifest
    assert "contextMenus" not in manifest["permissions"]
    assert pack.check(manifest) == []
    assert DEV["host_permissions"] == ["http://localhost:5173/*"], "dev copy mutated"


def test_the_shortcuts_stay_when_asked():
    manifest = pack.store_manifest(DEV, origin=ORIGIN, with_shortcuts=True)
    assert manifest["omnibox"]["keyword"] == "cubie"
    assert "contextMenus" in manifest["permissions"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://cubie.acme.example", ORIGIN),
        ("https://cubie.acme.example/", ORIGIN),
        ("  https://cubie.acme.example:8443  ", "https://cubie.acme.example:8443"),
    ],
)
def test_an_origin_is_normalised(raw, expected):
    assert pack.parse_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "cubie.acme.example",
        "http://cubie.acme.example",
        "https://cubie.acme.example/app",
        "https://cubie.acme.example/?x=1",
        "https://user@cubie.acme.example",
        "https://",
        "https://cubie.acme.example:abc",
        "https://cubie.acme.example:99999",
    ],
)
def test_anything_but_one_https_origin_is_refused(raw):
    with pytest.raises(ValueError):
        pack.parse_origin(raw)


def test_packing_without_an_origin_stops(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CUBIE_PUBLIC_ORIGIN", raising=False)
    monkeypatch.setattr(pack, "OUT", tmp_path)
    assert pack.main([]) == 2
    assert "CUBIE_PUBLIC_ORIGIN" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_packing_writes_the_store_zip(monkeypatch, tmp_path):
    monkeypatch.setenv("CUBIE_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setattr(pack, "OUT", tmp_path)
    assert pack.main([]) == 0
    (target,) = tmp_path.iterdir()
    assert target.name == f"cubie-for-chrome-{DEV['version']}.zip"
    with zipfile.ZipFile(target) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        names = set(zf.namelist())
    assert manifest["host_permissions"] == [f"{ORIGIN}/*"]
    assert {"icons/icon-16.png", "icons/icon-128.png", "src/bridge.js"} <= names
