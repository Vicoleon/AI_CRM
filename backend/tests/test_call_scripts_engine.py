"""Call Scripts engine — the four rules that fail silently if they break.

``rubik_core/call_capture/scripts.py`` resolves scripts and reports what a call
did not cover. Every rule pinned here shares the property that makes this whole
feature worth building: **when it breaks, nothing errors.** The panel renders,
the wrap-up renders, the sweep logs success, and the AE is told something untrue
about their own call.

1. **Approval, not activity, is what makes a script live** (the design rule:
   *"Do not ship an AI-generated script as authoritative"*). Scripts are MINED from the
   transcript corpus by a batch job, so ``source="mined"`` rows arrive written by
   a machine with ``is_active`` already true. A resolver that gated on
   ``is_active`` would put a model's guess in front of an AE as the company's own
   checklist — plausible questions, no warning, and the first symptom is an SOW
   built on a fact nobody at the company agreed to ask for.

2. **An unmatched fact is a FINDING, not missing data.** ``coverage_for`` emits a
   row for every script item, always. Dropping the unmatched ones — the obvious
   "only report what we found" shape — produces a wrap-up that looks complete
   over an incomplete call, which is precisely the problem the scripts were
   added to solve: what is not asked on the call is missed, and no automation
   afterwards recovers it.

3. **Coverage is per product and never merged.** Lead↔deal is
   1:1, so three products means three leads. A single blended
   percentage over a two-product call is wrong for both products and hides the
   one that was never asked about — and it is the natural thing to write once the
   sections are in a list.

4. **A wrong "split this into a new lead" is worse than none.**
   ``unscripted_products`` is allowed exactly two exact signals and must produce
   NOTHING from a hallucinated product string, a fact key two products share, or
   a vendor field. The failure is an AE being told to create a lead for a product
   nobody discussed, by the feature asking to be trusted about what it heard.

``coverage_for`` is PURE, and its tests take no ``db`` fixture at all — that is
the assertion, not an accident. The database tests build SQLite in memory with
``StaticPool``, following ``backend/tests/test_call_capture_extraction.py``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.call_capture import scripts
from rubik_core.database.call_scripts import (
    CallScript,
    CompanyProductDefault,
    ScriptItem,
)
from rubik_core.database.models import Base

AE = "parker@acme.example"
APPROVER = "harper@acme.example"
COMPANY = "7311992001"

# A sample script: the facts an author would otherwise reverse-engineer out of
# a call summary to build an SOW. Used as a shape,
# never seeded: nothing in the engine or its migration authors company content.
PLAN_FACTS = ("discovery_workshop", "site_count", "hours_per_site", "rate")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # models.py's last-line import is what registers these tables; a dropped
    # name there does not fail loudly, create_all simply stops seeing them.
    assert "call_scripts" in Base.metadata.tables
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _script(
    db,
    product_key: str,
    *,
    version: int = 1,
    approved: bool = True,
    is_active: bool = True,
    facts: tuple[str, ...] = (),
    targets: dict[str, str] | None = None,
    optional: tuple[str, ...] = (),
    name: str | None = None,
    source: str = "manual",
) -> CallScript:
    """One script row plus its items. Content is fixture-local by design."""
    script = CallScript(
        id=uuid.uuid4().hex,
        product_key=product_key,
        name=name or product_key.replace("_", " ").title(),
        version=version,
        is_active=is_active,
        approved_by=APPROVER if approved else None,
        approved_at=datetime.utcnow() if approved else None,
        source=source,
    )
    db.add(script)
    db.flush()
    for sequence, fact in enumerate(facts, start=1):
        db.add(
            ScriptItem(
                id=uuid.uuid4().hex,
                script_id=script.id,
                sequence=sequence,
                prompt_text=f"Ask about {fact.replace('_', ' ')}?",
                fact_key=fact,
                hubspot_target=(targets or {}).get(fact),
                required=fact not in optional,
            )
        )
    db.flush()
    db.refresh(script)
    return script


def _item(
    fact: str,
    *,
    required: bool = True,
    target: str | None = None,
) -> dict:
    """A script item as a plain mapping — ``coverage_for`` needs no ORM."""
    return {
        "fact_key": fact,
        "prompt_text": f"Ask about {fact.replace('_', ' ')}?",
        "required": required,
        "hubspot_target": target,
    }


def _extracted(
    *,
    fact: str | None = None,
    target_property: str | None = None,
    product: str | None = None,
    value: Any = "60",
    edited: Any = None,
    entry: str | None = "entry-1",
    status: str = "proposed",
    confidence: float | None = 0.9,
) -> dict:
    """An extraction item as a plain mapping, shaped like the real row."""
    payload: dict[str, Any] = {}
    if product is not None:
        payload["product"] = product
    if fact is not None:
        payload["fact_key"] = fact
    return {
        "status": status,
        "target_property": target_property,
        "proposed_value": value,
        "edited_value": edited,
        "evidence_entry_id": entry,
        "confidence": confidence,
        "payload": payload or None,
    }


def _by_fact(coverage: list[dict]) -> dict[str, dict]:
    return {row["fact_key"]: row for row in coverage}


# ══ 1. Approval, not activity, is what makes a script live ════════════════════


def test_a_mined_but_unapproved_script_never_reaches_an_ae(db):
    """THE test in this file. ``is_active`` is not a liveness gate.

    A mining job writes an active row for every product it can draft. If
    ``active_script_for`` accepted that, an AE would be shown a model's guess as
    the company's own checklist, with nothing on screen to say so — and the SOW built
    from it is the first symptom, weeks later.
    """
    _script(db, "implementation_plan", approved=False, facts=PLAN_FACTS, source="mined")

    assert scripts.active_script_for(db, "implementation_plan") is None
    assert scripts.scripts_for_products(db, ["implementation_plan"]) == []


def test_a_deactivated_but_approved_script_is_also_not_live(db):
    """The other half: approval alone does not resurrect a retired version."""
    _script(db, "implementation_plan", approved=True, is_active=False, facts=PLAN_FACTS)

    assert scripts.active_script_for(db, "implementation_plan") is None


def test_the_newest_approved_version_wins(db):
    """Editing an approved script forks a new version; the fork is what runs."""
    _script(db, "implementation_plan", version=1, facts=PLAN_FACTS[:2])
    v2 = _script(db, "implementation_plan", version=2, facts=PLAN_FACTS)

    resolved = scripts.active_script_for(db, "implementation_plan")

    assert resolved is not None
    assert resolved.id == v2.id
    assert resolved.version == 2
    assert len(resolved.items) == len(PLAN_FACTS)


def test_an_unapproved_newer_version_does_not_outrank_an_approved_older_one(db):
    """A draft v3 in flight must not blank the panel or leak itself into it.

    Ordering by version and *then* checking approval — the same two clauses in
    the wrong order — silently swaps the live checklist for an unsigned draft,
    or shows nothing at all while Harper is mid-edit.
    """
    v2 = _script(db, "implementation_plan", version=2, facts=PLAN_FACTS)
    _script(db, "implementation_plan", version=3, approved=False, facts=("rate",))

    resolved = scripts.active_script_for(db, "implementation_plan")

    assert resolved is not None and resolved.id == v2.id


def test_an_unknown_or_blank_product_resolves_to_nothing(db):
    """The empty-tables state is the normal early state, not an error."""
    assert scripts.active_script_for(db, "field_service") is None
    assert scripts.active_script_for(db, "") is None
    assert scripts.active_script_for(db, None) is None
    assert scripts.scripts_for_products(db, []) == []
    assert scripts.scripts_for_products(db, None) == []


def test_product_keys_are_matched_folded(db):
    """A casing mismatch on a join key does not raise — it renders no panel."""
    _script(db, "implementation_plan", facts=PLAN_FACTS)

    assert scripts.active_script_for(db, "  Implementation_Plan  ") is not None


def test_scripts_for_products_keeps_the_ae_s_order_and_skips_the_unscripted(db):
    """Sections render in the order the AE picked; a missing script is a skip."""
    _script(db, "implementation_plan", facts=PLAN_FACTS)
    _script(db, "staff_training", facts=("trainee_count",))

    resolved = scripts.scripts_for_products(
        db, ["staff_training", "hr_setup", "implementation_plan", "implementation_plan"]
    )

    assert [s.product_key for s in resolved] == [
        "staff_training",
        "implementation_plan",
    ]


def test_script_payload_carries_the_panel_shape(db):
    """``prompt_text`` and ``fact_key`` stay separate values — they diverge."""
    script = _script(
        db, "implementation_plan", facts=("hours_per_site", "rate"), optional=("rate",)
    )

    payload = scripts.script_payload(script)

    assert payload["product_key"] == "implementation_plan"
    assert payload["script_id"] == script.id
    assert payload["name"] == "Implementation Plan"
    assert [i["fact_key"] for i in payload["items"]] == ["hours_per_site", "rate"]
    assert payload["items"][0]["prompt_text"] == "Ask about hours per site?"
    assert payload["items"][0]["required"] is True
    assert payload["items"][1]["required"] is False
    assert payload["items"][0]["help_text"] is None


# ══ 2. Absence is the product ═════════════════════════════════════════════════


def test_coverage_reports_every_item_when_nothing_was_found():
    """The whole feature in one assertion, and it needs no database.

    A wrap-up that lists only what the extractor found looks clean over a call
    that asked nothing. Every script item produces a row; the unmatched ones are
    the finding.
    """
    coverage = scripts.coverage_for([_item(f) for f in PLAN_FACTS], [])

    assert len(coverage) == len(PLAN_FACTS)
    assert all(row["found"] is False for row in coverage)
    assert all(row["value"] is None for row in coverage)
    assert all(row["evidence_entry_id"] is None for row in coverage)
    assert [row["fact_key"] for row in coverage] == list(PLAN_FACTS)


def test_coverage_reports_every_item_when_everything_was_found():
    covered = [
        _extracted(fact=fact, value=f"value-{fact}", entry=f"entry-{fact}")
        for fact in PLAN_FACTS
    ]

    coverage = scripts.coverage_for([_item(f) for f in PLAN_FACTS], covered)

    assert all(row["found"] is True for row in coverage)
    assert _by_fact(coverage)["rate"]["value"] == "value-rate"
    assert _by_fact(coverage)["rate"]["evidence_entry_id"] == "entry-rate"


def test_coverage_is_partial_and_names_which_facts_are_missing():
    """Harper's live example: three of five asked, two never came up."""
    coverage = scripts.coverage_for(
        [_item(f) for f in PLAN_FACTS],
        [
            _extracted(fact="discovery_workshop", value="yes"),
            _extracted(fact="site_count", value="2"),
            _extracted(fact="hours_per_site", value="60"),
        ],
    )

    found = {row["fact_key"] for row in coverage if row["found"]}
    missing = [row["fact_key"] for row in coverage if not row["found"]]

    assert found == {"discovery_workshop", "site_count", "hours_per_site"}
    assert missing == ["rate"]


