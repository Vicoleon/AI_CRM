"""Build the Chrome Web Store upload for Cubie for Chrome.

    python3 scripts/pack_chrome_extension.py --origin https://<CUBIE_PUBLIC_HOST>
        # -> dist/cubie-for-chrome-<version>.zip
    CUBIE_PUBLIC_ORIGIN=https://<CUBIE_PUBLIC_HOST> python3 scripts/pack_chrome_extension.py
    python3 scripts/pack_chrome_extension.py --origin … --with-shortcuts   # keep the two shortcuts

The repo's ``chrome_extension/manifest.json`` is the DEVELOPMENT manifest: it
allows only ``localhost:5173``, so the extension can be pointed at a local
Cubie. The store build is given the deployment's one public origin
(``--origin``, else ``CUBIE_PUBLIC_ORIGIN``) and keeps that origin alone, in
both ``host_permissions`` and the content script's ``matches``, so Chrome asks
every person for access to one site and the Web Store reviewer sees nothing
that looks like a test hook. No deployment's address is committed to the repo.
The extension reads its allowed addresses from its own manifest
(``src/store.js``), so nothing else needs editing.

The Web Store refuses an upload whose ``version`` is not higher than the one
already published: bump ``version`` in ``chrome_extension/manifest.json``
before building an update. Docs: ``docs/chrome_extension/README.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "chrome_extension"
OUT = ROOT / "dist"

ORIGIN_ENV = "CUBIE_PUBLIC_ORIGIN"
# Only what the extension needs at runtime goes in the zip.
INCLUDE = ("manifest.json", "icons", "src")


def parse_origin(raw: str) -> str:
    """``raw`` as exactly one ``https://host[:port]`` origin, or ValueError.

    The port is read, not just carried along: ``urlsplit`` keeps
    ``host:abc`` or ``host:99999`` in the netloc and only complains when
    ``.port`` is asked for, so an unread port would ship into the manifest.
    """
    url = urlsplit(raw.strip())
    try:
        url.port  # noqa: B018 — reading it is the check
    except ValueError:
        raise ValueError(f"not a single https origin: {raw!r}") from None
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError(f"not a single https origin: {raw!r}")
    return f"https://{url.netloc}"


def store_manifest(dev: dict, *, origin: str, with_shortcuts: bool = False) -> dict:
    manifest = json.loads(json.dumps(dev))  # deep copy
    pattern = f"{origin}/*"
    manifest["host_permissions"] = [pattern]
    for script in manifest.get("content_scripts", []):
        script["matches"] = [pattern]
    if not with_shortcuts:
        # The Web Store's single-purpose rule for a new-tab extension allows the
        # new tab and a toolbar button, nothing else, so the "cubie" address-bar
        # keyword and the right-click "Ask Cubie about…" stay out of the store
        # build. The extension checks its manifest before using either.
        manifest.pop("omnibox", None)
        manifest["permissions"] = [
            p for p in manifest.get("permissions", []) if p != "contextMenus"
        ]
    return manifest


def check(manifest: dict) -> list[str]:
    problems = []
    if manifest.get("manifest_version") != 3:
        problems.append("manifest_version must be 3")
    for size, rel in (manifest.get("icons") or {}).items():
        if not (SRC / rel).is_file():
            problems.append(f"icon {size} missing: {rel}")
    if "128" not in (manifest.get("icons") or {}):
        problems.append("the Web Store needs a 128x128 icon")
    for pattern in manifest["host_permissions"]:
        if "localhost" in pattern or "run.app" in pattern:
            problems.append(f"development address left in: {pattern}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--origin",
        default=os.environ.get(ORIGIN_ENV, ""),
        help=f"the deployment's public origin, https://host (default: ${ORIGIN_ENV})",
    )
    parser.add_argument("--with-shortcuts", action="store_true")
    args = parser.parse_args(argv)
    if not args.origin:
        print(
            f"Not packed: pass --origin https://<public host> or set {ORIGIN_ENV}.",
            file=sys.stderr,
        )
        return 2
    try:
        origin = parse_origin(args.origin)
    except ValueError as exc:
        print(f"Not packed: {exc}", file=sys.stderr)
        return 2

    dev = json.loads((SRC / "manifest.json").read_text())
    manifest = store_manifest(dev, origin=origin, with_shortcuts=args.with_shortcuts)
    problems = check(manifest)
    if problems:
        print("Not packed:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1

    OUT.mkdir(exist_ok=True)
    target = OUT / f"cubie-for-chrome-{manifest['version']}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for top in INCLUDE[1:]:
            for path in sorted((SRC / top).rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    zf.write(path, path.relative_to(SRC).as_posix())
    print(f"{target}  (version {manifest['version']}, {origin})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
