"""The curated skill vocabulary Smart Teams staffs a project from (ST-8).

``st_skills_taxonomy`` is a closed list on purpose: "get me someone who knows
data privacy" only works if the same competence is not stored eleven ways.
This module is that list, plus the alias matcher that maps the phrasing people
actually use onto a canonical key, plus an idempotent seeder.

**A neutral starter set.** Cubie ships to companies in any industry, so the
seed is industry-agnostic: about ninety skills across five domains (business,
sales and customer, operations, technology, people and leadership). Each
workspace extends it with its own specialist skills in Smart Teams → Curate
skills (:func:`validate_new_skill`), and :func:`seed_taxonomy` never touches a
row it does not ship, so those additions survive every re-seed.

Two kinds of vocabulary are deliberately NOT used as inputs. Get-it / want-it /
capacity ratings and sales-coaching rubric scores are *performance and
competence judgements*, and Smart Teams is advisory: a skill row says "you may
ask me about this", never "this person is good at their job". Seeding a skill
list from a manager's capacity judgement would make every downstream read a
performance read. ``cluster`` is therefore always a DOMAIN ("Operations"),
never a level ("Advanced").

**Size.** Specific enough that a project lead can ask for "data privacy and
financial reporting" and get an answer, coarse enough that one person's skills
fit on a screen and the alias matcher is not trying to tell sixteen shades of
the same thing apart.

**Retirement is ``is_active = False``, never a delete.** ``st_person_skills``
holds a RESTRICT foreign key here, so dropping a row would either fail or erase
somebody's claimed history. :func:`seed_taxonomy` never deletes and never
re-activates a row an admin retired — see its docstring.

ADVISORY ONLY. Nothing in this module, and nothing that reads it, may frame a
skill as competence, potential, promotion-readiness, or fitness for a role.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

#: The domain groupings. Order is the presentation order for the picker.
CLUSTERS: tuple[str, ...] = (
    "Business",
    "Sales & Customer",
    "Operations",
    "Technology",
    "People & Leadership",
)

#: Same shape as ``interview._SKILL_KEY_PATTERN``, re-stated rather than
#: imported so this module does not depend on the interview catalog (the seeder
#: runs on the worker, where the interview is not in play). A test pins the two
#: patterns together.
_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]*$")

#: An alias shorter than this is not matched against free text at all. Two- and
#: three-letter acronyms ("QA", "SOW", "AWS", and "IDs" — the EOS term "IDS"
#: spelled the way every identifier in the app is) collide with ordinary words,
#: and a false positive here is a suggestion about a real person built on a
#: coincidence. Four is the bound because the four-letter aliases in this
#: vocabulary are distinctive acronyms (GAAP, GDPR, CCPA, OSHA, OKRS) that no
#: ordinary sentence produces by accident. A short alias that is
#: NOT scan-safe still belongs in ``aliases`` — the picker's search box is an
#: explicit lookup rather than a scan, and only :func:`match_phrases` applies
#: this bound. ``test_smart_teams_skills.py`` enumerates every phrase under six
#: characters, so adding a risky short alias fails a test rather than quietly
#: mis-attributing evidence.
MIN_SCAN_ALIAS_CHARS = 4

#: Aliases long enough to clear :data:`MIN_SCAN_ALIAS_CHARS` that are still
#: ordinary English, and so are excluded from :func:`match_phrases` by name.
#:
#: The length bound is a proxy for distinctiveness and "lean" is where the proxy
#: breaks: it is exactly four characters, so the bound waves it through, and it is
#: also a common verb ("I lean toward shipping early", "we leaned on the vendor").
#: A hit there would put Lean process improvement in front of somebody as an
#: AI-inferred skill on the strength of a coincidence, which is the specific
#: failure the bound exists to prevent.
#:
#: Exclusion is from SCANNING only. These stay in ``aliases`` and stay in
#: :func:`alias_index`, so the picker's search box — an explicit lookup, where the
#: person typing "lean" means the methodology — still finds them. Same split the
#: bound already makes for two- and three-letter acronyms.
SCAN_UNSAFE_ALIASES: frozenset[str] = frozenset({"lean"})

#: Wording that encodes a LEVEL rather than a domain, refused in a skill's name,
#: cluster or aliases — for both the curated list (at import) and anything an
#: admin adds (:func:`validate_new_skill`).
#:
#: Not a style rule. ``st_person_skills.level`` is a structured column whose
#: values are ``familiar | working | strong | expert``, so a skill *named* for a
#: level double-encodes it and produces incoherent rows: "familiar with Expert
#: Excel" says two contradictory things, and a staffing read has no way to know
#: which one the person meant. The module docstring already states the rule for
#: clusters ("always a DOMAIN, never a level 'Advanced'") — this enforces it, and
#: enforces it on names too, where it matters more because names are what a
#: colleague reads.
#:
#: This is a SEPARATE screen from ``strengths._ADVANCEMENT_PROSE``, which catches
#: promotion judgements in guide prose ("ready for more", "has potential"). That
#: regex was written for sentences and does not cover the seniority ladder; both
#: run here because they catch different things.
#:
#: Two near-misses are deliberately NOT screened, because ordinary business
#: vocabulary needs them:
#:
#: * **"level"** — "EOS / Level 10 facilitation" is a proper noun, not a rank.
#: * **"master"** — "master data management" and "master services agreement"
#:   are ordinary business terms, and a workspace will eventually want one.
#:   The belts below are screened by name instead.
_LEVEL_WORDING = re.compile(
    r"\b(?:"
    r"senior|junior|beginner|novice|intermediate|advanced|expert|proficient"
    r"|entry[- ]level|mid[- ]level"
    r"|(?:black|green|yellow|white|brown) belt"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SkillSpec:
    """One curated skill: what it is called, where it sits, how to spot it."""

    skill_key: str
    name: str
    cluster: str
    #: Lower-case phrases that mean this skill. Used by the picker's search and
    #: by the inference sweep's deterministic pre-filter. The canonical
    #: ``name`` and the de-slugged key are added implicitly by
    #: :func:`alias_index`, so they never need repeating here.
    aliases: tuple[str, ...] = field(default_factory=tuple)


def _s(skill_key: str, name: str, cluster: str, *aliases: str) -> tuple[str, SkillSpec]:
    return skill_key, SkillSpec(
        skill_key=skill_key, name=name, cluster=cluster, aliases=aliases
    )


_BU = "Business"
_SC = "Sales & Customer"
_OP = "Operations"
_TE = "Technology"
_PL = "People & Leadership"

#: The seed vocabulary. Keyed by ``skill_key`` so a duplicate is a dict literal
#: collision the import-time guard below can catch.
SKILLS: Mapping[str, SkillSpec] = dict(
    (
        # ── Business ─────────────────────────────────────────────────────────
        _s(
            "strategic_planning",
            "Strategic planning",
            _BU,
            "strategic plan",
            "strategy offsite",
            "annual plan",
            "okrs",
        ),
        _s(
            "financial_reporting",
            "Financial reporting (GAAP)",
            _BU,
            "gaap",
            "financial statements",
            "month-end close",
        ),
        _s(
            "budgeting_forecasting",
            "Budgeting and forecasting",
            _BU,
            "annual budget",
            "budget forecast",
            "cash flow forecast",
        ),
        _s(
            "financial_analysis",
            "Financial analysis and modeling",
            _BU,
            "financial model",
            "unit economics",
            "variance analysis",
        ),
        _s(
            "accounts_payable_receivable",
            "Accounts payable and receivable",
            _BU,
            "accounts payable",
            "accounts receivable",
            "invoice processing",
        ),
        _s(
            "regulatory_compliance",
            "Regulatory compliance",
            _BU,
            "regulatory compliance",
            "compliance review",
            "regulatory filing",
        ),
        _s(
            "data_privacy",
            "Data privacy (GDPR / CCPA)",
            _BU,
            "data privacy",
            "gdpr",
            "ccpa",
            "privacy impact assessment",
            "data subject request",
        ),
        _s(
            "risk_management",
            "Enterprise risk management",
            _BU,
            "risk assessment",
            "risk register",
            "risk management",
        ),
        _s(
            "internal_audit",
            "Internal audit and control testing",
            _BU,
            "internal audit",
            "control testing",
            "audit workpapers",
        ),
        _s(
            "audit_readiness",
            "Audit and exam readiness",
            _BU,
            "audit readiness",
            "external audit",
            "examiner request",
            "audit evidence",
        ),
        _s(
            "contract_management",
            "Contract management",
            _BU,
            "contract management",
            "contract administration",
            "contract renewal",
        ),
        _s(
            "vendor_risk",
            "Vendor and third-party risk management",
            _BU,
            "vendor management",
            "third party risk",
            "vendor due diligence",
        ),
        _s(
            "policy_authoring",
            "Policy and procedure authoring",
            _BU,
            "policy writing",
            "policy manual",
            "written procedure",
        ),
        _s(
            "market_research",
            "Market and competitive research",
            _BU,
            "market research",
            "competitive analysis",
            "competitor research",
        ),
        _s(
            "pricing_strategy",
            "Pricing strategy",
            _BU,
            "pricing strategy",
            "price increase",
            "pricing model",
        ),
        _s(
            "business_development",
            "Business development and partnerships",
            _BU,
            "business development",
            "strategic partnership",
            "partnership deal",
        ),
        _s(
            "board_reporting",
            "Board and investor reporting",
            _BU,
            "board deck",
            "investor update",
            "board pack",
        ),
        # ── Sales & Customer ─────────────────────────────────────────────────
        _s(
            "pipeline_management",
            "Pipeline and CRM hygiene",
            _SC,
            "hubspot",
            "salesforce",
            "sales pipeline",
            "deal stage",
            "pipeline hygiene",
        ),
        _s(
            "prospecting",
            "Prospecting and outbound",
            _SC,
            "cold outreach",
            "outbound prospecting",
            "cold call",
            "lead list",
        ),
        _s(
            "discovery_calls",
            "Discovery and needs analysis calls",
            _SC,
            "discovery call",
            "needs analysis",
            "qualification call",
        ),
        _s(
            "sales_demos",
            "Product demos",
            _SC,
            "product demo",
            "sales demo",
            "demo script",
        ),
        _s(
            "pricing_proposals",
            "Pricing and quoting",
            _SC,
            "rate sheet",
            "quote build",
            "pricing proposal",
        ),
        _s(
            "proposal_response",
            "RFP and proposal response",
            _SC,
            "proposal response",
            "request for proposal",
            "bid response",
        ),
        _s(
            "contract_negotiation",
            "Contract negotiation and renewals",
            _SC,
            "contract negotiation",
            "renewal negotiation",
            "redlines",
        ),
        _s(
            "account_management",
            "Account management",
            _SC,
            "account management",
            "account review",
            "quarterly business review",
        ),
        _s(
            "customer_success",
            "Customer success and retention",
            _SC,
            "customer success",
            "churn risk",
            "renewal risk",
            "customer health",
        ),
        _s(
            "client_onboarding",
            "Client onboarding and implementation",
            _SC,
            "client onboarding",
            "client implementation",
            "kickoff call",
        ),
        _s(
            "customer_support",
            "Customer support",
            _SC,
            "customer support",
            "support ticket",
            "support queue",
        ),
        _s(
            "escalation_management",
            "Customer escalation management",
            _SC,
            "client escalation",
            "escalated issue",
            "escalation path",
        ),
        _s(
            "complaint_handling",
            "Complaint handling",
            _SC,
            "customer complaint",
            "complaint response",
        ),
        _s(
            "client_reporting",
            "Client reporting and report review",
            _SC,
            "client report",
            "monthly report",
            "report review",
            "client deliverable",
        ),
        _s(
            "engagement_scoping",
            "Engagement scoping and statements of work",
            _SC,
            "scope of work",
            "statement of work",
            "engagement scope",
        ),
        _s(
            "sales_enablement",
            "Sales enablement",
            _SC,
            "sales enablement",
            "battle card",
            "sales playbook",
        ),
        _s(
            "content_marketing",
            "Content marketing and thought leadership",
            _SC,
            "content marketing",
            "thought leadership",
            "blog post",
            "newsletter",
        ),
        _s(
            "digital_marketing",
            "Digital marketing and paid ads",
            _SC,
            "paid ads",
            "google ads",
            "linkedin ads",
            "paid search",
        ),
        _s(
            "search_optimization",
            "Search engine optimization",
            _SC,
            "search engine optimization",
            "keyword research",
            "organic search",
        ),
        _s(
            "email_marketing",
            "Email marketing",
            _SC,
            "email campaign",
            "email marketing",
            "drip campaign",
        ),
        _s(
            "social_media",
            "Social media",
            _SC,
            "social media",
            "linkedin post",
            "social post",
        ),
        _s(
            "brand_design",
            "Brand and visual design",
            _SC,
            "brand guidelines",
            "visual identity",
            "logo design",
        ),
        _s(
            "event_marketing",
            "Events and trade shows",
            _SC,
            "trade show",
            "conference booth",
            "event planning",
        ),
        _s(
            "partner_channel",
            "Partner and referral channel development",
            _SC,
            "referral partner",
            "channel partner",
            "partner program",
        ),
        # ── Operations ───────────────────────────────────────────────────────
        _s(
            "project_management",
            "Project management",
            _OP,
            "project plan",
            "project management",
            "milestone plan",
            "gantt chart",
        ),
        _s(
            "process_documentation",
            "Process mapping and documentation",
            _OP,
            "process map",
            "process documentation",
            "runbook",
            "standard operating procedure",
        ),
        _s(
            "eos_facilitation",
            "EOS / Level 10 facilitation",
            _OP,
            "level 10",
            "l10 meeting",
            "scorecard review",
            "accountability chart",
        ),
        _s(
            "capacity_planning",
            "Capacity and staffing planning",
            _OP,
            "capacity planning",
            "staffing plan",
            "workload planning",
        ),
        _s(
            "procurement",
            "Procurement and purchasing",
            _OP,
            "procurement",
            "purchase order",
            "purchasing process",
        ),
        _s(
            "change_management",
            "Change management and rollout",
            _OP,
            "change management",
            "rollout plan",
            "adoption plan",
        ),
        _s(
            "lean_process",
            "Lean process improvement",
            _OP,
            "lean",
            "lean process",
            "lean manufacturing",
            "value stream",
            "value stream mapping",
            "waste reduction",
            "bottleneck analysis",
        ),
        _s(
            "six_sigma",
            "Six Sigma process improvement",
            _OP,
            "six sigma",
            "dmaic",
            "statistical process control",
            "control chart",
            "process capability",
            "root cause analysis",
        ),
        _s(
            "kaizen",
            "Kaizen and continuous improvement",
            _OP,
            "kaizen",
            "continuous improvement",
            "kaizen event",
            "gemba",
            "gemba walk",
            "pdca",
            "plan do check act",
        ),
        _s(
            "quality_assurance",
            "Quality assurance and review",
            _OP,
            "quality assurance",
            "quality review",
            "qa checklist",
            "quality control",
        ),
        _s(
            "supply_chain",
            "Supply chain and logistics",
            _OP,
            "supply chain",
            "logistics provider",
            "inventory management",
            "freight",
        ),
        _s(
            "order_fulfillment",
            "Order fulfillment",
            _OP,
            "order fulfillment",
            "order processing",
            "fulfillment center",
        ),
        _s(
            "scheduling_dispatch",
            "Scheduling and dispatch",
            _OP,
            "field dispatch",
            "crew scheduling",
            "shift schedule",
        ),
        _s(
            "facilities_management",
            "Facilities and office management",
            _OP,
            "facilities management",
            "office move",
            "building maintenance",
        ),
        _s(
            "health_safety",
            "Workplace health and safety",
            _OP,
            "workplace safety",
            "osha",
            "incident report",
        ),
        _s(
            "business_continuity",
            "Business continuity planning",
            _OP,
            "business continuity",
            "disaster recovery",
            "continuity plan",
        ),
        # ── Technology ───────────────────────────────────────────────────────
        _s(
            "helpdesk_support",
            "Help desk triage and end-user support",
            _TE,
            "help desk",
            "helpdesk",
            "ticket triage",
            "end user support",
        ),
        _s(
            "identity_access",
            "Identity and access administration",
            _TE,
            "google workspace",
            "access request",
            "account provisioning",
            "offboarding request",
            "single sign-on",
        ),
        _s(
            "endpoint_management",
            "Endpoint and device management",
            _TE,
            "device management",
            "laptop setup",
            "endpoint",
            "mobile device management",
        ),
        _s(
            "network_administration",
            "Network and infrastructure administration",
            _TE,
            "firewall",
            "network configuration",
            "vpn access",
            "dns record",
        ),
        _s(
            "saas_administration",
            "SaaS application administration",
            _TE,
            "application admin",
            "license management",
            "admin console",
            "seat provisioning",
        ),
        _s(
            "cloud_administration",
            "Cloud administration",
            _TE,
            "google cloud",
            "cloud run",
            "cloud sql",
            "service account",
            "amazon web services",
        ),
        _s(
            "integration_engineering",
            "System integration and API work",
            _TE,
            "api integration",
            "webhook",
            "middleware",
            "system integration",
        ),
        _s(
            "software_development",
            "Software development",
            _TE,
            "pull request",
            "code review",
            "typescript",
            "python script",
        ),
        _s(
            "web_development",
            "Web development",
            _TE,
            "web development",
            "website update",
            "landing page",
            "wordpress",
        ),
        _s(
            "software_testing",
            "Software testing",
            _TE,
            "test plan",
            "regression testing",
            "test automation",
            "bug report",
        ),
        _s(
            "product_management",
            "Product management",
            _TE,
            "product roadmap",
            "product requirements",
            "feature spec",
            "user story",
        ),
        _s(
            "ux_design",
            "UX and interface design",
            _TE,
            "user experience",
            "wireframe",
            "figma",
            "usability test",
        ),
        _s(
            "information_security",
            "Information security (SOC 2 / ISO 27001)",
            _TE,
            "information security",
            "soc 2",
            "iso 27001",
            "security review",
        ),
        _s(
            "sql_analysis",
            "SQL and data analysis",
            _TE,
            "sql query",
            "bigquery",
            "data analysis",
            "join the tables",
        ),
        _s(
            "dashboard_reporting",
            "Dashboard and BI reporting",
            _TE,
            "dashboard",
            "looker studio",
            "power bi",
            "kpi report",
        ),
        _s(
            "spreadsheet_modeling",
            "Spreadsheet modeling",
            _TE,
            "pivot table",
            "spreadsheet model",
            "google sheets",
            "excel workbook",
        ),
        _s(
            "workflow_automation",
            "Workflow automation",
            _TE,
            "workflow automation",
            "automated workflow",
            "scripted automation",
        ),
        _s(
            "data_engineering",
            "Data pipelines and warehousing",
            _TE,
            "data pipeline",
            "data warehouse",
            "etl job",
        ),
        _s(
            "data_quality",
            "Data quality and reconciliation",
            _TE,
            "data quality",
            "reconciliation",
            "data cleanup",
            "deduplicate",
        ),
        _s(
            "ai_tooling",
            "AI tooling and prompt design",
            _TE,
            "prompt engineering",
            "generative ai",
            "ai tooling",
            "large language model",
        ),
        # ── People & Leadership ──────────────────────────────────────────────
        _s(
            "team_leadership",
            "Team leadership and delegation",
            _PL,
            "team leadership",
            "delegation",
            "running a team",
        ),
        _s(
            "coaching",
            "Coaching conversations",
            _PL,
            "coaching conversation",
            "coaching plan",
            "coaching session",
        ),
        _s(
            "mentoring",
            "Mentoring and peer support",
            _PL,
            "mentoring",
            "peer support",
            "shadowing",
        ),
        _s(
            "recruiting",
            "Recruiting and interviewing",
            _PL,
            "recruiting",
            "job description",
            "candidate interview",
            "hiring pipeline",
        ),
        _s(
            "new_hire_onboarding",
            "New-hire onboarding",
            _PL,
            "new hire",
            "new-hire onboarding",
            "employee orientation",
        ),
        _s(
            "hr_administration",
            "HR administration and benefits",
            _PL,
            "benefits enrollment",
            "payroll run",
            "employee handbook",
        ),
        _s(
            "training_delivery",
            "Training design and delivery",
            _PL,
            "training session",
            "training deck",
            "webinar",
            "lunch and learn",
        ),
        _s(
            "knowledge_management",
            "Knowledge base curation",
            _PL,
            "knowledge base",
            "documentation library",
            "procedure library",
        ),
        _s(
            "meeting_facilitation",
            "Meeting facilitation",
            _PL,
            "facilitate the meeting",
            "meeting facilitation",
            "workshop facilitation",
        ),
        _s(
            "presentation_delivery",
            "Presenting to executives and clients",
            _PL,
            "executive briefing",
            "presentation deck",
            "board presentation",
        ),
        _s(
            "technical_writing",
            "Technical and procedural writing",
            _PL,
            "technical writing",
            "write the documentation",
            "drafting documentation",
        ),
        _s(
            "internal_communications",
            "Internal communications",
            _PL,
            "internal communications",
            "staff announcement",
            "company announcement",
        ),
        _s(
            "conflict_resolution",
            "Conflict resolution",
            _PL,
            "conflict resolution",
            "mediation",
            "difficult conversation",
        ),
        _s(
            "employee_engagement",
            "Culture and employee engagement",
            _PL,
            "employee engagement",
            "team culture",
            "engagement survey",
        ),
    )
)


# ── Import-time guards ────────────────────────────────────────────────────────
# Assertions rather than tests, for the same reason ``interview.py`` states: a
# broken vocabulary makes every downstream read wrong, and an alias claimed by
# two skills would silently attribute one person's evidence to the other one.
# The same properties are covered in backend/tests/test_smart_teams_skills.py,
# which explains why each matters.


def _deslug(skill_key: str) -> str:
    return skill_key.replace("_", " ")


def _check_taxonomy() -> None:
    from rubik_core.smart_teams.instruments.strengths import _ADVANCEMENT_PROSE

    seen_phrases: dict[str, str] = {}
    for skill_key, spec in SKILLS.items():
        if spec.skill_key != skill_key:
            raise RuntimeError(
                f"skill taxonomy: key {skill_key!r} does not match "
                f"spec.skill_key {spec.skill_key!r}"
            )
        if not _KEY_PATTERN.match(skill_key):
            raise RuntimeError(
                f"skill taxonomy: {skill_key!r} is not a slug (lower case, "
                "digits, underscores); the interview validates declared keys "
                "against this shape and would refuse it"
            )
        if spec.cluster not in CLUSTERS:
            raise RuntimeError(
                f"skill taxonomy: {skill_key} sits in unknown cluster "
                f"{spec.cluster!r}; add it to CLUSTERS in the same change"
            )
        # A cluster is a DOMAIN, and a skill is something you can be asked
        # about. Neither may read as a judgement about advancement — the same
        # screen ``strengths._check_taxonomy`` applies to guide prose, applied
        # here because these strings render into the picker and into staffing
        # output that colleagues read.
        for field_name, prose in (
            ("name", spec.name),
            ("cluster", spec.cluster),
            *(("alias", alias) for alias in spec.aliases),
        ):
            hit = _ADVANCEMENT_PROSE.search(prose)
            if hit:
                raise RuntimeError(
                    f"skill taxonomy: {skill_key}.{field_name} contains "
                    f"{hit.group(0)!r}. Skills are advisory — what somebody can "
                    "be asked about — and may not read as a judgement about "
                    "readiness, promotion, or performance."
                )
            level_hit = _LEVEL_WORDING.search(prose)
            if level_hit:
                raise RuntimeError(
                    f"skill taxonomy: {skill_key}.{field_name} contains "
                    f"{level_hit.group(0)!r}, which is a LEVEL. This module "
                    "ships domains; how well somebody knows one is "
                    "st_person_skills.level. See _LEVEL_WORDING."
                )
        for alias in spec.aliases:
            if alias != alias.strip().lower():
                raise RuntimeError(
                    f"skill taxonomy: {skill_key} alias {alias!r} is not "
                    "already folded; aliases are matched case-insensitively "
                    "and a stored alias with capitals hides that"
                )
        for phrase in _phrases_for(spec):
            owner = seen_phrases.get(phrase)
            if owner is not None and owner != skill_key:
                raise RuntimeError(
                    f"skill taxonomy: {phrase!r} is claimed by both {owner} and "
                    f"{skill_key}. An ambiguous alias attributes one person's "
                    "evidence to the wrong skill; make it specific to one."
                )
            seen_phrases[phrase] = skill_key

    names = [spec.name for spec in SKILLS.values()]
    if len(set(names)) != len(names):
        raise RuntimeError("skill taxonomy: duplicate display name")


def _phrases_for(spec: SkillSpec) -> tuple[str, ...]:
    """Every phrase that resolves to ``spec`` — aliases plus the implicit two.

    The display name and the de-slugged key are implicit aliases so a curator
    never has to repeat them, and so ``resolve("Data privacy")`` works before
    anybody has thought about aliases at all.
    """
    return tuple(
        dict.fromkeys(
            [
                *(alias.strip().lower() for alias in spec.aliases if alias.strip()),
                spec.name.strip().lower(),
                _deslug(spec.skill_key),
            ]
        )
    )


_check_taxonomy()


# ── Lookup and matching ───────────────────────────────────────────────────────

_ALIAS_INDEX: dict[str, str] | None = None
_SCAN_RE: re.Pattern[str] | None = None
_DEFAULT_VOCABULARY: Vocabulary | None = None


def alias_index() -> Mapping[str, str]:
    """Every phrase → ``skill_key``. Built once, guaranteed unambiguous."""
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        index: dict[str, str] = {}
        for spec in SKILLS.values():
            for phrase in _phrases_for(spec):
                index[phrase] = spec.skill_key
        _ALIAS_INDEX = index
    return _ALIAS_INDEX


def resolve(phrase: object) -> str | None:
    """The ``skill_key`` an exact phrase names, or ``None``.

    ``None`` means "not in the vocabulary", and every caller DROPS rather than
    coerces. Coercing a near-miss onto a neighbouring skill is how a
    person ends up with a suggestion about work they have never touched.
    """
    if not isinstance(phrase, str):
        return None
    return alias_index().get(phrase.strip().lower())


def _build_scan_pattern(index: Mapping[str, str]) -> re.Pattern[str]:
    """One alternation over every scannable phrase in ``index``, longest first.

    Longest-first matters: ``"value stream mapping"`` and ``"value stream"`` overlap, and
    Python's ``|`` is first-match-wins, so a shorter phrase listed earlier would
    swallow the longer one and attribute the evidence to the wrong skill.
    """
    phrases = sorted(
        (
            p
            for p in index
            if len(p) >= MIN_SCAN_ALIAS_CHARS and p not in SCAN_UNSAFE_ALIASES
        ),
        key=lambda p: (-len(p), p),
    )
    body = "|".join(re.escape(p) for p in phrases)
    # \b on both ends so "pricing analysis" does not match inside a longer
    # token, and re.I because transcripts and document titles are prose.
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)


def _scan_pattern() -> re.Pattern[str]:
    """The curated-only scan pattern, built once."""
    global _SCAN_RE
    if _SCAN_RE is None:
        _SCAN_RE = _build_scan_pattern(alias_index())
    return _SCAN_RE


@dataclass(frozen=True)
class PhraseHit:
    """One place a skill's phrasing appeared in a piece of source text."""

    skill_key: str
    phrase: str
    start: int
    end: int