def test_a_missed_optional_fact_is_reported_but_is_not_a_required_gap():
    """``required`` drives the "✗ not discussed" surface; both misses are rows.

    The caller derives ``missing_required`` from this list, so an optional miss
    that arrived flagged required would nag every AE on every call — and a
    required miss flagged optional would never be shown at all.
    """
    coverage = scripts.coverage_for(
        [_item("rate"), _item("commencement_date", required=False)],
        [],
    )
    missing_required = [
        row["fact_key"] for row in coverage if row["required"] and not row["found"]
    ]

    assert len(coverage) == 2
    assert missing_required == ["rate"]
    assert coverage[1]["found"] is False


def test_an_item_that_never_says_required_is_treated_as_required():
    """Matching the column default, and the asymmetry behind it.

    A wrongly-required fact shows a gap the AE dismisses in a second. A wrongly-
    optional one never shows a gap at all, which is the silent failure.
    """
    coverage = scripts.coverage_for([{"fact_key": "rate", "prompt_text": "?"}], [])

    assert coverage[0]["required"] is True


def test_coverage_matches_the_hubspot_target_property_against_todays_extractor():
    """The signal that works against the SHIPPED extraction, not a future one.

    ``call_extraction_items`` has no ``fact_key`` column and the prose output
    contract does not ask for one, so without this branch coverage would report
    every fact as not discussed on every real call today.
    """
    coverage = scripts.coverage_for(
        [_item("supplier", target="company.supplier")],
        [_extracted(target_property="supplier", value="Cendra", entry="e9")],
    )

    assert coverage[0]["found"] is True
    assert coverage[0]["value"] == "Cendra"
    assert coverage[0]["evidence_entry_id"] == "e9"


