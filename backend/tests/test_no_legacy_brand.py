"""Lock-in for the rebrand: the repo carries no trace of the previous brand.

The pattern, the name digests (staff, an outside contact, clients) and the
scanners live in ``_legacy_brand``. Every banned term there, and every sample
below, is assembled from fragments or stored only as a digest, so neither file
ever matches the scan itself; the names in the samples are made up.

Two hard gates cover what ships: every git-tracked file, by name and by
content, and the built frontend bundle (``frontend/dist``) that every browser
downloads. The backend's own code (``rubik_core/`` and ``backend/``, tests
aside) is also gated one test per area, so a regression names the module it is
in. The last section pins what the renames left behind: citations of deleted
docs and deleted migrations, revision ids that never existed, "an" before the
new name, the old company's clock, and people where a role belongs.
"""

from __future__ import annotations

import base64
import errno
import functools
import hashlib
import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from backend.tests import _legacy_brand
from backend.tests._legacy_brand import (
    BINARY_SNIFF_BYTES,
    LEGACY_PATTERN,
    REPO_ROOT,
    SKIPPED_DIRS,
    STAFF_FULL_NAME_DIGESTS,
    STAFF_TOKEN_DIGESTS,
    legacy_terms,
    scan_repo,
    scan_tree,
    staff_name_hits,
)
from rubik_core.brand import PRODUCT_NAME

MAX_REPORTED_HITS = 50


def _describe(
    hits: list[tuple[str, int, str]],
    what: str = "legacy-brand hits (path:line, line 0 = name)",
) -> str:
    shown = "\n".join(
        f"  {path}:{line_no}: {match}"
        for path, line_no, match in hits[:MAX_REPORTED_HITS]
    )
    hidden = len(hits) - MAX_REPORTED_HITS
    more = f"\n  ... and {hidden} more" if hidden > 0 else ""
    return f"{len(hits)} {what}:\n{shown}{more}"


# -- the gates ---------------------------------------------------------------


def test_repo_has_no_legacy_brand():
    """Every git-tracked file, by name and by content. Local folders git does
    not track (the work ledger, editor worktrees, caches) never reach CI and
    are not read."""
    hits = scan_repo(REPO_ROOT)
    assert hits == [], _describe(hits)


FRONTEND = REPO_ROOT / "frontend"
FRONTEND_DIST = FRONTEND / "dist"
#: A cold ``tsc -b && vite build`` takes about a minute on a laptop; the
#: ceiling only stops a hung build from holding the suite forever.
FRONTEND_BUILD_TIMEOUT_S = 15 * 60
#: How much of a failed build's output the failure message carries.
BUILD_LOG_TAIL = 4000


#: The folders under ``frontend/`` the bundle is built from.
BUNDLE_SOURCES = ("src/", "public/")


def _dist_is_stale(frontend: Path) -> bool:
    """``dist/index.html`` is missing, or older than the newest file git tracks
    under the bundle's sources. Untracked files there are local and do not
    count."""
    index = frontend / "dist" / "index.html"
    if not index.is_file():
        return True
    built = index.stat().st_mtime
    for rel in _legacy_brand.tracked_files(frontend):
        if not rel.startswith(BUNDLE_SOURCES):
            continue
        try:
            if (frontend / rel).stat().st_mtime > built:
                return True
        except OSError:
            continue  # tracked but gone from the working tree
    return False


