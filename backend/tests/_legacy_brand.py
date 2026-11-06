"""Banned-terms scan shared by the legacy-brand guards.

The codebase used to ship under another company's name, product names,
clients, staff, cloud ids and palette. ``LEGACY_PATTERN`` matches the names,
ids and colours; the name digests match its people (staff and an outside
contact) and its clients. ``legacy_terms`` applies both to a text.
``scan_repo`` reports every hit in the git-tracked files under a folder, and
``scan_tree`` every hit in a folder on disk (the built frontend), in file
contents and in file and folder names.

Every term below is assembled from fragments, and names of people and clients
are stored only as digests, so neither this module nor the tests that import it
ever match the scan themselves. Test modules import this helper; they never
import one another.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import os
import re
import stat
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]

# Version-control, vendored, built and bytecode trees: never authored files.
SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "dist", "__pycache__"})

# A file counts as binary when a NUL byte shows up in its first 8 KB.
BINARY_SNIFF_BYTES = 8 * 1024

# Lock-file digests (npm SRI ``sha512-…``, uv/pip ``sha256:…``) are random
# base64 or hex and can spell a short term by chance, so they are blanked out
# before matching.
_DIGEST = re.compile(r"sha(?:1|256|384|512)[-:][A-Za-z0-9+/]{16,}={0,2}")

# Inline base64 payloads of binary media (``data:font/woff2;base64,…``, a
# bundled image) are encoded bytes, not text, and can spell a short term by
# chance too, so they are blanked. A text payload (an SVG, plain text, a script
# or JSON) is decoded and scanned like any other text, and a ``data:`` URI that
# is not base64 is read as it stands.
_BASE64_DATA_URI = re.compile(
    r"data:(?P<type>[\w.+/-]*)(?:;[\w.+=-]+)*;(?i:base64),"
    r"(?P<payload>[A-Za-z0-9+/\\]+={0,2})"
)

# RFC 2397: a ``data:`` URI that names no media type is ``text/plain``.
_TEXT_MEDIA_TYPES = frozenset(
    {
        "",
        "application/ecmascript",
        "application/javascript",
        "application/json",
        "application/x-javascript",
        "application/xml",
    }
)


def _is_text_media(media_type: str) -> bool:
    kind = media_type.lower()
    return (
        kind in _TEXT_MEDIA_TYPES
        or kind.startswith("text/")
        or kind.endswith(("+xml", "+json"))
    )


def _decoded_text(payload: str) -> str:
    """A base64 payload as text. One that does not decode has none. A bundler
    may escape the slashes (``\\/``) of a payload inside a string."""
    data = payload.replace("\\", "")
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


# Every branch opens with its literal and checks the boundary in front of it
# afterwards, with a lookbehind that spans the literal. A branch that opens
# with a lookbehind is tried at every character and triples the scan time.


def _standalone(term: str) -> str:
    """``term`` not glued to another letter or digit (``_``, ``-``, ``.`` split)."""
    return rf"{term}(?<![0-9A-Za-z]{term})(?![0-9A-Za-z])"


def _whole_number(digits: str) -> str:
    return rf"{digits}(?<!\d{digits})(?!\d)"


def _product_name(word: str) -> str:
    """A product name as a whole token in any casing or identifier style.

    Prose, snake_case, kebab-case, SCREAMING_CASE and camelCase spellings all
    match. The name glued to more letters of its own case does not, so a
    longer, unrelated word or person's name that contains it is left alone.
    """
    lower, title, upper = word.lower(), word.capitalize(), word.upper()
    return (
        rf"{title}(?![a-z])"
        rf"|{lower}(?<![a-z]{lower})(?![a-z])"
        rf"|{upper}(?<![A-Z]{upper})(?![A-Z])"
    )


def _colour(hex6: str) -> str:
    """One palette colour as hex, as a 0x byte triplet, or as decimal r, g, b.

    Hex after ``#``, a URL-encoded ``%23`` (an inline SVG) or ``0x`` may end
    in a two-hex alpha (``#RRGGBBAA``) or in anything but a digit. Bare hex is
    not glued to another digit, since the same six characters inside a longer
    number are not the colour, unless the eight characters form one hex token
    that opens with an alpha byte (``00RRGGBB`` from openpyxl, ``FFRRGGBB`` in
    a spreadsheet).
    """
    pairs = [hex6[i : i + 2] for i in (0, 2, 4)]
    red, green, blue = (str(int(pair, 16)) for pair in pairs)
    return "|".join(
        [
            rf"(?:#|%23|0x){hex6}(?:[0-9a-f]{{2}}(?![0-9a-f])|(?!\d))",
            rf"{hex6}(?<!\d{hex6})(?!\d)",
            rf"{hex6}(?<=(?<![0-9a-f])[0-9a-f]{{2}}{hex6})(?![0-9a-f])",
            r",\s*".join("0x" + pair for pair in pairs),
            rf"{red},(?<!\d{red},)\s*{green},\s*{blue}(?!\d)",
        ]
    )


_PRODUCT_NAMES = ["At" + "las", "Bri" + "an", "Jar" + "vis"]

_COMPANY = [
    # Full name and email domain first, so a hit reports the longer form.
    "mqm" + r"[\s_-]*" + "research",
    "mq" + "mr",
]

_CLIENT_AND_SISTER_COMPANY = [
    "ato" + "mus",
    _standalone("s" + "qc"),
]

# The old company's vendor-management service line, which also named one of
# its departments and that department's L10. The letters mean nothing else, in
# any case, so a slug or a variable named after it counts too.
_OLD_SERVICE_LINE = [_standalone("hq" + "vm")]

# Another of its departments, by its three-letter abbreviation. Matched in
# capitals only: in lower case the same letters are a common abbreviation (a
# binary search tree, a bibliography style file). The tracked tree uses the
# capitals for nothing else, no time zone included.
_CASE_SENSITIVE = [_standalone("BS" + "T")]

_CLOUD_AND_CRM_IDS = [
    *(
        _whole_number(digits)
        for digits in (
            "4746" + "18",  # GCP project suffix
            "5947116" + "09963",  # GCP project number / OAuth client
            "52183" + "64",  # HubSpot portal
            "81918" + "15",  # HubSpot pipelines
            "129439" + "50",
            "122546" + "39",
            "5685794529" + "555251200",  # Vertex RAG corpus
            # The old portal's custom deal stages: Quote/Proposal, Drafting
            # SOW, Ops Involved, Pending Final Contract Sign-Off, Hold, and
            # its two numeric closed-won stages.
            "13082" + "11840",
            "226" + "72671",
            "13415" + "81641",
            "13082" + "18660",
            "255" + "55726",
            "238" + "37821",
            "237" + "10249",
            # A contact record in the old portal.
            "2410742" + "37003",
            # A reply and its thread in the old Slack workspace, as Slack
            # timestamps and in a permalink's ``p…`` form.
            r"1783706295\." + "536819",
            r"1783699183\." + "497849",
            "1783706295" + "536819",
            "1783699183" + "497849",
        )
    ),
    # The old portal's two closed-won stage UUIDs.
    "bd52579e" + "-3251-4260-b791-" + "f02f122283c0",
    "a277ef6d" + "-eaf4-4680-b603-" + "f2df96048a9d",
    # The old Slack workspace's channels.
    *(
        _standalone(channel)
        for channel in ("C09M4" + "34U5NV", "C0ARR" + "B8PV3M", "C0BGH" + "0R11UN")
    ),
]

# The old portal's custom properties, by internal name: its industry's
# company vendor fields, and the deal and company fields Customer X-Ray, the
# turn-times answer and AI Priorities used to read by name (each is now a
# setting). Generic words it also used as property names (a loan system's
# ``los``, ``product_interest``, ``contract_link`` …) are left out: a new
# workspace may name its own fields that way.
_PORTAL_PROPERTIES = [
    _standalone(name)
    for name in (
        "client_status" + "__cloned_",
        "active_lead" + "_status",
        "company_co" + "_owner",
        "contract_auto" + "_renews",
        "auto_renewal_one" + "_time_engagement",
        "contract_renewal" + "_date",
        "ongoing" + "_engagements",
        "one_time" + "_engagements",
        "product" + "_services",
        "turn" + "_time",
        "sub" + "servicer",
        "sub" + "servicer_oversight_vendor",
        "internal_audit" + "_vendor",
        "doc_" + "custodian",
        "doc_" + "custodian_vendor",
        "doc_prep" + "_company",
        "servicing" + "_qc_vendor",
        "post_closing" + "_qc_vendor",
        "pre_funding" + "_qc_vendor",
        "fair_lending" + "_vendor",
        "mers" + "_vendor",
        "aml_training" + "_vendor",
        "hmda" + "_vendor",
        "compliance_vendor" + "_advisor",
        "vendor_management" + "_vendor",
        "settlement_agent" + "_oversight",
        "nmls" + "_id",
        "lending" + "_channels",
    )
]

_OLD_PALETTE = ["8527" + "33", "6a1f" + "29", "5E1B" + "24", "f4e6" + "e8"]

_CASE_INSENSITIVE = [
    *_COMPANY,
    *_CLIENT_AND_SISTER_COMPANY,
    *_OLD_SERVICE_LINE,
    *_CLOUD_AND_CRM_IDS,
    *_PORTAL_PROPERTIES,
    *(_colour(hex6) for hex6 in _OLD_PALETTE),
]

# Product names carry their own casing rules, and the case-sensitive terms are
# matched as written; everything else ignores case.
LEGACY_PATTERN: re.Pattern[str] = re.compile(
    "|".join(
        [
            *(_product_name(name) for name in _PRODUCT_NAMES),
            *_CASE_SENSITIVE,
            "(?i:" + "|".join(_CASE_INSENSITIVE) + ")",
        ]
    )
)


# The previous company's staff, by name. Spec §4.2.2 lets the scan store a
# term hashed, so no name is kept in clear: each entry is the sha256 hex digest
# of a lower-case word (accents dropped). The people who build Cubie at
# RubikSoft are not staff of the previous company and are not on the list.
#
# A login handle, a nickname, or a name rare enough to stand alone, is one
# word. A surname written in several words counts as one ("de la x", "o'x"),
# and so does a handle glued into a camelCase identifier.
STAFF_TOKEN_DIGESTS: frozenset[str] = frozenset(
    {
        "10c4548280c82a4bf6249ae1f12d55b090f9f6667707628dadc6fce11d44ce2b",
        "1f9307ff57abbfb96f23d0239479dc0c8837915ef8015095ab733e512bfd58f0",
        "21fb7cd729e01ab5a9a4a5877252f51ffb9dc0b1e2028ec6212d943f5aaf82a4",
        "232e59da6b8eeceb0e623888d877b0a20d6c589f2911068b276472d5db751a2c",
        "2423747b49c0ac2731c5b2f9540f39a1e026044c754d2134cb117a94931b883c",
        "2e5cb6b554fbc883fb9b4b430dac24a64d12685fa547e4f21d959fd0db188c27",
        "32658718b1fc90acce30cbf58b3d38794fb2bafbbd6ace33e748d45eb6931db0",
        "3b58e79bc4aefb1f40251900ba89e98c83721003ec375110f3748516f605a960",
        "3fce0e0311d9cd449da08c79476963f68e5ef8840476069c5f65e46d220d014a",
        "423dd6e18c800c5dd36be20c0c289c4da9d2d16a4afe52f891b1a1674912d4f2",
        "62a5b544a58996807f33b79ba5ad26ada499d27ec316aa65e3b7ccd1f2b44a58",
        "6a81862a20195f7c516c8cc8df7e1df16a960619e57bdbadf127002ca93c5f12",
        "7def2979f3170429d692f02b21ec31a01c6d4853cf265051fef2cb7775f2ffa1",
        "7fd9a918f862879baf023215e1fff1186da89318df49cbdaec1a1c156169287e",
        "889a20118b2a794bd9b03b6b8074fdd9ad2d8aaa5620fabf2df7dd5890306b99",
        "8a09ba307931236a499ef446340feed8ad4a51c7ff873ee0f73f45b0ebf7c3f0",
        "8b532a90da09520b9585c6c8d51ff35c2df77d8b0f67bc671ad622aade21e22f",
        "8f794b7df1331d360b141267f4e1f96ac85ee0649b0ee692f2aab0297d85deaa",
        "8fb137addcdba0694860b777b4ad72d3ff52260dbb584bf3e02e29798642570c",
        "94a6e0524e4ec8079f93ec94e74f24d13f39ded17366269ff27022af726d4de0",
        "9a1cbf52a5ac6bd7dc25175685e7d0636a319783bca9b999455392da99c28c6c",
        "b4cb6cb33fe4b865868de825023a1e2790dc12ac01ecc8d7c5afe8254071c8ba",
        "bab5ccef4ddfec358fe72c61d41a4c002abad8baa2732e9b8deaa563e92cc3a5",
        "c5a514329eb1f585cd9c8298acd7d6107cbd931c833c4510bcacae5e12197372",
        "ca9e6ea121f48c4d4e2f30a889dc510d07ebaa947567bca3506e22af9ef83134",
        "d129a820afbcdc2c995d481ad1b197c1ebe9c37a390d6c7f17bce8beebe8b9af",
        "d9f7df6cbcd918bbecfb1b40b7a2d770bbe2969b0c14ba60f55634cb7124a3ce",
        "dabaf496e8405af0d5794a3ea3519bbf99a1de0749da6e27f98d3a13498c9ab7",
        "e82891908fafa094fc1c6f3086a7b7e2c0b1b83e6766c420ad417450af99e601",
        "e84fff77b7ea143b8afb05d7ceed7db4f97a6641e6fafed72ec10e39f6c47fdf",
        "fe78cd5f21e8338cee599a2ac9cb778baab7cf0ebebd1e27e3d6832e8d525ea2",
        "ff5f205252dfd3cf34dc8ae06c14b2ef2a41a7c46c65afb91039e8800bc0e63d",
    }
)

# A first name followed by a common surname (one that is also an ordinary word
# or a common name, so it cannot stand alone): the digest of the first name and
# the digest of "first last". The two words match when adjacent, whatever
# separates them ("First Last", "first.last", "FIRST_LAST").
STAFF_FULL_NAME_DIGESTS: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "026ad9b14a7453b7488daa0c6acbc258b1506f52c441c7c465474c1a564394ff",
            "1309af7c94a1a70bc71ed7d6a3c0c052826080d52396bd1aecca63996d6d58e6",
        ),
        (
            "26db532931d57a616e72a35f9fc1c26643e36d113c70837541cd37b106d08678",
            "6a305cc96fe832f86f1f37dc88a23b0d9a091eb25faf60f73e626fc42d38a656",
        ),
        (
            "34550715062af006ac4fab288de67ecb44793c3a05c475227241535f6ef7a81b",
            "5f9fcb81830c683eb35e6b0e3951c3c034b105206c86406c3e50bc5c89342920",
        ),
        (
            "395acd169ca407da8ba4ce63046bbea547468235c2823b28d28a4d66f1bd7bb6",
            "bc8789cea2fb1fa93eaa2778503f2b9d1e57232d287a46c037415ab537d694d4",
        ),
        (
            "5e608d7beca7513cab18e6a396ea894f2c5466379dcc1e3888cc23d685aea600",
            "e30f64dfca5eedf810182883b554ba4dab734c54911d10d4c7bbf5d6bb6f61bf",
        ),
        (
            "64b4d0f47c93ce23d157e68a58767356283dc9b63c459d45d0e0e39b3a64b9b9",
            "0861ce70f81e4dff4030e07542f9f83f98acf105d720c28f0c9d15e1429f0152",
        ),
        (
            "9501a3a90b603b488a23a064637b0c165099a72281b4eb8e81500f7880b1f284",
            "ddc63901159e7ce446b819c8bff03993e792bff160325c818b3762aaa4864f8c",
        ),
        (
            "b78469618fb15871b9508defd1ff70014747c1f918e4185425c5f2bbea2a4e5d",
            "80fbbe0d0d119b8e45b9aa1848a136b1c16cd322fc7e53ad8c99c5499d92b0a2",
        ),
        (
            "b918c3f9116ce3a61efd71a1bada0d46c8f575283ce5d5798454a4abfb56ae79",
            "22010cdd38b643fe7048673d9fe23f5e9faaa6ea53d7d11a9894c7c999c14d60",
        ),
        (
            "bb44bf07cf9a2db0554bba63a03d822c927deae77df101874496df5a6a3e896d",
            "ed73a9b9e4275037776abd90fa0c89f3ebc547d0ac62ca5be31809d0c77d94a0",
        ),
        (
            "bd3dae5fb91f88a4f0978222dfd58f59a124257cb081486387cbae9df11fb879",
            "2e334f4c2963f30447bd418f2471e0077ec70281630c69a4de6bcb9a709addee",
        ),
        (
            "d84fe7e07bedb227cffff10009151d96fc944f6a1bd37cff60e8e4626a1eb1c3",
            "df68a1f932811933517068b4767600e111f23d961e3ffd4492137f956a81b75b",
        ),
        (
            "ef27508d7ad1ab8e003dc07195d1f3c274f4ecbf5509c01cec4eb304dfb5b9bd",
            "69b9642d0f48b4c91c06ac1077540337681c9a9288b5ca4cdb378e5e97ee4a35",
        ),
    }
)

# Common surnames of staff on the full-name list above. Alone they are ordinary
# words, so they match only as the value of a surname field
# (``last_name="…"``): a fixture person who kept a real surname under a new
# first name is still the real person. The RubikSoft founder's surname is left
# out: it is the vendor's own name, not the old company's.
STAFF_SURNAME_DIGESTS: frozenset[str] = frozenset(
    {
        "43762f7506b7249e54aaec56c5429982b7401dc6716ed08898cab1a067d0bcaa",
        "57fce44d7c6df51ad8525da1580a246e9d1142d79d1d1f176b1d29643d61ed44",
        "6933a3e55ac582fdf4e4ef8811dd58fc796e4651442fd3727f19cb0baabe24c9",
        "712df00b36e4528d3e317251adf8e602d17a5380001ac65d7c1b5aa3300a2084",
        "78cde64c3e47f2cbfd9da721f54aacde33779916683c79de86962898feefac21",
        "9706987d633e05a0d2931c21108b37e8c790570048ac47c48900314bb325d606",
        "accb7fc4d0144dcaa760e485a8100fc977f3404d3bfdc8e589384f66ca934330",
        "b57e9659b5eae711a041b1fc7f7510e529b5b4df8b364157732a5cfec9ad7a45",
        "b771e9e64666c83251ddcf8a999333189105cc492b44e17650f92321b04ecada",
        "d02587c28a239476a1db9da099286bd259850e18ceaf6c59123ecfc9855de4b6",
        "ef94814c0bb166ebb20339d63de73a69c89488a04ed28d8cbab3881a83c9828a",
    }
)

# Real people outside the old company who appear in its data (an attendee
# researched from a real profile), by a name rare enough to stand alone.
CONTACT_TOKEN_DIGESTS: frozenset[str] = frozenset(
    {"5531042c1c699c430640ec41734ec87376866535f6820a90bca6ae12181ada99"}
)

# The old company's clients and the industry bodies it dealt with, taken from
# its live CRM: a distinctive name, a glued email domain, an association.
CLIENT_TOKEN_DIGESTS: frozenset[str] = frozenset(
    {
        "06e660f147293ea56daa16e91e85b1940d60d5197efb37f6da5a833299965e2a",
        "0ec14e62e17e8ea53f05dd24cc912bb11888c574cc206d28020c4803755544c2",
        "4cc7a4b341d8ce3315ea112ebb01e664bd328e88781db0f4bbc0e3f65e201bf9",
        "5a20fe2f1b70d1150712b032c84f5998ac797d1bd9c84ed2ada77866ea030287",
        "5e66cc54b8272de984d66d0e35438693168000d44f408058a70995cae8e10e01",
        "8846f7fa7c7fc03b2ad95e13ce741809338ee0fcce7824f5d9033b5ca1b6d4e5",
        "a6198218dbbc648c2e66b26467681b320caf09ede622351b53aae711fff30e8e",
        "c3608ae41a88c1294f016bd2f13ccc17d404af9dcd500bc3071b3d9e4537621a",
    }
)

# Clients whose name is two or three common words that only together name
# them. Each entry is the digest of every prefix of the name, so the scan can
# follow it word by word: ("a", "a b", "a b c").
CLIENT_NAME_DIGESTS: frozenset[tuple[str, ...]] = frozenset(
    {
        (
            "3b80d38f7686a8b5f8e61ad562ec069ac172732fb4dab946401f21a438669a4b",
            "27c7c84e925332bb3202e8b8b4c132c8d4cd45343b229a599a201067c95fae52",
        ),
        (
            "5873102fa0951713a81154785217660a3a116274481a19fb2eea5c56182f4860",
            "cceb666ec0dc9bbdc2be2009256ee5872f24b2d9608937d6ab06bb15ccfb3d83",
        ),
        (
            "707ff1c8a83dad594edba8737452858e66f6029a7c78e57b282c0171fa17ee81",
            "8cc2bff1c453f6835c68fec04977912d14ef5c1811bffcea57e7162f3a08a666",
        ),
        (
            "a7937b64b8caa58f03721bb6bacf5c78cb235febe0e70b1b84cd99541461a08e",
            "8abed21821859e1aac90d96ab30cb506e6940cd0147ba34691f3141ca20d4429",
            "326a50ab102b1c4ef30133b3c5153b111a7ef94fa42c1b936804fa8bf1ff4fab",
        ),
        (
            "ca52e7d1c18961a8c5b3947a54e48fbf4ee70b66ea413cf7814f1c962631fd58",
            "86bc9911fde154eb880a1a451566f560ac4f670d28af6afc5b97447b7397951b",
        ),
        (
            "dca6ec9510fc0176c600bb5d75a919fba07877c74eb1a41b0530b330c5767648",
            "cb52492a89702eab49cbaafd4e6740f088cadfc7377fa8c81c3bd7b4da468b58",
        ),
    }
)


def _accent_folding() -> dict[int, str]:
    """Each accented Latin letter to its plain letter, one character for one,
    so offsets survive the fold."""
    table: dict[int, str] = {}
    for code in (*range(0xC0, 0x250), *range(0x1E00, 0x1F00)):
        base = unicodedata.normalize("NFD", chr(code))[0]
        if base != chr(code) and base.isascii() and base.isalpha():
            table[code] = base
    return table


_ACCENT_FOLDING = _accent_folding()

# A word, split at camelCase humps: ``ownerEmail`` is two words, ``HTMLParser``
# too, ``McKay`` too.
_NAME_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")
# What may sit between the words of one surname: nothing (glued), a space, an
# apostrophe or a hyphen.
_SURNAME_JOINS = frozenset({"", " ", "'", "’", "-"})
# The particles a surname of several words opens with.
_SURNAME_PARTICLES = frozenset(
    {"d", "da", "de", "del", "della", "der", "di", "dos", "du"}
    | {"la", "le", "mac", "mc", "o", "st", "van", "von"}
)
_MAX_SURNAME_WORDS = 3
# "Last, First", as a directory or a spreadsheet lists people.
_LAST_FIRST = re.compile(r"\s*,\s*")
# A surname field and its quoted value, in Python, JSON or TypeScript.
_SURNAME_FIELD = re.compile(
    r"(?<![A-Za-z])(?i:last_?name|surname|family_?name)(?![A-Za-z])"
    r"""["']?\s*[:=]\s*["']([^"'\n]{1,60})["']"""
)
_NOT_A_LETTER = re.compile(r"[^a-z]+")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Single words repeat endlessly in source, so their digests are cached.
_digest = functools.lru_cache(maxsize=1 << 16)(_sha256)


