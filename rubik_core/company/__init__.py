"""The workspace's own company: its profile and its product/service catalog.

Cubie is industry-neutral. What a workspace sells, to whom, and at what price
is the workspace's data, entered by its admin in Admin → Company setup and
read by every prompt and document that needs it (spec §4.2.5):

* :mod:`rubik_core.company.profile` — name, letterhead facts and a short
  description of the business. Prompts include
  :func:`~rubik_core.company.profile.company_context_block` and fall back to
  "the company" when it is empty.
* :mod:`rubik_core.company.catalog` — the products and services, uploaded as a
  CSV. Empty by default.
* :mod:`rubik_core.company.domain` — the company's Workspace email domain, the
  one resolver every "is this address ours?" check reads.
"""

from __future__ import annotations

from rubik_core.company.domain import workspace_domain
from rubik_core.company.profile import (
    YOUR_COMPANY,
    CompanyProfile,
    company_context_block,
    company_name_for_copy,
    company_name_or_default,
    company_possessive,
    get_company_profile,
    save_company_profile,
)

__all__ = [
    "YOUR_COMPANY",
    "CompanyProfile",
    "company_context_block",
    "company_name_for_copy",
    "company_name_or_default",
    "company_possessive",
    "get_company_profile",
    "save_company_profile",
    "workspace_domain",
]