def _built_frontend() -> Path:
    """``frontend/dist``, built first when it is missing or stale."""
    if not _dist_is_stale(FRONTEND):
        return FRONTEND_DIST
    npm = shutil.which("npm")
    if npm is None or not (FRONTEND / "node_modules").is_dir():
        pytest.fail(
            "frontend/dist is missing or older than frontend/src and "
            "frontend/public, and cannot be built here: "
            "run `npm ci && npm run build` in frontend/ first"
        )
    build = subprocess.run(
        [npm, "run", "build"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=FRONTEND_BUILD_TIMEOUT_S,
    )
    log = (build.stdout + build.stderr)[-BUILD_LOG_TAIL:]
    assert build.returncode == 0, f"npm run build failed:\n{log}"
    return FRONTEND_DIST


def test_frontend_dist_has_no_legacy_brand():
    """The bundle every browser downloads (Review Focus 4): page titles, copy,
    storage keys and colours compiled into it name nothing of the old brand.
    It is rebuilt first when it is older than its sources. Inline base64 fonts
    and images are encoded bytes, not text, and are blanked before matching;
    an inline SVG or text payload is decoded and scanned."""
    dist = _built_frontend()
    assert any(dist.glob("assets/*.js")), "frontend/dist holds no JavaScript"
    hits = scan_tree(dist)
    assert hits == [], _describe(hits, "legacy-brand hits in frontend/dist")


#: The app's mark and the favicon are two files with one shape.
CUBE_MARK = FRONTEND / "src" / "brand" / "assets" / "cube.svg"
FAVICON = FRONTEND / "public" / "cubie.svg"
_VIEW_BOX = re.compile(r'<svg\b[^>]*\bviewBox="([^"]+)"')
_PATH_DATA = re.compile(r'<path\b[^>]*\bd="([^"]+)"')


def _shape(svg: Path) -> tuple[list[str], list[str]]:
    text = svg.read_text(encoding="utf-8")
    return _VIEW_BOX.findall(text), _PATH_DATA.findall(text)


def test_favicon_draws_the_same_cube_as_the_app_mark():
    """Only the colour may differ (the favicon turns lavender in dark mode). A
    redrawn cube must land in both files."""
    mark_box, mark_paths = _shape(CUBE_MARK)
    favicon_box, favicon_paths = _shape(FAVICON)
    assert mark_paths, f"{CUBE_MARK.name} has no path data"
    assert (favicon_box, favicon_paths) == (mark_box, mark_paths)


# -- the pattern -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "write to ops@" + "mqm" + "research.com",
        "MQM " + "Research, Inc.",
        "You are " + "MQ" + "MR Brain",
        'className="bg-' + "mq" + 'mr text-white"',
        "Welcome to " + "At" + "las",
        "search_" + "at" + "las_help",
        "ask" + "At" + "las" + "Href",
        "ATL" + "AS_JOBS",
        "class " + "Bri" + "anFeedback",
        "BRI" + "AN_AGENT_EMAIL",
        "service-" + "bri" + "an",
        "ask " + "Jar" + "vis",
        "vendor@" + "ato" + "mus.com",
        "the " + "S" + "QC logo",
        "sq" + "c_logo.png",
        "data-warehouse-dev-" + "4746" + "18",
        "projects/" + "5947116" + "09963/locations",
        "portal " + "52183" + "64",
        "pipelines " + "81918" + "15",
        "channel " + "C09M4" + "34U5NV",
        "dealstage = '" + "226" + "72671'",
        "_STAGE_OPS = '" + "13415" + "81641'",
        "IN ('" + "238" + "37821', '" + "237" + "10249')",
        "'" + "bd52579e" + "-3251-4260-b791-" + "f02f122283c0'",
        "'" + "a277ef6d" + "-eaf4-4680-b603-" + "f2df96048a9d'",
        '"' + "sub" + "servicer" + '": "Acme"',
        "company." + "sub" + "servicer_oversight_vendor",
        "pre_funding" + "_qc_vendor",
        "nmls" + "_id",
        'props.get("client_status' + '__cloned_")',
        '"product' + '_services": row.get(',
        '{"propertyName": "turn' + '_time", "operator": "HAS_PROPERTY"}',
        "c.get('ongoing" + "_engagements')",
        "rd.product" + "_services LIKE",
        "color: " + "#8527" + "33;",
        'PatternFill("solid", fgColor="' + "8527" + '33")',
        'font.color = "FF' + "8527" + '33"',
        "fill='%23" + "8527" + "33'",
        "RGBColor(0x85, 0x27, 0x" + "33)",
        "rgba(133, 39, " + "51, 0.18)",
        "hover:bg-[" + "#6a1f" + "29]",
        "#5E1B" + "24",
        "#F4E6" + "E8",
        # Eight-digit hex: an alpha after the colour (CSS, SVG, Tailwind) or
        # in front of it (openpyxl, Android), led by a digit or a letter.
        "border-color: " + "#8527" + "3380;",
        "outline: " + "#8527" + "3333",
        "background: " + "#8527" + "331A",
        "fill='%23" + "8527" + "3380'",
        'Color(rgb="00' + "8527" + '33")',
        'android:color="#80' + "8527" + '33"',
        "Color.hex(0x" + "8527" + "3380)",
        "bg-[" + "#6a1f" + "2940]",
        "shadow-[0_0_0_2px_" + "#5E1B" + "2480]",
        "stroke=%22%23" + "f4e6" + "e8cc%22",
        # The old company's own service line and department, in any case.
        "Department(name='" + "HQ" + "VM')",
        "the " + "HQ" + "VM L10, 2026-08-06",
        "Acme Annual " + "HQ" + "VM's renewal",
        'ids["' + "hq" + 'vm"]',
        "evt-" + "hq" + "vm",
        # Its other department, in capitals only (lower case is a common
        # abbreviation).
        "Department(name='" + "BS" + "T')",
        "a portal (" + "BS" + "T ONLY)",
        "evidence_ref='" + "BS" + "T-4410'",
        "Sales / " + "BS" + "T",
        # Record ids from the old live HubSpot portal and Slack workspace.
        "PATCH /crm/v3/objects/contacts/" + "2410742" + "37003 failed",
        '"channel": {"id": "' + "C0BGH" + '0R11UN"}',
        "archives/x/p" + "1783706295" + "536819?",
        "thread_ts=" + "1783699183" + "." + "497849&cid=x",
        'ts == "' + "1783706295" + "." + "536819" + '"',
        "p" + "1783699183" + "497849",
    ],
)
def test_pattern_flags_every_legacy_term(text):
    assert LEGACY_PATTERN.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Atlassian Jira and Confluence",
        "Brianna from Acme",
        "the brain behind it",
        "world atlases",
        '"integrity": "sha512-AOIg' + "S" + "QC" + 'epiJYwP3ARnGx+5VnTu2"',
        "a total of 147461800 rows",
        "row 1" + "8527" + "33 of the export",
        "invoice 8527" + "330",
        "order 10" + "8527" + "331 shipped",
        "#5E1B" + "240",
        "#8527" + "33801",
        "0x8527" + "330",
        "deal " + "226" + "726710 is a longer number",
        "our substation servicer",
        "the los_angeles office",
        "turn_times_by_product",
        "a return_time column",
        "HUBSPOT_DEAL_PRODUCT_PROPERTY",
        "XRAY_RENEWAL_DATE_PROPERTY",
        "Cubie by RubikSoft",
        "a bst of sorted keys",
        "SUBST and BSTR",
        "contacts/" + "2410742" + "370031",
        "C0BGH" + "0R11UNX",
        "ts 91783706295.5368190",
    ],
)
def test_pattern_ignores_lookalikes(text):
    assert LEGACY_PATTERN.search(text) is None, text