def test_the_object_half_of_a_hubspot_target_is_not_matched_on():
    """``company.`` vs ``companies.`` must not become "not discussed".

    Harper types the target into a form. Whichever spelling Harper uses, the fact
    was covered, and a false ✗ costs the gap list its credibility faster than a
    rare cross-object property collision costs anything.
    """
    coverage = scripts.coverage_for(
        [_item("supplier", target="companies.supplier")],
        [_extracted(target_property="supplier", value="Cendra")],
    )

    assert coverage[0]["found"] is True


def test_a_matched_fact_with_no_valid_value_is_still_covered():
    """The "Sendra vs Cemdra" case: heard, quoted, but outside the CRM's options.

    ``proposed_value`` is None with the heard string in ``needs_new_option``.
    Flipping that to "not discussed" would tell the AE to go and ask a question
    the client already answered.
    """
    coverage = scripts.coverage_for(
        [_item("supplier", target="company.supplier")],
        [_extracted(target_property="supplier", value=None, entry="e4")],
    )

    assert coverage[0]["found"] is True
    assert coverage[0]["value"] is None
    assert coverage[0]["evidence_entry_id"] == "e4"


def test_an_edited_value_beats_the_models_proposal():
    """The AE's correction is the fact; the model's first guess is not."""
    coverage = scripts.coverage_for(
        [_item("hours_per_site")],
        [_extracted(fact="hours_per_site", value="60", edited="45")],
    )

    assert coverage[0]["value"] == "45"