@dataclass(frozen=True)
class _NameIndex:
    tokens: frozenset[str]
    starts: frozenset[str]
    continues: frozenset[str]
    ends: frozenset[str]
    reversible_firsts: frozenset[str]
    reversible_ends: frozenset[str]
    surnames: frozenset[str]


@functools.lru_cache(maxsize=16)
def _name_index(
    tokens: frozenset[str],
    phrases: frozenset[tuple[str, ...]],
    people: frozenset[tuple[str, ...]],
    surnames: frozenset[str],
) -> _NameIndex:
    every = phrases | people
    return _NameIndex(
        tokens=tokens,
        starts=frozenset(name[0] for name in every),
        continues=frozenset(prefix for name in every for prefix in name[1:-1]),
        ends=frozenset(name[-1] for name in every),
        reversible_firsts=frozenset(name[0] for name in people if len(name) == 2),
        reversible_ends=frozenset(name[-1] for name in people if len(name) == 2),
        surnames=surnames,
    )


#: A word as ``(offset, end, lower-case text)`` in the folded text.
_Word = tuple[int, int, str]


def _glued_runs(words: list[_Word], folded: str) -> list[_Word]:
    """The words merged wherever nothing separates them: the letters of an
    identifier as one word (``DuBois``, ``qzorblatEmail``)."""
    runs: list[_Word] = []
    for begin, end, word in words:
        if runs and runs[-1][1] == begin:
            opened = runs[-1][0]
            runs[-1] = (opened, end, folded[opened:end].lower())
        else:
            runs.append((begin, end, word))
    return runs