def test_guard_files_do_not_match_themselves():
    for path in (Path(_legacy_brand.__file__), Path(__file__)):
        source = path.read_text(encoding="utf-8")
        assert legacy_terms(source) == [], path.name
        assert legacy_terms(path.name) == [], path.name


# -- staff names, by digest --------------------------------------------------


def _sha256(word: str) -> str:
    return hashlib.sha256(word.encode("utf-8")).hexdigest()


#: A made-up handle and a made-up full name stand in for the real staff, whose
#: names this file must not spell out, even in fragments. Two made-up surnames
#: that open with a particle stand in for the ones written in several words.
_HANDLE = "qzorblat"
_FIRST, _LAST = "wenda", "vexley"
_PARTICLE_SURNAMES = ("vanquorst", "oquillan")
_TOKENS = frozenset({_sha256(_HANDLE), *map(_sha256, _PARTICLE_SURNAMES)})
_FULL_NAMES = frozenset({(_sha256(_FIRST), _sha256(f"{_FIRST} {_LAST}"))})
_SURNAMES = frozenset({_sha256(_LAST)})


@pytest.mark.parametrize(
    "text, found",
    [
        ("cc qzorblat@acme.example on it", "qzorblat"),
        ('QZORBLAT_EMAIL = "x"', "QZORBLAT"),
        ("Owner: Wenda Vexley", "Wenda Vexley"),
        ("wenda.vexley@acme.example", "wenda.vexley"),
        ("WENDA_VEXLEY", "WENDA_VEXLEY"),
        ("Wenda\nVexley", "Wenda\nVexley"),
        # "Last, First" order, as a directory or a spreadsheet lists people.
        ("Owner: Vexley, Wenda", "Vexley, Wenda"),
        ("vexley,wenda", "vexley,wenda"),
        # A handle or a name glued into a camelCase identifier.
        ("const qzorblatEmail = x", "qzorblat"),
        ("sendToQzorblat()", "Qzorblat"),
        ("QZORBLATInbox", "QZORBLAT"),
        ("ownerWendaVexley", "WendaVexley"),
        # A surname written in several words, with a space, an apostrophe or
        # a hyphen, or glued.
        ("Owner: Ana Van Quorst", "Van Quorst"),
        ("van-quorst@acme.example", "van-quorst"),
        ("VanQuorst", "VanQuorst"),
        ("Dear Mr O'Quillan,", "O'Quillan"),
        ("Dear Mr O’Quillan,", "O’Quillan"),
        ("M OQuillan", "OQuillan"),
        ("O Quillan", "O Quillan"),
        # An accented spelling.
        ("cc qzörblat", "qzörblat"),
        ("Owner: Wénda Véxley", "Wénda Véxley"),
    ],
)
def test_staff_names_match_by_digest(text, found):
    """A handle or rare surname matches as a whole word; a first name with a
    common surname matches as two adjacent words, in any case or separator,
    and as "Last, First"."""
    hits = staff_name_hits(text, _TOKENS, _FULL_NAMES)
    assert [term for _, term in hits] == [found]
    assert text[hits[0][0] :].startswith(found)


@pytest.mark.parametrize(
    "text",
    [
        "qzorblats",
        "xqzorblat",
        "Wenda Smith",
        "the Vexley account",
        "Wenda",
        "Vexley and Wenda",
        "Vexley. Wenda",
        "the Quorst file",
        "van, Quorst",
        "Quillan",
        "QuorstVan",
    ],
)
def test_staff_names_ignore_other_words(text):
    assert staff_name_hits(text, _TOKENS, _FULL_NAMES) == [], text