def test_superseded_and_rejected_proposals_do_not_cover_a_fact():
    """A retired guess and an overruled one are not evidence of coverage.

    ``superseded`` is an earlier run of the same call; ``rejected`` is the AE
    saying the proposal is wrong. Counting either marks the fact ✓ from
    something nobody stands behind.
    """
    coverage = scripts.coverage_for(
        [_item("rate"), _item("site_count")],
        [
            _extracted(fact="rate", status="superseded"),
            _extracted(fact="site_count", status="rejected"),
        ],
    )

    assert [row["found"] for row in coverage] == [False, False]


def test_the_more_confident_of_two_matches_supplies_the_value():
    coverage = scripts.coverage_for(
        [_item("hours_per_site")],
        [
            _extracted(fact="hours_per_site", value="about 60", confidence=0.4),
            _extracted(fact="hours_per_site", value="60", confidence=0.9),
        ],
    )

    assert coverage[0]["value"] == "60"


def test_coverage_touches_no_database(db):
    """Purity, asserted rather than described.

    ``coverage_for`` takes no session at all, and the fixture is requested here
    only to prove the assertion is about a database that exists.
    """
    assert "db" not in inspect.signature(scripts.coverage_for).parameters
    assert scripts.coverage_for([_item("rate")], []) == [
        {
            "fact_key": "rate",
            "prompt_text": "Ask about rate?",
            "required": True,
            "found": False,
            "value": None,
            "evidence_entry_id": None,
        }
    ]


# ══ 3. Coverage is per product, never merged ══════════════════════════════════