def _token_spans(
    words: list[_Word], runs: list[_Word], folded: str, tokens: frozenset[str]
) -> set[tuple[int, int]]:
    """Single-word names: a word, a glued run, or a surname of several words
    that opens with a particle ("van x", "o'x", "de la x")."""
    spans = {
        (begin, end) for begin, end, word in (*words, *runs) if _digest(word) in tokens
    }
    for at, (begin, _end, word) in enumerate(words):
        if word not in _SURNAME_PARTICLES:
            continue
        joined = word
        for after in range(at + 1, min(at + _MAX_SURNAME_WORDS, len(words))):
            if folded[words[after - 1][1] : words[after][0]] not in _SURNAME_JOINS:
                break
            joined += words[after][2]
            if _digest(joined) in tokens:
                spans.add((begin, words[after][1]))
    return spans


def _phrase_spans(
    words: list[_Word], folded: str, index: _NameIndex
) -> set[tuple[int, int]]:
    """Names of several words, followed word by word through their prefix
    digests, and two-word people also as "Last, First"."""
    spans: set[tuple[int, int]] = set()
    partial: list[tuple[int, str]] = []
    for at, (begin, end, word) in enumerate(words):
        digest = _digest(word)
        longer: list[tuple[int, str]] = []
        for opened, phrase in partial:
            extended = f"{phrase} {word}"
            extended_digest = _sha256(extended)
            if extended_digest in index.ends:
                spans.add((opened, end))
            if extended_digest in index.continues:
                longer.append((opened, extended))
        if digest in index.starts:
            longer.append((begin, word))
        partial = longer
        if digest in index.reversible_firsts and at:
            last_begin, last_end, last = words[at - 1]
            if (
                _LAST_FIRST.fullmatch(folded, last_end, begin)
                and _sha256(f"{word} {last}") in index.reversible_ends
            ):
                spans.add((last_begin, end))
    return spans


