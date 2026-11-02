"""The proposal letterhead: the workspace's company details, Cubie's colours.

A proposal is the workspace's own document, so every fact on its letterhead
(name, legal name, address, website) comes from the company profile that its
admin fills in (Admin → Company setup). The colours come from
``rubik_core.brand``; a workspace's own accent colour arrives with the
multi-workspace work.

With no profile saved the letterhead says nothing about the company rather
than something wrong: the masthead band is plain, PREPARED BY lists the AE
alone, and the footer reads "Confidential Proposal". It never prints "None"
or an empty label.
"""

from __future__ import annotations

from dataclasses import dataclass

from rubik_core import brand
from rubik_core.company.profile import CompanyProfile

#: Masthead band, section headings and badges.
HEADING_RGB = brand.rgb(brand.INK_HEX)
#: The accent behind the pricing table's header row.
TINT_RGB = brand.rgb(brand.TINT_HEX)
#: The SAVE badge: a little stronger than the tint so it reads as a badge.
BADGE_RGB = brand.rgb(brand.SOFT_HEX)


@dataclass(frozen=True)
class Letterhead:
    """The company facts a proposal prints. Every field may be empty."""

    name: str = ""
    legal_name: str = ""
    address: str = ""
    website: str = ""

    @property
    def display_name(self) -> str:
        """The name for the masthead and PREPARED BY: legal name first."""
        return self.legal_name or self.name

    @property
    def contact_line(self) -> str:
        """Address and website on one line, whichever are set."""
        address = ", ".join(line for line in self.address.splitlines() if line)
        return "  |  ".join(part for part in (address, self.website) if part)

    @property
    def footer(self) -> str:
        """The line on every page."""
        if self.display_name:
            return f"{self.display_name} | Confidential Proposal"
        return "Confidential Proposal"


def letterhead(profile: CompanyProfile) -> Letterhead:
    return Letterhead(
        name=profile.name,
        legal_name=profile.legal_name,
        address=profile.address,
        website=profile.website,
    )