@pytest.mark.parametrize(
    "text, found",
    [
        ('Person(first_name="Ana", last_name="Vexley")', "Vexley"),
        ('{"lastName": "Vexley"}', "Vexley"),
        ("surname: 'vexley',", "vexley"),
        ('owner_last_name = "Vexley"', "Vexley"),
        ('family_name="Véxley"', "Véxley"),
    ],
)
def test_a_common_staff_surname_matches_in_a_surname_field(text, found):
    """A real surname that is also a common word cannot stand alone in the
    scan, but kept as a fixture person's surname under a new first name it is
    still the real person (F11)."""
    hits = staff_name_hits(text, frozenset(), frozenset(), surnames=_SURNAMES)
    assert [term for _, term in hits] == [found]
    assert text[hits[0][0] :].startswith(found)


@pytest.mark.parametrize(
    "text",
    [
        "the Vexley account",
        'company="Vexley"',
        'last_name="Vexleyson"',
        'first_name="Vexley"',
    ],
)
def test_a_common_staff_surname_is_left_alone_elsewhere(text):
    assert staff_name_hits(text, frozenset(), frozenset(), surnames=_SURNAMES) == []


#: A made-up client: a one-word name and a three-word one, by digest.
_CLIENT_TOKENS = frozenset({_sha256("zorvex")})
_CLIENT_NAMES = frozenset(
    {
        (
            _sha256("brindle"),
            _sha256("brindle quay"),
            _sha256("brindle quay bank"),
        )
    }
)


@pytest.mark.parametrize(
    "text, found",
    [
        ("Zorvex - Q3 renewal", "Zorvex"),
        ("ana@zorvex.example", "zorvex"),
        ("Brindle Quay Bank - Q3 renewal", "Brindle Quay Bank"),
        ("brindle_quay_bank", "brindle_quay_bank"),
        ("BrindleQuayBank", "BrindleQuayBank"),
    ],
)
def test_client_names_match_by_digest(text, found):
    """An old client by a distinctive word, or by a name of several common
    words that only together name it."""
    hits = _legacy_brand.client_name_hits(text, _CLIENT_TOKENS, _CLIENT_NAMES)
    assert [term for _, term in hits] == [found]
    assert text[hits[0][0] :].startswith(found)


@pytest.mark.parametrize(
    "text",
    ["Brindle Quay", "Brindle Bank", "Quay Bank", "zorvexa", "Quay Bank, Brindle"],
)
def test_client_names_ignore_their_parts(text):
    assert _legacy_brand.client_name_hits(text, _CLIENT_TOKENS, _CLIENT_NAMES) == []


def test_name_digests_are_well_formed():
    """sha256 hex of the lower-case token or phrase (spec §4.2.2). A name of
    several words is the digest of each of its prefixes, so the scan can follow
    it word by word: a full name is the first name's digest and the digest of
    "first last"."""
    sha256_hex = re.compile(r"[0-9a-f]{64}")
    single = (
        STAFF_TOKEN_DIGESTS,
        _legacy_brand.STAFF_SURNAME_DIGESTS,
        _legacy_brand.CONTACT_TOKEN_DIGESTS,
        _legacy_brand.CLIENT_TOKEN_DIGESTS,
    )
    phrases = (STAFF_FULL_NAME_DIGESTS, _legacy_brand.CLIENT_NAME_DIGESTS)
    assert all(single) and all(phrases)
    for digests in single:
        assert all(sha256_hex.fullmatch(digest) for digest in digests)
    for names in phrases:
        for prefixes in names:
            assert len(prefixes) >= 2
            assert all(sha256_hex.fullmatch(digest) for digest in prefixes)
            assert len(set(prefixes)) == len(prefixes)
    assert all(len(name) == 2 for name in STAFF_FULL_NAME_DIGESTS)


def test_scans_report_staff_names_in_text_and_in_file_names(tmp_path, monkeypatch):
    monkeypatch.setattr(_legacy_brand, "STAFF_TOKEN_DIGESTS", _TOKENS)
    monkeypatch.setattr(_legacy_brand, "STAFF_FULL_NAME_DIGESTS", _FULL_NAMES)
    (tmp_path / "team.md").write_text("Owners\nWenda Vexley and qzorblat\n")
    (tmp_path / "qzorblat.json").write_text("{}\n")

    assert scan_tree(tmp_path) == [
        ("qzorblat.json", 0, "qzorblat"),
        ("team.md", 2, "Wenda Vexley"),
        ("team.md", 2, "qzorblat"),
    ]


def test_scans_report_clients_contacts_and_surnames(tmp_path, monkeypatch):
    """The client, outside-contact and surname-field digests are read at call
    time, like the staff ones."""
    monkeypatch.setattr(_legacy_brand, "CLIENT_TOKEN_DIGESTS", _CLIENT_TOKENS)
    monkeypatch.setattr(_legacy_brand, "CLIENT_NAME_DIGESTS", _CLIENT_NAMES)
    monkeypatch.setattr(
        _legacy_brand, "CONTACT_TOKEN_DIGESTS", frozenset({_sha256("pellworth")})
    )
    monkeypatch.setattr(_legacy_brand, "STAFF_SURNAME_DIGESTS", _SURNAMES)
    (tmp_path / "deals.py").write_text(
        'DEAL = "Brindle Quay Bank - Q3 rollout"\n'
        'OWNER = Person(first_name="Ana", last_name="Vexley")\n'
        'CC = "pellworth@outside.example"\n'
    )
    (tmp_path / "zorvex.json").write_text("{}\n")

    assert scan_tree(tmp_path) == [
        ("deals.py", 1, "Brindle Quay Bank"),
        ("deals.py", 2, "Vexley"),
        ("deals.py", 3, "pellworth"),
        ("zorvex.json", 0, "zorvex"),
    ]