def hashed_name_hits(
    text: str,
    tokens: frozenset[str],
    phrases: frozenset[tuple[str, ...]] = frozenset(),
    people: frozenset[tuple[str, ...]] = frozenset(),
    surnames: frozenset[str] = frozenset(),
) -> list[tuple[int, str]]:
    """Every hashed name in ``text`` as ``(offset, matched text)``, in order.

    Accents are dropped before hashing, and the text is read both as runs of
    letters and as those runs split at camelCase humps. ``tokens`` match as one
    word, or as a surname of several words opened by a particle. ``phrases``
    and ``people`` match as consecutive words, whatever separates them; a
    two-word name in ``people`` also matches as "Last, First". ``surnames``
    match only as the value of a surname field.
    """
    index = _name_index(tokens, phrases, people, surnames)
    folded = text if text.isascii() else text.translate(_ACCENT_FOLDING)
    words = [
        (m.start(), m.end(), m.group().lower()) for m in _NAME_WORD.finditer(folded)
    ]
    runs = _glued_runs(words, folded)
    spans = _token_spans(words, runs, folded, index.tokens)
    spans |= _phrase_spans(words, folded, index)
    if len(runs) != len(words):
        spans |= _phrase_spans(runs, folded, index)
    for field in _SURNAME_FIELD.finditer(folded):
        surname = _NOT_A_LETTER.sub("", field.group(1).lower())
        if surname and _digest(surname) in index.surnames:
            spans.add(field.span(1))
    return [(begin, text[begin:end]) for begin, end in sorted(spans)]


