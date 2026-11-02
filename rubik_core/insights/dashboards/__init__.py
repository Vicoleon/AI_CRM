"""The Insights dashboard registry (contract §2).

Every dashboard the product plans is listed here, shipped or not, so ``/meta``
can tell the frontend the whole map and the order groups render in. A
dashboard that has not shipped carries ``available=False`` and no module; the
API answers 404 for it and the frontend hides it from navigation.

Adding a dashboard is a module in this package with ``build(ctx)`` (the full
payload) and ``headline(ctx)`` (the few KPIs Overview shows), plus flipping
its row here to available.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType

from rubik_core.brand import PRODUCT_NAME

#: Group key → heading, in render order.
GROUPS: tuple[tuple[str, str], ...] = (
    ("company", "Company"),
    ("work", "Work"),
    ("sales", "Sales"),
    ("platform", "Platform"),
)


@dataclass(frozen=True)
class DashboardDef:
    slug: str
    title: str
    group: str
    description: str
    phase: int
    module: str | None = None

    @property
    def available(self) -> bool:
        return self.module is not None

    @property
    def href(self) -> str:
        return f"/insights/{self.slug}"

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "group": self.group,
            "available": self.available,
            "description": self.description,
        }


DASHBOARDS: tuple[DashboardDef, ...] = (
    DashboardDef(
        "overview",
        "Overview",
        "company",
        f"Is {PRODUCT_NAME} working for the company? The headline from every dashboard "
        "and a side-by-side comparison of departments.",
        1,
        "rubik_core.insights.dashboards.overview",
    ),
    DashboardDef(
        "adoption",
        "Adoption & Engagement",
        "company",
        f"Who uses {PRODUCT_NAME}, how often, which parts, and who has stopped.",
        1,
        "rubik_core.insights.dashboards.adoption",
    ),
    DashboardDef(
        "ask-cubie",
        f"Ask {PRODUCT_NAME}",
        "work",
        f"How much people rely on Ask {PRODUCT_NAME}, how fast and how well it answers.",
        1,
        "rubik_core.insights.dashboards.ask_cubie",
    ),
    DashboardDef(
        "priorities",
        "AI Priorities",
        "work",
        f"What {PRODUCT_NAME} caught for people each morning, and what they did with it.",
        1,
        "rubik_core.insights.dashboards.priorities",
    ),
    DashboardDef(
        "follow-through",
        "Follow-through",
        "work",
        "Whether loose ends and promises are closing.",
        2,
        "rubik_core.insights.dashboards.follow_through",
    ),
    DashboardDef(
        "crm",
        "CRM & HubSpot",
        "sales",
        f"What {PRODUCT_NAME} wrote into HubSpot, and whether people trust it.",
        2,
        "rubik_core.insights.dashboards.crm",
    ),
    DashboardDef(
        "proposals",
        "Proposals & Prep",
        "sales",
        "Whether proposals and meeting preparation are getting produced.",
        2,
        "rubik_core.insights.dashboards.proposals",
    ),
    DashboardDef(
        "ai-spend",
        "AI Spend",
        "platform",
        f"What {PRODUCT_NAME}'s AI costs, and what it costs per person and per result.",
        3,
        "rubik_core.insights.dashboards.ai_spend",
    ),
    DashboardDef(
        "knowledge",
        "Knowledge & Content",
        "platform",
        "Whether the knowledge base is growing and being used.",
        3,
        "rubik_core.insights.dashboards.knowledge",
    ),
    DashboardDef(
        "quality",
        "Quality & Trust",
        "platform",
        f"Where {PRODUCT_NAME}'s AI is weakest, surface by surface.",
        3,
        "rubik_core.insights.dashboards.quality",
    ),
)

_BY_SLUG: dict[str, DashboardDef] = {d.slug: d for d in DASHBOARDS}
assert len(_BY_SLUG) == len(DASHBOARDS), "duplicate Insights dashboard slug"


def get(slug: str) -> DashboardDef | None:
    return _BY_SLUG.get((slug or "").strip().lower())


def module_for(slug: str) -> ModuleType | None:
    d = get(slug)
    if d is None or d.module is None:
        return None
    return importlib.import_module(d.module)


def available() -> list[DashboardDef]:
    return [d for d in DASHBOARDS if d.available]