def test_two_products_each_compute_their_own_coverage(db):
    """The multi-product behaviour is the point, not an edge case.

    One blended number over this call would read "3 of 6 covered" and hide the
    fact that staff training was never asked about at all — which is the
    product the AE would have to split into its own lead.
    """
    implementation_plan = _script(db, "implementation_plan", facts=PLAN_FACTS)
    supplier = _script(db, "staff_training", facts=("trainee_count", "modules_needed"))
    extraction = [
        _extracted(fact="discovery_workshop", value="yes"),
        _extracted(fact="site_count", value="2"),
        _extracted(fact="hours_per_site", value="60"),
    ]

    sections = [
        {
            "product_key": script.product_key,
            "items": scripts.coverage_for(script.items, extraction),
        }
        for script in scripts.scripts_for_products(
            db, ["implementation_plan", "staff_training"]
        )
    ]

    assert [s["product_key"] for s in sections] == [
        implementation_plan.product_key,
        supplier.product_key,
    ]
    ia_found = [row["found"] for row in sections[0]["items"]]
    so_found = [row["found"] for row in sections[1]["items"]]
    assert ia_found == [True, True, True, False]
    assert so_found == [False, False]
    # Every item of every product is still reported — nothing is pooled away.
    assert len(sections[0]["items"]) + len(sections[1]["items"]) == 6


def test_one_products_facts_never_satisfy_anothers(db):
    """Coverage is computed against ONE script's items, per section.

    Passing the whole call's extraction to each section is correct and expected;
    passing another product's *items* is what a merged implementation does, and
    it would report a product covered by facts belonging to a different one.
    """
    _script(db, "implementation_plan", facts=("hours_per_site",))
    _script(db, "staff_training", facts=("trainee_count",))
    extraction = [_extracted(fact="hours_per_site", value="60")]

    plan, training = scripts.scripts_for_products(
        db, ["implementation_plan", "staff_training"]
    )

    assert scripts.coverage_for(plan.items, extraction)[0]["found"] is True
    assert scripts.coverage_for(training.items, extraction)[0]["found"] is False


# ══ 4. A product discussed that the call was never scripted for ═══════════════


def test_a_discussed_product_with_no_script_attached_is_surfaced(db):
    """The sentence the multi-product design exists for.

    *"Staff training was discussed but this call is only scripted for the
    implementation plan — split it into its own lead?"* Lead↔deal is 1:1, so
    each product belongs on its own lead, and AEs generally do not split them.
    """
    found = scripts.unscripted_products(
        db,
        [
            _extracted(product="implementation_plan"),
            _extracted(product="staff_training"),
        ],
        ["implementation_plan"],
    )

    assert found == ["staff_training"]


def test_an_attached_product_is_never_reported_as_unscripted(db):
    """Including when the model's casing differs from the AE's pick."""
    found = scripts.unscripted_products(
        db, [_extracted(product="Implementation_Plan ")], ["implementation_plan"]
    )

    assert found == []


def test_a_hallucinated_product_string_produces_no_prompt(db):
    """``validators._validate_lead`` enforces no enum and caps no length.

    A wrong "split this into a new lead" is worse than none: it is an AE being
    told to create a lead for a product nobody discussed, by the feature asking
    to be trusted about what it heard.
    """
    found = scripts.unscripted_products(
        db,
        [
            _extracted(
                product=(
                    "they mentioned wanting a review of their whole payroll "
                    "setup at some point next year, possibly"
                )
            ),
            _extracted(product="implementation plan; drop table"),
            _extracted(product="   "),
        ],
        [],
    )

    assert found == []


def test_a_vendor_field_produces_no_split_prompt(db):
    """Firmographics about a happy incumbent are not expressed interest.

    "They use Cendra for payroll" carries no product in its payload, and
    that is the correct population to be silent about — the prompt itself says a
    vendor field is NOT a lead.
    """
    found = scripts.unscripted_products(
        db, [_extracted(target_property="supplier", value="Cendra")], []
    )

    assert found == []


