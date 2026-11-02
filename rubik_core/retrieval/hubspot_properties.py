"""The company's own HubSpot properties, named by configuration: one module.

HubSpot's built-in properties (``dealname``, ``amount``, ``dealstage``,
``closedate`` …) are the same in every portal, so the code reads them by name.
Any other property is one the company created, so its internal name is
configuration, never code (spec §2 and §4.5.2): an admin names it in a
setting, and with the setting empty the feature that needs it says it is not
set up, or leaves that part out. It never guesses a name.

The deal properties shared by several features live here:

* ``HUBSPOT_DEAL_PRODUCT_PROPERTY``: the deal property that says what a deal
  is for (its products or services). Ask Cubie's turn-times-by-product answer
  groups by it, AI Priorities matches newly added products against it, the
  BigQuery CRM reports filter by it, and Customer X-Ray reads what a customer
  already owns from it.
* ``HUBSPOT_DEAL_TURNAROUND_PROPERTY``: a deal duration property (HubSpot
  stores a duration in milliseconds) that the turn-times answer takes the
  median of.

Customer X-Ray's own roles (renewal date, account status …) are in
``rubik_core/xray/properties.py`` and read the same workspace connector.

A configured name must look like a HubSpot internal name (a letter, then
letters, digits or underscores). Anything else is logged and ignored, so a
typo never reaches an API body and a name is always safe to use as a BigQuery
column.
"""

from __future__ import annotations

import logging
import re

from rubik_core.tenancy.hubspot_config import get_hubspot_config

logger = logging.getLogger(__name__)

_INTERNAL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def property_names(raw: object, *, setting: str = "") -> list[str]:
    """A comma-separated setting as valid internal names, in order, no repeats."""
    names: list[str] = []
    for part in str(raw or "").split(","):
        name = part.strip()
        if not name:
            continue
        if not _INTERNAL_NAME.fullmatch(name):
            logger.warning(
                "ignoring %r in %s: not a HubSpot internal property name",
                name,
                setting or "a property setting",
            )
            continue
        if name not in names:
            names.append(name)
    return names


def deal_product_property() -> str:
    """The deal property that says what a deal is for, or ``""``."""
    return get_hubspot_config().deal_product_property


def deal_turnaround_property() -> str:
    """The deal duration property turn times are read from, or ``""``."""
    return get_hubspot_config().deal_turnaround_property
