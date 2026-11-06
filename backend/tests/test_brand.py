"""The brand lives in one backend module and one frontend module.

``rubik_core.brand`` is the backend's single source for the product name, the
vendor, the support address, the palette and the type pair; prompts, emails and
generated documents read from it. ``frontend/src/brand/names.ts`` mirrors the
names and the support address for the UI, ``frontend/src/brand/brand.ts``
re-exports them and exports the cube mark; ``frontend/src/index.css`` mirrors
the palette and the type pair as CSS variables, and
``frontend/src/brand/colors.ts`` falls back to the same palette when a variable
is unset. The favicon and the cube mark are plain vector files: a ``viewBox``,
paths filled with ``currentColor``, and nothing embedded.

These tests pin the values, keep the two modules in agreement and keep the
icons free of raster content.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from backend.tests._legacy_brand import LEGACY_PATTERN
from rubik_core import brand

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_BRAND = REPO_ROOT / "frontend" / "src" / "brand" / "brand.ts"
FRONTEND_NAMES = REPO_ROOT / "frontend" / "src" / "brand" / "names.ts"
PRIVACY_PAGE = REPO_ROOT / "frontend" / "public" / "chrome-privacy.html"
README = REPO_ROOT / "README.md"
CUBE_MARK = REPO_ROOT / "frontend" / "src" / "brand" / "assets" / "cube.svg"
FAVICON = REPO_ROOT / "frontend" / "public" / "cubie.svg"
FRONTEND_CSS = REPO_ROOT / "frontend" / "src" / "index.css"
FRONTEND_COLORS = REPO_ROOT / "frontend" / "src" / "brand" / "colors.ts"

HEX = re.compile(r"^#[0-9A-F]{6}$")

PINNED_HEX = {
    "BRAND_HEX": "#7C3AED",
    "BRAND_HOVER_HEX": "#6D28D9",
    "INK_HEX": "#0E0729",
    "TINT_HEX": "#F2F0F7",
    "SOFT_HEX": "#D5CAFC",
    "ACCENT_HEX": "#A47BFF",
    "SUCCESS_HEX": "#03553E",
}

# Names the frontend module mirrors from the backend one.
MIRRORED = ("PRODUCT_NAME", "VENDOR_NAME", "VENDOR_URL", "SUPPORT_EMAIL")

# index.css custom property -> the backend colour it carries, as RGB channels.
CSS_VARIABLES = {
    "--brand": "BRAND_HEX",
    "--brand-hover": "BRAND_HOVER_HEX",
    "--brand-tint": "TINT_HEX",
    "--brand-soft": "SOFT_HEX",
    "--accent": "ACCENT_HEX",
    "--rail": "INK_HEX",
    "--rail-active": "BRAND_HEX",
    "--ink": "INK_HEX",
    "--success-brand": "SUCCESS_HEX",
}

# colors.ts fallback key -> the backend colour it carries.
COLOR_FALLBACKS = {
    "brand": "BRAND_HEX",
    "brandHover": "BRAND_HOVER_HEX",
    "tint": "TINT_HEX",
    "accent": "ACCENT_HEX",
    "ink": "INK_HEX",
}


def _channels(name: str) -> str:
    return " ".join(str(channel) for channel in brand.rgb(getattr(brand, name)))


def test_product_and_vendor_names():
    assert brand.PRODUCT_NAME == "Cubie"
    assert brand.PRODUCT_SLUG == "cubie"
    assert brand.VENDOR_NAME == "RubikSoft"
    assert brand.VENDOR_URL == "https://www.rubik-soft.com"
    assert brand.SUPPORT_EMAIL == "jose.leon@rubik-soft.com"


def test_type_pair():
    assert brand.DISPLAY_FONT == "Unbounded"
    assert brand.BODY_FONT == "Montserrat"


def _colours() -> dict[str, str]:
    return {name: value for name, value in vars(brand).items() if name.endswith("_HEX")}


def test_palette_is_pinned():
    colours = _colours()
    assert colours == PINNED_HEX


def test_every_brand_colour_is_an_uppercase_six_digit_hex():
    colours = _colours()
    assert colours, "rubik_core.brand defines no *_HEX colour"
    bad = {name: value for name, value in colours.items() if not HEX.match(value)}
    assert bad == {}


def test_frontend_brand_module_mirrors_the_backend_names():
    source = FRONTEND_NAMES.read_text(encoding="utf-8")
    for name in MIRRORED:
        match = re.search(rf"export const {name} = '([^']*)';", source)
        assert match, f"names.ts does not export {name} as a string literal"
        assert match.group(1) == getattr(brand, name), name


def test_the_names_module_is_import_free_and_brand_ts_re_exports_it():
    """Bare Node (the frontend test runner) loads ``names.ts``; an import there
    (the cube SVG, say) would break every test whose module reads a name."""
    names = FRONTEND_NAMES.read_text(encoding="utf-8")
    assert not re.search(r"^\s*import\b", names, re.MULTILINE)
    match = re.search(
        r"export \{([^}]*)\} from '\./names\.ts';",
        FRONTEND_BRAND.read_text(encoding="utf-8"),
    )
    assert match, "brand.ts does not re-export ./names.ts"
    assert {part.strip() for part in match.group(1).split(",")} == set(MIRRORED)


def test_the_static_pages_give_the_support_address():
    """The privacy page is static HTML and the README is prose, so both write
    the address out by hand; it must be the brand module's."""
    privacy = PRIVACY_PAGE.read_text(encoding="utf-8")
    mailtos = re.findall(r'href="mailto:([^"]+)"', privacy)
    assert mailtos == [brand.SUPPORT_EMAIL]
    assert f">{brand.SUPPORT_EMAIL}</a>" in privacy
    readme = README.read_text(encoding="utf-8")
    assert re.findall(r"\(mailto:([^)]+)\)", readme) == [brand.SUPPORT_EMAIL]