def test_the_no_signal_case_is_explicit_and_queries_nothing(db):
    """Today's extraction items carry no product and no fact key at all.

    Nothing emits ``fact_key`` yet, so this function must reach the database
    zero times on a real wrap-up. The stub raises rather than returning empty:
    "it made a query I did not expect" has to fail loudly here, since the
    alternative is one extra round trip per wrap-up render forever.
    """

    class _Explodes:
        def execute(self, *_a, **_k):  # pragma: no cover - the point is not to
            raise AssertionError("unscripted_products queried with nothing to resolve")

    assert scripts.unscripted_products(_Explodes(), [_extracted()], []) == []
    assert scripts.unscripted_products(_Explodes(), [], ["implementation_plan"]) == []
    assert scripts.unscripted_products(_Explodes(), None, None) == []


def test_a_fact_key_unique_to_one_live_script_names_its_product(db):
    """Signal 2, forward-compatible with a script-aware extraction.

    Nothing emits fact keys today; the day ``_OUTPUT_CONTRACT`` gains them (with
    the PROMPT_VERSION/SCHEMA_VERSION bump any contract change requires), this
    starts working with no change to the engine.
    """
    _script(db, "implementation_plan", facts=("hours_per_site",))
    _script(db, "staff_training", facts=("trainee_count",))

    found = scripts.unscripted_products(
        db, [_extracted(fact="trainee_count")], ["implementation_plan"]
    )

    assert found == ["staff_training"]


def test_a_fact_key_two_products_share_names_neither(db):
    """Ambiguity yields nothing — no "closest" product, no guess.

    ``commencement_date`` belongs to every SOW-shaped product. Picking one would
    be a confident classifier over a signal that carries no product information.
    """
    _script(db, "implementation_plan", facts=("commencement_date",))
    _script(db, "staff_training", facts=("commencement_date",))

    found = scripts.unscripted_products(db, [_extracted(fact="commencement_date")], [])

    assert found == []


def test_an_unapproved_scripts_fact_keys_are_not_a_signal_either(db):
    """A mined draft must not name products in the wrap-up before it is signed."""
    _script(db, "staff_training", approved=False, facts=("trainee_count",))

    found = scripts.unscripted_products(db, [_extracted(fact="trainee_count")], [])

    assert found == []


def test_superseded_and_rejected_items_do_not_propose_a_split(db):
    """Same rule as coverage: a retired or overruled proposal is not evidence."""
    found = scripts.unscripted_products(
        db,
        [
            _extracted(product="staff_training", status="superseded"),
            _extracted(product="hr_setup", status="rejected"),
        ],
        [],
    )

    assert found == []


def test_a_product_with_no_script_at_all_is_still_worth_splitting(db):
    """Reported because it was DISCUSSED and NOT ATTACHED, not because it is
    scripted.

    Requiring an approved script here would mute the feature exactly while the
    tables are still filling up — which is now, and for as long as Indigo's
    product list is unfinished.
    """
    found = scripts.unscripted_products(db, [_extracted(product="hr_setup")], [])

    assert found == ["hr_setup"]
    assert scripts.active_script_for(db, "hr_setup") is None


def test_each_unscripted_product_is_reported_once_in_first_seen_order(db):
    found = scripts.unscripted_products(
        db,
        [
            _extracted(product="hr_setup"),
            _extracted(product="staff_training"),
            _extracted(product="hr_setup"),
        ],
        [],
    )

    assert found == ["hr_setup", "staff_training"]


# ══ 5. Remembering the AE's pick per client ═══════════════════════════════════


def test_remembered_products_round_trip(db):
    scripts.remember_products(db, AE, COMPANY, ["implementation_plan", "hr_setup"])

    assert scripts.remembered_products(db, AE, COMPANY) == [
        "implementation_plan",
        "hr_setup",
    ]