def match_phrases(text: object, *, limit_per_skill: int = 3) -> list[PhraseHit]:
    """Deterministic pre-filter: which curated skills does this text mention?

    This is what keeps the inference sweep grounded and cheap. Nothing reaches
    the model that a curated phrase did not already point at, so the model can
    only ever judge candidates from the closed list — it is never asked to
    invent a skill name, and a skill it hallucinates resolves to nothing and is
    dropped.

    A hit is NOT evidence that the person has the skill: the phrase may have
    been said by somebody else in the room, or said as a question. That
    judgement is the one thing the LLM pass is for.
    """
    return _match_with(alias_index(), _scan_pattern(), text, limit_per_skill)


def _match_with(
    index: Mapping[str, str],
    pattern: re.Pattern[str],
    text: object,
    limit_per_skill: int,
) -> list[PhraseHit]:
    """The scan itself, shared by :func:`match_phrases` and :class:`Vocabulary`.

    One body on purpose: two copies would eventually disagree about the per-skill
    cap or the lower-casing, and the disagreement would show up as a person's
    inferred skills depending on whether an admin had added anything.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    counts: dict[str, int] = {}
    hits: list[PhraseHit] = []
    for match in pattern.finditer(text):
        phrase = match.group(0).lower()
        skill_key = index.get(phrase)
        if skill_key is None:  # pragma: no cover — the pattern is built from it
            continue
        if counts.get(skill_key, 0) >= max(1, limit_per_skill):
            continue
        counts[skill_key] = counts.get(skill_key, 0) + 1
        hits.append(
            PhraseHit(
                skill_key=skill_key,
                phrase=phrase,
                start=match.start(),
                end=match.end(),
            )
        )
    return hits


# ── Extending the vocabulary with admin-added skills ─────────────────────────
#
# The curated list above is no longer the only source of truth. An admin can add
# a skill through the admin surface, and that row lives only in
# ``st_skills_taxonomy`` — :func:`seed_taxonomy` deliberately never touches a row
# this module does not ship, which is what makes admin rows durable.
#
# Declaration validation already reads that table
# (``smart_teams.py:_known_skill_keys``), so an admin skill was declarable the
# moment it was created. But :func:`match_phrases` and :func:`resolve` read the
# module constant, so an admin skill could never be *inferred* — the AI half of
# the feature silently excluded exactly the skills somebody had gone out of their
# way to curate, and nothing anywhere said so. :class:`Vocabulary` is the fix:
# the same matcher over a set of specs chosen by the caller.


@dataclass(frozen=True)
class Vocabulary:
    """An alias index and scan pattern over one specific set of specs.

    Built per sweep rather than cached globally, because the DB rows it folds in
    can change between sweeps and a stale global would be invisible.
    """

    index: Mapping[str, str]
    pattern: re.Pattern[str]
    #: ``skill_key`` → spec, for callers that need the cluster or display name.
    specs: Mapping[str, SkillSpec]

    def resolve(self, phrase: object) -> str | None:
        """Same contract as the module-level :func:`resolve`, over this set."""
        if not isinstance(phrase, str):
            return None
        return self.index.get(phrase.strip().lower())

    def match_phrases(
        self, text: object, *, limit_per_skill: int = 3
    ) -> list[PhraseHit]:
        """Same contract as the module-level :func:`match_phrases`."""
        return _match_with(self.index, self.pattern, text, limit_per_skill)


def build_vocabulary(specs: Iterable[SkillSpec]) -> Vocabulary:
    """A :class:`Vocabulary` over exactly ``specs``.

    A phrase claimed by two specs is a curation error and raises, the same rule
    :func:`_check_taxonomy` applies at import — an ambiguous phrase would resolve
    to whichever spec was iterated last, which is not a decision anyone made.
    """
    index: dict[str, str] = {}
    by_key: dict[str, SkillSpec] = {}
    for spec in specs:
        by_key[spec.skill_key] = spec
        for phrase in _phrases_for(spec):
            owner = index.get(phrase)
            if owner is not None and owner != spec.skill_key:
                raise ValueError(
                    f"skill vocabulary: {phrase!r} is claimed by both "
                    f"{owner!r} and {spec.skill_key!r}; a phrase must name one "
                    "skill or the match is arbitrary"
                )
            index[phrase] = spec.skill_key
    return Vocabulary(index=index, pattern=_build_scan_pattern(index), specs=by_key)


def spec_from_row(row: object) -> SkillSpec | None:
    """A :class:`SkillSpec` from an ``st_skills_taxonomy`` row, or ``None``.

    ``None`` for anything malformed — an unknown cluster, a non-slug key — rather
    than a raise: one bad admin row must not take down a sweep that would
    otherwise process every other skill correctly. The caller logs the drop.
    """
    skill_key = getattr(row, "skill_key", None)
    name = getattr(row, "name", None)
    cluster = getattr(row, "cluster", None)
    if not isinstance(skill_key, str) or not _KEY_PATTERN.match(skill_key):
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    if cluster not in CLUSTERS:
        return None
    raw = getattr(row, "aliases", None) or []
    aliases = tuple(a.strip().lower() for a in raw if isinstance(a, str) and a.strip())
    return SkillSpec(
        skill_key=skill_key, name=name.strip(), cluster=cluster, aliases=aliases
    )


def vocabulary_with_db_skills(db) -> Vocabulary:
    """The curated list plus every ACTIVE admin-added row in the table.

    Curated specs win on conflict: this module is the source of truth for the
    wording of a skill it ships (the same rule :func:`seed_taxonomy` enforces on
    the way in), so a stale table row cannot change how a curated skill matches.
    Only rows whose key this module does not ship are folded in.

    Falls back to the curated list on any DB error. A sweep that runs with a
    slightly smaller vocabulary is a degraded sweep; one that dies takes the
    whole nightly inference run with it.
    """
    from rubik_core.database.smart_teams import SmartTeamsSkill

    extras: list[SkillSpec] = []
    try:
        rows = (
            db.query(SmartTeamsSkill).filter(SmartTeamsSkill.is_active.is_(True)).all()
        )
    except Exception:  # pragma: no cover - defensive, see docstring
        logger.exception(
            "[smart-teams] could not read st_skills_taxonomy; inferring against "
            "the curated list only, so admin-added skills will be missed"
        )
        return default_vocabulary()

    dropped: list[str] = []
    for row in rows:
        key = getattr(row, "skill_key", None)
        if key in SKILLS:
            continue  # curated wins
        spec = spec_from_row(row)
        if spec is None:
            dropped.append(str(key))
            continue
        extras.append(spec)
    if dropped:
        logger.warning(
            "[smart-teams] %d admin skill row(s) skipped as unusable: %s",
            len(dropped),
            ", ".join(sorted(dropped)),
        )
    if not extras:
        return default_vocabulary()
    try:
        return build_vocabulary([*SKILLS.values(), *extras])
    except ValueError:
        logger.exception(
            "[smart-teams] an admin skill's alias collides with the curated "
            "vocabulary; inferring against the curated list only"
        )
        return default_vocabulary()


def phrases_for(spec: SkillSpec) -> tuple[str, ...]:
    """Public :func:`_phrases_for`: every phrase that resolves to ``spec``.

    The admin surface stores this rather than the raw alias list, so a row an
    admin created carries the same implicit phrases (display name, de-slugged
    key) that :func:`seed_taxonomy` writes for a curated one. Without it, an
    admin skill would be findable by its aliases but not by its own name.
    """
    return _phrases_for(spec)


def advisory_violation(prose: str) -> str | None:
    """The advancement word in ``prose``, or ``None``.

    Exposed so the admin surface applies the SAME screen the import-time check
    applies to :data:`SKILLS`, instead of a second copy that would drift. A
    skill says "you may ask me about this" — never that somebody is ready,
    senior, or good at it.
    """
    from rubik_core.smart_teams.instruments.strengths import _ADVANCEMENT_PROSE

    hit = _ADVANCEMENT_PROSE.search(prose or "")
    return hit.group(0) if hit else None


def validate_new_skill(
    vocab: Vocabulary,
    *,
    skill_key: str,
    name: str,
    cluster: str,
    aliases: Sequence[str],
) -> tuple[SkillSpec | None, list[str]]:
    """Check an admin-proposed skill against every rule :data:`SKILLS` obeys.

    Returns the spec and an EMPTY problem list, or ``None`` and every problem
    found — all of them, not the first, because an admin filling in a form
    should not have to discover four mistakes in four round trips.

    The rules are the import-time guard's rules, applied to a row that never
    passes through the import-time guard. Notably:

    * a key this module already ships is REFUSED rather than merged. Curated
      wording wins everywhere else (:func:`seed_taxonomy`,
      :func:`vocabulary_with_db_skills`), so accepting one here would create a
      row that silently never takes effect.
    * an alias already claimed by another skill is refused, because a phrase
      resolves to exactly one key and the loser would be decided by iteration
      order.
    * the advisory screen runs on the name, the cluster and every alias.

    Deliberately NOT checked: whether an alias is long enough to be scanned.
    Short aliases are legitimate — they are searchable in the picker and simply
    excluded from prose scanning by :data:`MIN_SCAN_ALIAS_CHARS`, which is a
    property of the matcher rather than a reason to reject the curation.
    """
    problems: list[str] = []

    key = (skill_key or "").strip().lower()
    if not key:
        problems.append("A skill needs a key.")
    elif not _KEY_PATTERN.match(key):
        problems.append(
            f"{key!r} is not a slug — use lower case letters, digits and "
            "underscores, starting with a letter or digit."
        )
    elif key in SKILLS:
        problems.append(
            f"{key!r} is a curated skill that ships in the app, so it cannot be "
            "redefined here. Change it in skill_taxonomy.py and release it."
        )

    clean_name = (name or "").strip()
    if not clean_name:
        problems.append("A skill needs a display name.")

    if cluster not in CLUSTERS:
        problems.append(
            f"{cluster!r} is not one of the clusters: {', '.join(CLUSTERS)}."
        )

    folded: list[str] = []
    for alias in aliases or ():
        if not isinstance(alias, str) or not alias.strip():
            continue
        phrase = alias.strip().lower()
        if phrase in folded:
            continue
        owner = vocab.index.get(phrase)
        if owner is not None and owner != key:
            problems.append(
                f"{phrase!r} already means {owner!r}. A phrase can only name one "
                "skill, so pick different wording for one of them."
            )
        folded.append(phrase)

    for label, prose in (
        ("name", clean_name),
        ("cluster", cluster or ""),
        *(("alias", phrase) for phrase in folded),
    ):
        hit = advisory_violation(prose)
        if hit:
            problems.append(
                f"The {label} contains {hit!r}. Skills are advisory — what "
                "somebody can be asked about — and may not read as a judgement "
                "about readiness, seniority or performance."
            )
        level_hit = _LEVEL_WORDING.search(prose)
        if level_hit:
            problems.append(
                f"The {label} contains {level_hit.group(0)!r}, which is a level "
                "rather than a subject. Name the skill itself — how well "
                "somebody knows it is a separate field they pick when they "
                "declare it."
            )

    if problems:
        return None, problems
    spec = SkillSpec(
        skill_key=key, name=clean_name, cluster=cluster, aliases=tuple(folded)
    )
    # The implicit phrases (display name, de-slugged key) can collide too, and
    # only exist once the spec does — so this check has to come last.
    for phrase in _phrases_for(spec):
        owner = vocab.index.get(phrase)
        if owner is not None and owner != key:
            problems.append(
                f"{phrase!r} is implied by this skill's name or key but already "
                f"means {owner!r}."
            )
    if problems:
        return None, problems
    return spec, []


def resolve_terms(
    vocab: Vocabulary, terms: Iterable[str]
) -> tuple[dict[str, str], list[str]]:
    """Map everyday phrasing onto ``skill_key``, reporting what did not resolve.

    Takes a :class:`Vocabulary` rather than a session so a caller resolving a
    list of terms pays for one DB read, not one per term.

    A bare ``skill_key`` is accepted alongside aliases: the picker sends keys and
    a human types phrases, and both arrive on the same query parameter.
    ``_deslug`` puts "data privacy" in the index but not "data_privacy", so the
    key has to be checked separately.

    Unresolved terms come back as a LIST rather than being dropped, because
    "nobody has this skill" and "we do not track this skill" are different
    answers and a caller that cannot tell them apart will report the wrong one.
    Order is the caller's order, so a UI can line results up against the input.
    """
    resolved: dict[str, str] = {}
    unknown: list[str] = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            continue
        key = vocab.resolve(term)
        if key is None:
            candidate = term.strip().lower()
            if candidate in vocab.specs:
                key = candidate
        if key is None:
            unknown.append(term)
        else:
            resolved[term] = key
    return resolved, unknown


def default_vocabulary() -> Vocabulary:
    """The curated list only. Cached — :data:`SKILLS` cannot change at runtime."""
    global _DEFAULT_VOCABULARY
    if _DEFAULT_VOCABULARY is None:
        _DEFAULT_VOCABULARY = Vocabulary(
            index=alias_index(), pattern=_scan_pattern(), specs=SKILLS
        )
    return _DEFAULT_VOCABULARY


def excerpt_around(text: str, start: int, end: int, *, window: int = 100) -> str:
    """±``window`` characters around a hit, flattened and capped.

    Same primitive as ``backend/signals/transcript.py:_excerpt_around`` and the
    same reason: the citation a person is shown has to be short enough to read
    and long enough to recognise.
    """
    left = max(0, start - window)
    right = min(len(text), end + window)
    snippet = " ".join(text[left:right].split())
    return snippet[: 2 * window + 60]


def clusters_present() -> tuple[str, ...]:
    """Clusters that actually have a skill in them, in presentation order."""
    used = {spec.cluster for spec in SKILLS.values()}
    return tuple(cluster for cluster in CLUSTERS if cluster in used)


# ── Seeding ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SeedResult:
    """What one :func:`seed_taxonomy` pass did. Idempotent by construction."""

    inserted: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    #: Rows in the table that this module does not ship. Left completely alone
    #: (they may be hand-curated additions), reported so a run says so out loud
    #: rather than looking like it managed them.
    unmanaged: tuple[str, ...]

    @property
    def wrote_anything(self) -> bool:
        return bool(self.inserted or self.updated)

    def to_dict(self) -> dict[str, object]:
        return {
            "inserted": list(self.inserted),
            "updated": list(self.updated),
            "unchanged": list(self.unchanged),
            "unmanaged": list(self.unmanaged),
        }


def seed_taxonomy(
    db,
    *,
    specs: Sequence[SkillSpec] | None = None,
    now: datetime | None = None,
) -> SeedResult:
    """Write :data:`SKILLS` into ``st_skills_taxonomy``. Safe to run repeatedly.

    Three rules, each with a reason a re-run would otherwise get wrong:

    * ``name``, ``cluster`` and ``aliases`` are overwritten from this module —
      it is the source of truth for wording, and a curated rename should land.
    * ``is_active`` is set on INSERT only. Retirement is an admin decision
      (``is_active = False``), and a nightly seed that re-activated every
      retired row would silently undo it every night.
    * a row this module does not ship is never touched and never deleted —
      ``st_person_skills`` holds a RESTRICT foreign key here, so a delete would
      either fail or erase somebody's claimed history.

    **Seeding turns membership validation ON.** While the table is empty,
    ``backend/routers/smart_teams.py:_known_skill_keys`` shape-checks declared
    skill keys only and logs that it is doing so. The first successful seed ends
    that, which is why this runs before anyone declares at scale: a key someone
    declared during the shape-only window is not guaranteed to resolve
    afterwards.
    """
    from rubik_core.database.smart_teams import SmartTeamsSkill

    stamp = now or datetime.utcnow()
    wanted = {spec.skill_key: spec for spec in (specs or list(SKILLS.values()))}

    existing = {row.skill_key: row for row in db.query(SmartTeamsSkill).all()}

    inserted: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []

    for skill_key, spec in wanted.items():
        aliases = list(_phrases_for(spec))
        row = existing.get(skill_key)
        if row is None:
            db.add(
                SmartTeamsSkill(
                    skill_key=skill_key,
                    name=spec.name,
                    cluster=spec.cluster,
                    aliases=aliases,
                    is_active=True,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            inserted.append(skill_key)
            continue
        if (
            row.name == spec.name
            and row.cluster == spec.cluster
            and list(row.aliases or []) == aliases
        ):
            unchanged.append(skill_key)
            continue
        row.name = spec.name
        row.cluster = spec.cluster
        row.aliases = aliases
        row.updated_at = stamp
        updated.append(skill_key)

    db.commit()

    result = SeedResult(
        inserted=tuple(sorted(inserted)),
        updated=tuple(sorted(updated)),
        unchanged=tuple(sorted(unchanged)),
        unmanaged=tuple(sorted(set(existing) - set(wanted))),
    )
    logger.info(
        "[smart-teams][skills] taxonomy seed: %d inserted, %d updated, "
        "%d unchanged, %d not managed here",
        len(result.inserted),
        len(result.updated),
        len(result.unchanged),
        len(result.unmanaged),
    )
    return result


def active_specs(db) -> list[object]:
    """The active taxonomy rows, cluster order then name. For the picker."""
    from rubik_core.database.smart_teams import SmartTeamsSkill

    rows = (
        db.query(SmartTeamsSkill)
        .filter(SmartTeamsSkill.is_active.is_(True))
        .order_by(SmartTeamsSkill.cluster.asc(), SmartTeamsSkill.name.asc())
        .all()
    )
    return list(rows)


def search(
    db, *, query: str | None = None, cluster: str | None = None, limit: int = 200
) -> list[object]:
    """Active taxonomy rows matching a free-text query and/or a cluster.

    The query is matched against the name AND the stored aliases, because the
    whole point of the alias list is that somebody typing "gdpr" finds "Data
    privacy (GDPR / CCPA)". Alias matching happens in Python rather than in
    SQL: ``aliases`` is a JSON column and the JSON operators differ between
    SQLite (tests) and Postgres (production), which is exactly the kind of
    divergence that passes CI and fails on deploy.
    """
    rows = active_specs(db)
    if cluster:
        folded = cluster.strip().lower()
        rows = [r for r in rows if (r.cluster or "").strip().lower() == folded]
    needle = (query or "").strip().lower()
    if needle:
        rows = [
            r
            for r in rows
            if needle in (r.name or "").lower()
            or needle in (r.skill_key or "")
            or any(needle in str(alias).lower() for alias in (r.aliases or []))
        ]
    return rows[: max(1, limit)]


def keys_in_db(db, *, active_only: bool = True) -> frozenset[str]:
    """Taxonomy keys the database holds — the membership set writes validate on."""
    from rubik_core.database.smart_teams import SmartTeamsSkill

    query = db.query(SmartTeamsSkill.skill_key)
    if active_only:
        query = query.filter(SmartTeamsSkill.is_active.is_(True))
    return frozenset(key for (key,) in query.all())


def cluster_of(db, skill_keys: Iterable[str]) -> dict[str, str | None]:
    """``skill_key`` → cluster for the keys given. For coverage grouping."""
    from rubik_core.database.smart_teams import SmartTeamsSkill

    keys = [k for k in dict.fromkeys(skill_keys) if k]
    if not keys:
        return {}
    rows = (
        db.query(SmartTeamsSkill.skill_key, SmartTeamsSkill.cluster)
        .filter(SmartTeamsSkill.skill_key.in_(keys))
        .all()
    )
    return {key: cluster for key, cluster in rows}