# -- the scanners ------------------------------------------------------------


def test_scan_tree_reports_names_and_lines_and_skips_non_source(tmp_path):
    product = "At" + "las"
    package = "mq" + "mr_pkg"
    (tmp_path / package).mkdir()
    (tmp_path / package / "ok.py").write_text("x = 1\n")
    (tmp_path / "notes.md").write_text(f"first line\nWelcome to {product}\n")
    (tmp_path / f"{product.lower()}.svg").write_text("<svg/>\n")
    for skipped in (".git", "node_modules", ".venv", "dist", "__pycache__"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / f"{product}.txt").write_text(f"{product}\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00" + product.encode())
    random_digest = "sha512-Ab" + "Mq" + "Mr" + "Zz+/09xY" * 10 + "=="
    (tmp_path / "package-lock.json").write_text(f'"integrity": "{random_digest}"\n')

    hits = scan_tree(tmp_path)

    assert hits == [
        (f"{product.lower()}.svg", 0, product.lower()),
        (package, 0, "mq" + "mr"),
        ("notes.md", 2, product),
    ]


def _base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_inline_base64_is_blanked_but_inline_text_is_read(tmp_path):
    """A bundled font or image is random base64, which can spell a short term
    by chance. An inline SVG, or any text payload, is text whether or not it is
    base64-encoded: it is decoded and its terms and colours count, reported on
    the line the payload sits on."""
    abbreviation, sister = "mq" + "mr", "S" + "QC"
    old_maroon, product = "8527" + "33", "At" + "las"
    svg = _base64(f"<svg xmlns='http://www.w3.org/2000/svg' fill='#{old_maroon}'/>")
    note = _base64(f"Welcome to {product}")
    script = _base64(f'const team = "{abbreviation}";')
    (tmp_path / "app.css").write_text(
        f".a{{background:url(data:image/png;base64,iVBOR{abbreviation}x+{sister}/w==)}}\n"
        f".b{{src:url(data:font/woff2;charset=utf-8;base64,d09G{abbreviation}Mg==)}}\n"
        f".c{{background:url(\"data:image/svg+xml,%3Csvg fill='%23{old_maroon}'/%3E\")}}\n"
        f".d{{background:url(data:image/svg+xml;base64,{svg})}}\n"
        f".e{{content:url(data:text/plain;charset=utf-8;base64,{note})}}\n"
        f'import("data:text/javascript;base64,{script}")\n'
    )

    assert scan_tree(tmp_path) == [
        ("app.css", 3, f"%23{old_maroon}"),
        ("app.css", 4, f"#{old_maroon}"),
        ("app.css", 5, product),
        ("app.css", 6, abbreviation),
    ]


@pytest.mark.parametrize("error", [errno.EIO, errno.ENAMETOOLONG, errno.ELOOP])
def test_a_file_that_cannot_be_read_has_no_content(tmp_path, monkeypatch, error):
    """An I/O error, a name the file system refuses, a link loop: any OSError
    while reading a file leaves nothing to scan in it, never a crash."""
    product = "At" + "las"
    (tmp_path / "notes.md").write_text(f"{product}\n")

    def refuse(self, *args, **kwargs):
        raise OSError(error, os.strerror(error), str(self))

    monkeypatch.setattr(Path, "open", refuse)

    assert scan_tree(tmp_path) == []


#: A clean git environment: the developer's own config (templates, global
#: ignores, hooks) must not change what the fixture repo tracks.
_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
    )


def _write(root: Path, texts: dict[str, str]) -> None:
    for rel, text in texts.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@needs_git
