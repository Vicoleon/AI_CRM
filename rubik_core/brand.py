"""The product's brand, in one place.

Cubie is a RubikSoft product. LLM prompts, emails, Slack copy and generated
documents read the product name, the vendor, the support address, the palette
and the type pair from this module instead of repeating a literal. The frontend
mirrors the names in ``frontend/src/brand/names.ts`` (re-exported by
``brand.ts``); ``backend/tests/test_brand.py`` pins that the two agree.

Workspaces may set their own logo and accent colour, but never the product
name.
"""

from __future__ import annotations

PRODUCT_NAME = "Cubie"
#: Lower-case form for slugs, keywords and file names.
PRODUCT_SLUG = "cubie"
VENDOR_NAME = "RubikSoft"
VENDOR_URL = "https://www.rubik-soft.com"
#: Where a workspace admin reaches RubikSoft.
SUPPORT_EMAIL = "jose.leon@rubik-soft.com"

#: Violet: primary actions and active states.
BRAND_HEX = "#7C3AED"
#: Darker violet for the hover state of primary actions.
BRAND_HOVER_HEX = "#6D28D9"
#: Ink: the navigation rail and headings.
INK_HEX = "#0E0729"
#: Off-white: tinted surfaces.
TINT_HEX = "#F2F0F7"
#: Soft lavender: secondary surfaces and borders.
SOFT_HEX = "#D5CAFC"
#: Lavender: the secondary accent.
ACCENT_HEX = "#A47BFF"
#: Green, used sparingly and only for positive/success signals.
SUCCESS_HEX = "#03553E"

#: Logo, titles and highlights.
DISPLAY_FONT = "Unbounded"
#: Body text.
BODY_FONT = "Montserrat"


def rgb(hex_colour: str) -> tuple[int, int, int]:
    """``"#7C3AED"`` → ``(124, 58, 237)``, for renderers that take channels."""
    value = hex_colour.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