def staff_name_hits(
    text: str,
    tokens: frozenset[str] | None = None,
    full_names: frozenset[tuple[str, str]] | None = None,
    surnames: frozenset[str] | None = None,
) -> list[tuple[int, str]]:
    """Every staff name in ``text`` as ``(offset, matched text)``.

    The digests default to the module's lists, read at call time.
    """
    return hashed_name_hits(
        text,
        STAFF_TOKEN_DIGESTS if tokens is None else tokens,
        people=STAFF_FULL_NAME_DIGESTS if full_names is None else full_names,
        surnames=STAFF_SURNAME_DIGESTS if surnames is None else surnames,
    )


def client_name_hits(
    text: str,
    tokens: frozenset[str] | None = None,
    names: frozenset[tuple[str, ...]] | None = None,
) -> list[tuple[int, str]]:
    """Every old client in ``text`` as ``(offset, matched text)``.

    The digests default to the module's lists, read at call time.
    """
    return hashed_name_hits(
        text,
        CLIENT_TOKEN_DIGESTS if tokens is None else tokens,
        phrases=CLIENT_NAME_DIGESTS if names is None else names,
    )


def _people_and_client_hits(text: str) -> list[tuple[int, str]]:
    """Staff, outside contacts and clients in one pass over ``text``."""
    return hashed_name_hits(
        text,
        STAFF_TOKEN_DIGESTS | CONTACT_TOKEN_DIGESTS | CLIENT_TOKEN_DIGESTS,
        phrases=CLIENT_NAME_DIGESTS,
        people=STAFF_FULL_NAME_DIGESTS,
        surnames=STAFF_SURNAME_DIGESTS,
    )


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group())