def test_scan_repo_reads_tracked_files_only(tmp_path):
    """What CI checks out is what ships: tracked files, wherever they sit.
    Untracked and ignored files are local and are not read."""
    product = "At" + "las"
    folder = product.lower()
    _write(
        tmp_path,
        {
            "tracked.md": f"first line\nWelcome to {product}\n",
            f"{folder}/ok.py": "x = 1\n",
            "dist/bundle.js": f"{product}\n",
            "untracked.md": f"{product}\n",
            "ignored.md": f"{product}\n",
            ".gitignore": "ignored.md\n",
        },
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-f", "tracked.md", f"{folder}/ok.py", "dist/bundle.js")
    _git(tmp_path, "add", ".gitignore")

    assert scan_repo(tmp_path) == [
        (folder, 0, folder),
        ("dist/bundle.js", 1, product),
        ("tracked.md", 2, product),
    ]
    assert scan_repo(tmp_path / "dist") == [("bundle.js", 1, product)]


@needs_git
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_scan_repo_reads_no_content_from_special_or_vanished_files(tmp_path):
    """A tracked path whose file is gone, unreadable, a link, a folder or a
    pipe has no content to scan, and reading a pipe would hang the suite."""
    product = "At" + "las"
    names = ("gone.md", "locked.md", "pipe.md", "folder.md")
    _write(tmp_path, {name: f"{product}\n" for name in (*names, "target.md")})
    (tmp_path / "link.md").symlink_to("target.md")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-f", *names, "link.md")
    (tmp_path / "gone.md").unlink()
    (tmp_path / "pipe.md").unlink()
    os.mkfifo(tmp_path / "pipe.md")
    (tmp_path / "folder.md").unlink()
    (tmp_path / "folder.md").mkdir()
    locked = tmp_path / "locked.md"
    locked.chmod(0)
    readable = os.access(locked, os.R_OK)  # root reads it anyway
    try:
        hits = scan_repo(tmp_path)
    finally:
        locked.chmod(0o644)

    assert hits == ([("locked.md", 1, product)] if readable else [])


@needs_git
def test_dist_is_stale_when_older_than_a_tracked_source(tmp_path):
    """A bundle built before the last change to ``src`` or ``public`` would
    be scanned as it was, so it is rebuilt first. A file git does not track (an
    editor's backup, a scratch file) does not count, and a missing bundle is
    always built."""
    _write(
        tmp_path,
        {
            "src/main.ts": "export {};\n",
            "public/icon.svg": "<svg/>\n",
            "src/scratch.ts": "export {};\n",
            "dist/index.html": "<!doctype html>\n",
        },
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-f", "src/main.ts", "public/icon.svg")
    start = 1_700_000_000

    def touch(rel: str, seconds: int) -> None:
        os.utime(tmp_path / rel, (start + seconds, start + seconds))

    touch("src/main.ts", 0)
    touch("public/icon.svg", 0)
    touch("dist/index.html", 60)
    touch("src/scratch.ts", 120)
    assert _dist_is_stale(tmp_path) is False

    touch("public/icon.svg", 180)
    assert _dist_is_stale(tmp_path) is True

    touch("dist/index.html", 240)
    assert _dist_is_stale(tmp_path) is False

    (tmp_path / "dist" / "index.html").unlink()
    assert _dist_is_stale(tmp_path) is True


# -- the backend's own code: a hard gate today (Task 17) --------------------

#: The trees whose prompts, emails, Slack copy, comments and file names are
#: rewritten for Cubie. Their tests are left to Task 20's whole-repo gate: some
#: build old names from fragments on purpose.
BACKEND_TREES = ("rubik_core", "backend")
BACKEND_TESTS = "backend/tests"


def _is_backend_test(rel: str) -> bool:
    return rel == BACKEND_TESTS or rel.startswith(BACKEND_TESTS + "/")


def _area(rel: str) -> str:
    """``rubik_core/smart_teams/guide.py`` → ``rubik_core/smart_teams``. A file
    at the top of a tree (``backend/main.py``) is an area of its own."""
    return "/".join(rel.split("/")[:2])


def _backend_areas() -> list[str]:
    """Every package, module and file at the top of the backend trees."""
    return sorted(
        f"{tree}/{child.name}"
        for tree in BACKEND_TREES
        for child in (REPO_ROOT / tree).iterdir()
        if child.name not in SKIPPED_DIRS
        and not _is_backend_test(f"{tree}/{child.name}")
    )


@functools.lru_cache(maxsize=1)
def _backend_hits() -> dict[str, list[tuple[str, int, str]]]:
    """One scan of both trees, grouped by area, paths from the repo root."""
    by_area: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for tree in BACKEND_TREES:
        for path, line_no, match in scan_repo(REPO_ROOT / tree):
            rel = f"{tree}/{path}"
            if not _is_backend_test(rel):
                by_area[_area(rel)].append((rel, line_no, match))
    return dict(by_area)


@pytest.mark.parametrize("area", _backend_areas())
def test_backend_code_has_no_legacy_brand(area):
    """Prompts say Cubie and read the company from its profile. Emails, Slack
    posts, error messages, comments, docstrings and file names name no old
    product, company, client, cloud id or colour."""
    hits = _backend_hits().get(area, [])
    assert hits == [], _describe(hits)


# -- what the renames left behind in that code -------------------------------

#: Authored code: the backend trees (tests aside) and the operator scripts.
CODE_ROOTS = (*BACKEND_TREES, "scripts")
DOCS = REPO_ROOT / "docs"
#: The Help mirror is rewritten together with the frontend Help pages and is
#: guarded by ``test_help_chat_link.py``; its comments are left to that change.
HELP_MIRROR = "rubik_core/help/"


@functools.cache
def _text_files(roots: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """``(path from the repo root, text)`` of every authored text file under
    ``roots``. Backend tests, skipped trees, links and binaries are left out."""
    found: list[tuple[str, str]] = []
    for root in roots:
        base = REPO_ROOT / root
        paths = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in paths:
            parts = path.relative_to(REPO_ROOT).parts
            rel = "/".join(parts)
            if (
                not path.is_file()
                or path.is_symlink()
                or _is_backend_test(rel)
                or SKIPPED_DIRS.intersection(parts)
            ):
                continue
            data = path.read_bytes()
            if b"\0" in data[:BINARY_SNIFF_BYTES]:
                continue
            found.append((rel, data.decode("utf-8", errors="replace")))
    return tuple(found)


def _line_hits(
    pattern: re.Pattern[str], files: tuple[tuple[str, str], ...]
) -> list[tuple[str, int, str]]:
    return [
        (rel, line_no, match.group())
        for rel, text in files
        for line_no, line in enumerate(text.splitlines(), start=1)
        for match in pattern.finditer(line)
    ]


#: Code whose comments the citation pins read: the authored code above, the
#: migration environment and the frontend source.
CITING_ROOTS = (*CODE_ROOTS, "alembic", "frontend/src")
#: The old company's sales-workshop notes. They are deleted, not rewritten,
#: so every citation of them is a dead end.
SALES_KAIZEN_DOCS = "docs/sales_kaizen/"
#: Files that name deleted docs on purpose: the pins that keep them deleted,
#: and the two files of this guard, whose patterns quote the labels.
CITATION_EXEMPT = frozenset(
    {
        "backend/tests/test_removed_modules.py",
        "backend/tests/test_no_legacy_brand.py",
        "backend/tests/_legacy_brand.py",
    }
)

#: ``docs/…`` standing on its own, not the tail of a URL or of another path.
_DOC_PATH = re.compile(r"(?<![\w./-])docs/[\w./-]*[\w/]")
#: A sales-workshop doc by its path, its number or its name: "04-IMPL §3",
#: "INTERFACE-SHEET".
_SALES_KAIZEN_PATH = re.compile(r"(?<![\w./-])docs/sales_kaizen\b[\w./-]*")
_SALES_KAIZEN_DOC = re.compile(
    r"(?<![\w-])(?:\d{2}-(?:SPEC|REF|IMPL|RISKS|ADDENDUM|CODEBASE|DECISION)[\w-]*"
    r"|INTERFACE-SHEET|DECISION-BRIEF|IMPLEMENTATION-STATUS|CODEBASE-CONTRACT)"
)
#: A use-case doc by its file name: ``uc40_priorities_pipeline_scheduler.md``.
_USE_CASE_DOC = re.compile(r"(?<![\w/])uc\d+(?:_\d+)?_\w+\.md")
#: The old deployment's change log (deleted with its staff names and ids).
_DEPLOY_LOG = re.compile(r"\bDEPLOY_LOG\b")


def _doc_exists(cited: str) -> bool:
    path = cited.rstrip("/")
    if f"{path}/".startswith(SALES_KAIZEN_DOCS):
        return False
    return (REPO_ROOT / path).exists()


def _never(_cited: str) -> bool:
    return False


#: Any doc path, in code.
_CODE_CITATIONS = ((_DOC_PATH, _doc_exists),)
#: The deleted planning docs, in any tracked file.
_PLANNING_CITATIONS = (
    (_SALES_KAIZEN_PATH, _never),
    (_SALES_KAIZEN_DOC, _never),
    (_USE_CASE_DOC, lambda cited: (DOCS / cited).exists()),
    (_DEPLOY_LOG, lambda cited: (REPO_ROOT / f"{cited}.md").exists()),
)


#: A decision label from the deleted sales-kaizen interface sheet ("D-6"). The
#: sheet is gone, so the label points nowhere whatever it is attached to.
_INTERFACE_SHEET_DECISION = re.compile(r"\bD-\d+\b")


@functools.cache
def _tracked_text_files() -> tuple[tuple[str, str], ...]:
    """``(path, text)`` of every git-tracked text file but the exempt ones and
    the workshop notes themselves (pinned deleted below)."""
    found: list[tuple[str, str]] = []
    for rel in _legacy_brand.tracked_files(REPO_ROOT):
        if rel in CITATION_EXEMPT or rel.startswith(SALES_KAIZEN_DOCS):
            continue
        text = _legacy_brand._read_text(REPO_ROOT / rel)
        if text is not None:
            found.append((rel, text))
    return tuple(found)


def _dead_citations(
    citations: tuple, files: tuple[tuple[str, str], ...]
) -> list[tuple[str, int, str]]:
    return [
        hit
        for pattern, exists in citations
        for hit in _line_hits(pattern, files)
        if not exists(hit[2])
    ]


def test_code_cites_only_docs_that_exist():
    """A comment that sends the reader to a deleted doc (the sales-workshop
    notes, the use-case write-ups, the old deploy log) is a dead end. Drop the
    citation and keep whatever explanation is still true.

    The deleted planning docs may be cited nowhere in the tracked tree; any
    other doc path, nowhere in the code or the frontend source. Bare use-case
    labels ("UC-46 CRM Sync") are names, not citations, and may stay; a bare
    interface-sheet decision label ("D-6") is a citation of a deleted doc, so it
    may not, in any Python file of the backend trees (the Help mirror too) or
    anywhere in the frontend source."""
    code = tuple(
        (rel, text)
        for rel, text in _text_files(CITING_ROOTS)
        if not rel.startswith(HELP_MIRROR)
    )
    hits = _dead_citations(_CODE_CITATIONS, code)
    hits += _dead_citations(_PLANNING_CITATIONS, _tracked_text_files())
    labelled = tuple(
        (rel, text)
        for rel, text in _text_files((*BACKEND_TREES, "frontend/src"))
        if rel.endswith(".py") or rel.startswith("frontend/src/")
    )
    hits += _line_hits(_INTERFACE_SHEET_DECISION, labelled)
    hits = sorted(set(hits))
    assert hits == [], _describe(hits, "citations of docs that do not exist")


def test_the_sales_workshop_notes_are_deleted():
    """``docs/sales_kaizen`` kept the old company's internal workshop minutes:
    session quotes, prices, register rows, staff departures. The
    design that is still true lives in the code it explains."""
    left = [
        rel
        for rel in _legacy_brand.tracked_files(REPO_ROOT)
        if rel.startswith(SALES_KAIZEN_DOCS)
    ]
    assert left == [], f"still tracked: {left}"


#: A migration by its old ``zc…`` revision id: ``zc055``, ``zc00000000040``.
_MIGRATION_ID = re.compile(r"(?<![A-Za-z0-9])zc\d+(?!\d)")
_REVISION = re.compile(r"""^revision\s*(?::[^=]*)?=\s*["']([^"']+)["']""", re.M)


def _known_revisions() -> set[str]:
    """The revision ids ``alembic/versions`` defines today."""
    return {
        revision
        for path in (REPO_ROOT / "alembic" / "versions").glob("*.py")
        for revision in _REVISION.findall(path.read_text(encoding="utf-8"))
    }


def test_code_cites_no_deleted_migration():
    """Task 19 squashed the migrations into one baseline, so the old ``zc…``
    revisions are gone. A comment that dates a column or a rule by one sends
    the reader nowhere: keep the reason, drop the id. (Backend tests may still
    use such an id as a fixture value; the migration guard's own tests do.)"""
    known = _known_revisions()
    assert known, "alembic/versions defines no revision"
    hits = [
        hit
        for hit in _line_hits(_MIGRATION_ID, _text_files(CITING_ROOTS))
        if hit[2] not in known
    ]
    assert hits == [], _describe(hits, "deleted migration ids cited in code")


#: A Cloud Run revision: the service, a five-digit counter and a short suffix.
_REVISION_ID = re.compile(r"\bcubie(?:-[a-z]+)*-\d{5}-[a-z0-9]{3}\b")


def test_no_revision_id_is_cited_as_history():
    """The old service's incident revisions were renamed by the rebrand into
    ids that never existed. Keep the lesson; drop the id. (Tests may still use
    such a name as a fixture value.)"""
    hits = _line_hits(_REVISION_ID, _text_files((*CODE_ROOTS, "requirements.txt")))
    assert hits == [], _describe(hits, "revision ids cited in code")


_AN_PRODUCT = re.compile(rf"\b[Aa]n {PRODUCT_NAME}\b")


def test_the_product_name_takes_a_not_an():
    """A mechanical rename leaves "an" in front of a name that no longer
    starts with a vowel."""
    hits = _line_hits(_AN_PRODUCT, _text_files(CODE_ROOTS))
    assert hits == [], _describe(hits, f'"an {PRODUCT_NAME}" slips')


#: Where Insights computes and stores the day a thing happened on.
INSIGHTS_CODE = ("rubik_core/insights", "rubik_core/database/insights.py")
#: "the org's business-timezone day (America/Chicago)", "org-wide
#: America/Chicago", "on the org clock (America/Chicago)": the old company's
#: clock described as the business day, within a sentence that may wrap.
_OLD_CLOCK_AS_BUSINESS_DAY = re.compile(
    r"\b(?:business|org)\b[^.]{0,60}America/Chicago"
)


def test_the_business_day_is_the_companys_own():
    """The business day is the company's own time zone (UTC until an admin sets
    one), so no docstring or comment may call the old company's clock the
    business day. Naming a zone as an IANA example is fine."""
    insights = _line_hits(re.compile("Chicago"), _text_files(INSIGHTS_CODE))
    described = [
        (rel, text.count("\n", 0, match.start()) + 1, " ".join(match.group().split()))
        for rel, text in _text_files(BACKEND_TREES)
        for match in _OLD_CLOCK_AS_BUSINESS_DAY.finditer(text)
    ]
    hits = insights + described
    assert hits == [], _describe(hits, "mentions of the old company's clock")


def test_access_gates_name_roles_not_people():
    """The cockpit's roll-ups and worklists belong to roles. The people who
    first held those roles at the old company are not part of the product."""
    source = (REPO_ROOT / "backend" / "auth.py").read_text(encoding="utf-8")
    people = re.findall(r"\(([A-Z][a-z]+)'s view\)", source)
    assert people == [], f"backend/auth.py names a person's view: {people}"