def test_frontend_brand_module_exports_the_cube_mark():
    source = FRONTEND_BRAND.read_text(encoding="utf-8")
    assert re.search(r"import cubeMarkUrl from '\./assets/cube\.svg';", source)
    assert re.search(r"export const cubeMark: string = cubeMarkUrl;", source)


def test_rgb_splits_a_hex_into_channels():
    assert brand.rgb("#7C3AED") == (124, 58, 237)
    assert brand.rgb("0E0729") == (14, 7, 41)


@pytest.mark.parametrize(("variable", "name"), CSS_VARIABLES.items())
def test_css_variables_mirror_the_backend_palette(variable: str, name: str):
    css = FRONTEND_CSS.read_text(encoding="utf-8")
    match = re.search(rf"{re.escape(variable)}:\s*([\d ]+);", css)
    assert match, f"index.css does not define {variable}"
    assert match.group(1).strip() == _channels(name)


def test_css_fonts_mirror_the_backend_type_pair():
    css = FRONTEND_CSS.read_text(encoding="utf-8")
    display = re.search(r"--font-display:\s*([^;]+);", css)
    body = re.search(r"--font-body:\s*([^;]+);", css)
    assert display and brand.DISPLAY_FONT in display.group(1)
    assert body and brand.BODY_FONT in body.group(1)


@pytest.mark.parametrize(("key", "name"), COLOR_FALLBACKS.items())
def test_brand_color_fallbacks_mirror_the_backend_palette(key: str, name: str):
    source = FRONTEND_COLORS.read_text(encoding="utf-8")
    assert re.search(rf"\b{key}: '{_channels(name)}',", source), key


@pytest.mark.parametrize("icon", [FAVICON, CUBE_MARK], ids=["favicon", "cube_mark"])
def test_icon_is_a_plain_vector(icon: Path):
    assert icon.is_file(), f"{icon.relative_to(REPO_ROOT)} is missing"
    text = icon.read_text(encoding="utf-8")
    assert text.startswith("<svg")
    assert "<image" not in text
    assert "data:" not in text
    assert re.search(r'viewBox="[\d.]+ [\d.]+ [\d.]+ [\d.]+"', text)
    assert 'fill="currentColor"' in text
    assert "<path" in text


@pytest.mark.parametrize(
    "path",
    [
        Path(inspect.getsourcefile(brand)),
        FRONTEND_BRAND,
        FRONTEND_NAMES,
        CUBE_MARK,
        FAVICON,
        FRONTEND_CSS,
        FRONTEND_COLORS,
    ],
    ids=[
        "brand.py",
        "brand.ts",
        "names.ts",
        "cube.svg",
        "cubie.svg",
        "index.css",
        "colors.ts",
    ],
)
def test_brand_files_carry_no_legacy_brand(path: Path):
    assert LEGACY_PATTERN.search(path.read_text(encoding="utf-8")) is None


# -- runtime strings take the product name from the brand module --------------

#: The trees whose runtime strings (prompts, errors, emails, Slack copy, labels)
#: must read the product name from ``rubik_core.brand``.
RUNTIME_TREES = ("rubik_core", "backend")
#: Not runtime copy: the brand module itself, the Help mirror (Help prose is
#: copy written for people and may say the name), and the backend's tests.
RUNTIME_EXEMPT = ("rubik_core/brand.py", "rubik_core/help/", "backend/tests/")
# These are stable HTTP protocol header names, not product copy. Rebranding
# their bytes would break existing Scheduler jobs and the Meet add-on client.
PROTOCOL_HEADER_LITERALS = frozenset(
    {
        "X-Cubie-Workspace",
        "X-Cubie-Workspace is required for this target",
        "X-Cubie-Meet-Access-Token",
    }
)


def _documentation_strings(tree: ast.AST) -> set[int]:
    """ids of the string constants that only document code: docstrings and any
    other string standing alone as a statement. Neither reaches a person."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _runtime_name_hits(rel: str, source: str) -> list[str]:
    tree = ast.parse(source, filename=rel)
    documentation = _documentation_strings(tree)
    return [
        f"{rel}:{node.lineno}: {node.value.strip()[:80]!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
        and brand.PRODUCT_NAME in node.value
        and node.value not in PROTOCOL_HEADER_LITERALS
    ]


def test_runtime_strings_take_the_product_name():
    """A runtime string that spells the product name drifts the day the name
    changes. Prompts, errors, emails and labels build it from
    ``PRODUCT_NAME``; a string constant or the literal part of an f-string may
    not carry it. Docstrings and comments are documentation and may."""
    hits: list[str] = []
    for tree_name in RUNTIME_TREES:
        for path in sorted((REPO_ROOT / tree_name).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(RUNTIME_EXEMPT):
                continue
            hits.extend(_runtime_name_hits(rel, path.read_text(encoding="utf-8")))
    assert hits == [], (
        f"{len(hits)} runtime strings spell {brand.PRODUCT_NAME!r}; use "
        "rubik_core.brand.PRODUCT_NAME:\n" + "\n".join(hits[:60])
    )