def legacy_terms(text: str) -> list[tuple[int, str]]:
    """Every banned term in ``text`` as ``(offset, matched text)``, in order.

    Lock-file digests and binary base64 payloads are blanked first. A text
    payload is decoded and scanned, and its hits are reported at the offset of
    its ``data:`` URI.
    """
    payload_hits: list[tuple[int, str]] = []

    def blank_payload(match: re.Match[str]) -> str:
        if _is_text_media(match.group("type")):
            decoded = _decoded_text(match.group("payload"))
            payload_hits.extend(
                (match.start(), term) for _, term in legacy_terms(decoded)
            )
        return _blank(match)

    text = _DIGEST.sub(_blank, _BASE64_DATA_URI.sub(blank_payload, text))
    hits = [(match.start(), match.group()) for match in LEGACY_PATTERN.finditer(text)]
    return sorted(hits + payload_hits + _people_and_client_hits(text))


def _read_text(path: Path) -> str | None:
    """The text of a regular file. A link, a folder, a pipe, a binary file and
    a file that is gone or cannot be read (any OSError) have none."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        with path.open("rb") as handle:
            head = handle.read(BINARY_SNIFF_BYTES)
            if b"\0" in head:
                return None
            data = head + handle.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _content_hits(path: Path, rel: str) -> list[tuple[str, int, str]]:
    text = _read_text(path)
    if text is None:
        return []
    hits: list[tuple[str, int, str]] = []
    line_no, consumed = 1, 0
    for offset, term in legacy_terms(text):
        line_no += text.count("\n", consumed, offset)
        consumed = offset
        hits.append((rel, line_no, term))
    return hits


def _name_hits(rel: str) -> list[tuple[str, int, str]]:
    name = rel.rsplit("/", 1)[-1]
    return [(rel, 0, term) for _, term in legacy_terms(name)]


def tracked_files(root: Path) -> list[str]:
    """The files git tracks under ``root``, as POSIX paths relative to it."""
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
    if listing.returncode != 0:
        error = listing.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"cannot list the tracked files under {root}: {error}")
    return sorted(
        rel
        for rel in listing.stdout.decode("utf-8", errors="replace").split("\0")
        if rel
    )


def scan_repo(root: Path) -> list[tuple[str, int, str]]:
    """Every legacy-brand hit in the git-tracked files under ``root``, as
    ``(path, line_no, match)``.

    What git tracks is what CI checks out and what ships, so a tracked file is
    read wherever it sits; untracked and ignored files are local and are not.
    ``path`` is POSIX and relative to ``root``. A hit in a file or folder name
    is reported once, on line 0.
    """
    root = Path(root)
    files = tracked_files(root)
    folders = {
        parent.as_posix()
        for rel in files
        for parent in PurePosixPath(rel).parents
        if parent.name
    }
    hits: list[tuple[str, int, str]] = []
    for folder in folders:
        hits += _name_hits(folder)
    for rel in files:
        hits += _name_hits(rel)
        hits += _content_hits(root / rel, rel)
    return sorted(hits)


def scan_tree(root: Path) -> list[tuple[str, int, str]]:
    """Every legacy-brand hit in the files on disk under ``root``, as
    ``(path, line_no, match)``: for a folder git does not track, such as the
    built frontend.

    ``path`` is POSIX and relative to ``root``. A hit in a file or folder name
    is reported once, on line 0. Skipped trees are not entered.
    """
    root = Path(root)
    hits: list[tuple[str, int, str]] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIPPED_DIRS]
        base = Path(current)
        for name in dirnames:
            hits += _name_hits((base / name).relative_to(root).as_posix())
        for name in filenames:
            path = base / name
            rel = path.relative_to(root).as_posix()
            hits += _name_hits(rel)
            hits += _content_hits(path, rel)
    return sorted(hits)