def test_remembering_again_updates_the_same_row(db):
    """An upsert, not an insert. A second row would make the pick order-dependent.

    ``uq_call_company_product_defaults_ae_company`` would reject the duplicate,
    so the visible failure of an insert-only writer is a 500 at Start — but only
    on the second call with the same client, which is the one nobody tests.
    """
    scripts.remember_products(db, AE, COMPANY, ["implementation_plan"])
    scripts.remember_products(db, AE, COMPANY, ["staff_training", "hr_setup"])

    rows = db.query(CompanyProductDefault).all()

    assert len(rows) == 1
    assert scripts.remembered_products(db, AE, COMPANY) == [
        "staff_training",
        "hr_setup",
    ]


def test_the_stored_list_is_reassigned_so_the_update_actually_persists(db):
    """SQLAlchemy does not track in-place mutation of a plain JSON column.

    ``row.product_keys.append(...)`` emits no UPDATE and vanishes with no error.
    Expiring the instance forces the assertion to read the DATABASE, not the
    identity map, which is the only way this failure is visible.
    """
    scripts.remember_products(db, AE, COMPANY, ["implementation_plan"])
    scripts.remember_products(db, AE, COMPANY, ["implementation_plan", "hr_setup"])
    db.commit()
    db.expire_all()

    assert scripts.remembered_products(db, AE, COMPANY) == [
        "implementation_plan",
        "hr_setup",
    ]


def test_an_empty_pick_clears_the_memory(db):
    """The AE deselected everything, and that is a real answer.

    The corresponding trap belongs to the router and is written into the
    docstring: ``POST /api/calls/start`` must SKIP this call when the request
    omitted ``product_keys`` entirely, or an older client silently wipes a pick
    the AE made last week.
    """
    scripts.remember_products(db, AE, COMPANY, ["implementation_plan"])
    scripts.remember_products(db, AE, COMPANY, [])

    assert scripts.remembered_products(db, AE, COMPANY) == []


def test_the_memory_is_per_ae_and_per_company(db):
    """Two AEs sell different things into the same account."""
    other_ae = "harper@acme.example"
    scripts.remember_products(db, AE, COMPANY, ["implementation_plan"])
    scripts.remember_products(db, other_ae, COMPANY, ["staff_training"])
    scripts.remember_products(db, AE, "9999", ["hr_setup"])

    assert scripts.remembered_products(db, AE, COMPANY) == ["implementation_plan"]
    assert scripts.remembered_products(db, other_ae, COMPANY) == ["staff_training"]
    assert scripts.remembered_products(db, AE, "9999") == ["hr_setup"]


def test_the_ae_email_is_folded_on_write_and_on_read(db):
    """``users.email`` is stored verbatim from the Google token.

    An un-folded row makes the same human two different people, and the AE's
    remembered pick silently stops being found.
    """
    scripts.remember_products(
        db, "Parker@ACME.example ", COMPANY, ["implementation_plan"]
    )

    row = db.query(CompanyProductDefault).one()

    assert row.ae_email == AE
    assert scripts.remembered_products(db, "PARKER@acme.example", COMPANY) == [
        "implementation_plan"
    ]


def test_an_unbound_call_remembers_nothing_rather_than_sharing_a_row(db):
    """The ad-hoc path has no company binding — ``hubspot_company_id`` is nullable.

    A row keyed on ``""`` would collide every AE's ad-hoc picks into one shared
    memory, which is worse than not remembering.
    """
    scripts.remember_products(db, AE, None, ["implementation_plan"])
    scripts.remember_products(db, None, COMPANY, ["implementation_plan"])

    assert db.query(CompanyProductDefault).count() == 0
    assert scripts.remembered_products(db, AE, None) == []
    assert scripts.remembered_products(db, None, COMPANY) == []


def test_remembered_products_are_not_filtered_by_what_has_a_script(db):
    """ "What the AE picked" and "what Cubie can show a checklist for" differ.

    Silently unpicking a product whose script was retired would look to the AE
    like Cubie forgot their choice.
    """
    scripts.remember_products(db, AE, COMPANY, ["implementation_plan", "field_service"])

    assert scripts.remembered_products(db, AE, COMPANY) == [
        "implementation_plan",
        "field_service",
    ]
    assert (
        scripts.scripts_for_products(db, ["implementation_plan", "field_service"]) == []
    )
