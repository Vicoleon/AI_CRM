"""The Big Five backbone of the Work Style Interview — items, scoring, and honesty rails.

Every other layer of a Smart Teams profile is either authored in-house (DISC-style
styles, the strengths taxonomy, the motivator axes) or derived from something else
(the 16-type letters). This module is the one place where the numbers rest on a
published, replicated, externally validated instrument, which is exactly why it is
the backbone: when a working guide says something about a person, the sentence
should trace back to here.

**The five domains, in work terms.** Named as the interview names them, not as a
psychology textbook does:

``extraversion``
    How much of the person's thinking happens out loud, and how much energy they
    get from a room. Predicts whether they want a call or a doc, and whether
    silence in a meeting means agreement or means they have not been asked.
``agreeableness``
    How heavily other people's feelings weigh in a decision. High is not "nice"
    and low is not "difficult" — low agreeableness is where blunt, useful,
    unpopular feedback comes from, and high agreeableness is where a team keeps
    talking to each other after a hard quarter.
``conscientiousness``
    Structure and follow-through: plans, order, finishing. Predicts whether the
    person wants the project plan up front or wants to start and shape it.
``emotional_stability``
    Evenness under load. Says nothing about how much load someone can carry, only
    about how visible the load is and how fast they reset. See the polarity note
    below — this domain is deliberately NOT named "neuroticism".
``openness``
    Appetite for abstraction and for new approaches versus proven ones. Predicts
    whether "we've always done it this way" reads as reassuring or as a problem.

**Why Big Five is the backbone.** Two reasons, and the second one matters as much
as the first. It is the most validated model of normal adult personality that
exists — five-factor structure replicates across samples, instruments and
languages, and it is the model the research literature actually uses. And it is
free: the International Personality Item Pool is public domain, so we can put
validated items in a git repository, score them in our own code, and read the
scoring key without a licence. Every commercial alternative fails at least one of
those two tests, and most fail both.

**Provenance: the IPIP-50, verbatim — and here is how that was checked.** All 50
items below are Goldberg's IPIP Big-Five factor markers. Every one of
them was verified item-by-item — wording, factor, and keying sign — against the
printable "Big Five Personality Test" sheet published by openpsychometrics.org and
credited on its own face to ``ipip.ori.org``
(https://openpsychometrics.org/printable/big-five-personality-test.pdf), whose
scoring formulas also reproduce IPIP's per-item signs. Fifty of fifty matched, with
zero text or keying discrepancies; the check is a script, not a reading, and it is
written out in docs/smart_teams/instruments/big_five_and_type16.md so it can be
re-run. What we did *not* do is fetch ``ipip.ori.org`` itself during that pass, so
nothing here should be read as "transcribed from the official site" — the official
pages are the source of record and are cited in the doc, but the verification
evidence is the independent reproduction named above.

IPIP's public-domain status is likewise cited, not assumed: the University of
Oregon Personality and Social Dynamics Lab states flatly that "IPIP scales are 100%
public domain – no permission required, ever"
(https://psdlab.uoregon.edu/measuring-the-big-five-personality-domains). This is
the one layer of Smart Teams where verbatim reuse of someone else's items is both
legal and *desirable* — a backbone whose items we invented ourselves would have no
validity to inherit.

:attr:`Provenance.AUTHORED` exists for items we may add later. It is currently
unused, and that is the point: an item is ``ipip`` only if its wording was checked
against a source, and the bank's provenance counts (50 ``ipip``, 0 ``authored``)
are the audited result rather than an assumption. Anything we write ourselves gets
marked ``authored`` so a future reader can tell which half of the bank carries the
published psychometrics.

**Polarity: this module scores Emotional Stability, not Neuroticism.** IPIP keys
Factor IV toward stability, so "Get stressed out easily" is a *minus* item. We keep
IPIP's own direction rather than flipping it, because flipping it would mean the
``keyed`` field in this file no longer matches the source it claims to come from,
and provenance you have to mentally invert is provenance nobody checks.
:attr:`BigFiveResult.neuroticism_equivalent` gives the conventional Big Five
direction for anyone comparing against published research. The work-facing label
stays "Emotional Stability": a guide the whole company reads should not tell
someone their "neuroticism" score.

**The Emotional Stability keying imbalance is real, and we measure it instead of
hiding it.** IPIP-50 keys that domain 2 plus / 8 minus. A yes-saying respondent
therefore drifts toward *low* stability for reasons that have nothing to do with
them. We did not "fix" this by swapping in authored items with the opposite keying:
pulling items out of a validated marker set discards the validation we adopted the
set for. Instead :func:`response_quality` measures the response style directly —
see :func:`_balanced_acquiescence`, which averages only keying-balanced *pairs* of
items so that a real trait score cancels out of the index instead of masquerading
as yes-saying — and names Emotional Stability as the domain a skewed index distorts
most. See :data:`ACQUIESCENCE_SENSITIVE_DOMAINS`. The flag stays advisory either
way (:data:`ADVISORY_FLAGS`): no within-person index can fully separate style from
trait, so it asks a question and never invalidates a sitting.

**Facets are reported only where the items support one.** The IPIP-50 is a
domain-level instrument; it was never keyed to facets. We map each item onto the
ten-aspect structure of DeYoung, Quilty & Peterson (2007) by *content*, then refuse
to report an aspect unless it clears :data:`MIN_ITEMS_PER_FACET` and stays under
:data:`MAX_FACET_SHARE` of its domain. Agreeableness loses both of its aspects
under that rule — eight of its ten items are compassion-flavoured and only two are
politeness-flavoured — so Agreeableness is reported as a domain and nothing more.
That is the honest result, and printing a "politeness" number off two items would
have been the dishonest one.

**Scoring is entirely deterministic — no LLM calls, so no AI-cost category.** The
conversational segments of the interview cost money and are attributed elsewhere;
this module is arithmetic.

**Bands are positions on our scale, not percentiles.** See
:data:`BANDS_ARE_NOT_PERCENTILES`. We have no in-house norm sample and no licence to
anyone else's, so a band says where a score sits between the floor and ceiling of
this instrument, phrased as a description a person can disagree with. It is never a
verdict, and per the Smart Teams contract it never informs review, promotion or
compensation.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response scale
# ---------------------------------------------------------------------------

# IPIP administers its inventories on a five-point *accuracy* scale rather than a
# generic agree/disagree one, and the interview presents it that way because the
# items are self-descriptions ("Am the life of the party"): "how accurate is this
# about you" is the question they actually answer.
#
# Provenance of these five labels, precisely, because it is weaker than the item
# bank's. "Very Inaccurate" at 1 and "Very Accurate" at 5 are IPIP's anchors as
# reported by secondary descriptions of its administration procedure; we did not
# read IPIP's own instruction page in this pass, and — worth knowing — the
# printable sheet we verified the *items* against re-presents them on a
# disagree/agree scale instead, so it does not corroborate these words. The three
# middle labels are ours, written in the same idiom. Nothing in the scoring depends
# on the wording: the arithmetic uses the 1-5 values only.
RESPONSE_MIN = 1
RESPONSE_MAX = 5
RESPONSE_NEUTRAL = 3

RESPONSE_LABELS: Mapping[int, str] = {
    1: "Very inaccurate",
    2: "Moderately inaccurate",
    3: "Neither inaccurate nor accurate",
    4: "Moderately accurate",
    5: "Very accurate",
}

# Every item is presented in the first person and reads as a continuation of this
# stem, which is how IPIP administers them — the reproduction we verified against
# heads its answer column "I…" and starts every item mid-sentence for exactly this
# reason.
ITEM_STEM = "I…"


class Domain(str, Enum):
    """The five scored domains, keyed to IPIP's own factor names.

    ``str``-valued so these land in profile JSON and the ``st_profiles`` big5
    column as stable strings rather than integers nobody can read in a DB row.
    """

    EXTRAVERSION = "extraversion"
    AGREEABLENESS = "agreeableness"
    CONSCIENTIOUSNESS = "conscientiousness"
    EMOTIONAL_STABILITY = "emotional_stability"
    OPENNESS = "openness"


# What each domain is called and what it means to a colleague reading a guide.
# These strings are user-facing: plain, non-clinical, and framed as behaviour
# somebody could actually plan around.
DOMAIN_LABELS: Mapping[Domain, str] = {
    Domain.EXTRAVERSION: "Outward energy",
    Domain.AGREEABLENESS: "People-weighting",
    Domain.CONSCIENTIOUSNESS: "Structure and follow-through",
    Domain.EMOTIONAL_STABILITY: "Evenness under load",
    Domain.OPENNESS: "Appetite for the new",
}

DOMAIN_WORK_MEANING: Mapping[Domain, str] = {
    Domain.EXTRAVERSION: "How much of this person's thinking happens out loud, and whether they would rather talk it through or read it first.",
    Domain.AGREEABLENESS: "How heavily other people's reactions weigh when they decide. Low is where blunt useful feedback comes from; high is where a team stays on speaking terms.",
    Domain.CONSCIENTIOUSNESS: "Whether they want the plan before they start, or want to start and let the plan emerge.",
    Domain.EMOTIONAL_STABILITY: "How visible pressure is from the outside, and how quickly they reset. Not a measure of how much they can carry.",
    Domain.OPENNESS: "Whether a new approach reads as interesting or as risk, and how much abstraction they enjoy.",
}


class Provenance(str, Enum):
    """Where an item's wording came from.

    ``IPIP`` means the text was transcribed from the public-domain IPIP source
    pages and carries that instrument's published validity. ``AUTHORED`` means we
    wrote it — legitimate, but it inherits nothing, and any analysis that leans on
    published psychometrics must exclude it. Never label an item ``IPIP`` from
    memory; if the wording cannot be checked against the source, it is authored.
    """

    IPIP = "ipip"
    AUTHORED = "authored"


class Facet(str, Enum):
    """Aspect-level sub-scales, following DeYoung, Quilty & Peterson (2007).

    Two aspects sit inside each Big Five domain. Our assignment of IPIP-50 items
    to them is a *content* judgement, not a validated key — the BFAS has its own
    100 items and we are not using them. Treat a facet score as a hint about which
    half of a domain is driving it, never as an independent measurement.

    Members are named ``<domain letter>_<aspect>``, and the Emotional Stability
    pair is deliberately named in the stability direction to match the domain's
    polarity: ``ES_COMPOSURE`` is low volatility, ``ES_FREEDOM_FROM_WORRY`` is low
    withdrawal.

    Two of the ten member names are ours rather than the BFAS's own. Its Openness
    aspects are "Intellect" and "Openness", and an aspect sharing its parent
    domain's name reads as a typo in a guide, so the second is ``O_IMAGINATION``
    here. Its Emotional Stability aspects are Volatility and Withdrawal, named in
    the neuroticism direction we do not use. The structure is DeYoung, Quilty &
    Peterson's; these four labels are not, and neither is the item assignment.
    """

    E_ENTHUSIASM = "e_enthusiasm"
    E_ASSERTIVENESS = "e_assertiveness"
    A_COMPASSION = "a_compassion"
    A_POLITENESS = "a_politeness"
    C_INDUSTRIOUSNESS = "c_industriousness"
    C_ORDERLINESS = "c_orderliness"
    ES_COMPOSURE = "es_composure"
    ES_FREEDOM_FROM_WORRY = "es_freedom_from_worry"
    O_INTELLECT = "o_intellect"
    O_IMAGINATION = "o_imagination"


FACET_LABELS: Mapping[Facet, str] = {
    Facet.E_ENTHUSIASM: "Sociability",
    Facet.E_ASSERTIVENESS: "Speaking up",
    Facet.A_COMPASSION: "Compassion",
    Facet.A_POLITENESS: "Politeness",
    Facet.C_INDUSTRIOUSNESS: "Follow-through",
    Facet.C_ORDERLINESS: "Orderliness",
    Facet.ES_COMPOSURE: "Even temper",
    Facet.ES_FREEDOM_FROM_WORRY: "Freedom from worry",
    Facet.O_INTELLECT: "Abstraction",
    Facet.O_IMAGINATION: "Idea generation",
}


@dataclass(frozen=True, slots=True)
class Item:
    """One item in the bank. Frozen: the bank is a published artefact, not state.

    :param item_id: Stable id written into ``st_interviews`` answers. Never reuse
        or repurpose one — a stored session is only interpretable against the ids
        it was answered with.
    :param keyed: ``+1`` if agreement raises the domain score, ``-1`` if it lowers
        it. IPIP's own sign, in IPIP's own polarity (see the module docstring on
        Emotional Stability).
    :param ipip_number: Position in the official 50-item questionnaire, so any
        line here can be checked against the source in one lookup. ``None`` for
        authored items.
    :param facet: Content-based aspect assignment; ``None`` if the item does not
        sit cleanly in either aspect of its domain.
    """

    item_id: str
    domain: Domain
    keyed: int
    text: str
    provenance: Provenance
    ipip_number: int | None = None
    facet: Facet | None = None

    @property
    def is_reverse_keyed(self) -> bool:
        return self.keyed < 0


# ---------------------------------------------------------------------------
# The item bank
# ---------------------------------------------------------------------------
#
# Held in official IPIP-50 questionnaire order (item 1 → item 50), which cycles
# E → A → C → ES → O ten times. Keeping that order is not just provenance
# tidiness: consecutive same-domain items are what invite a respondent to answer
# the block instead of the item, and the interleave is the cheapest straight-lining
# defence there is. Present them in this order.
#
# Wording is verbatim, and "verbatim" here means checked: every text, domain, and
# keying sign below was diffed against the IPIP-credited reproduction named in the
# module docstring, all 50 matching. The leading "I" is carried by ITEM_STEM,
# exactly as IPIP administers it, so the text below starts mid-sentence on purpose.

ITEMS: tuple[Item, ...] = (
    Item(
        "bf_e_01",
        Domain.EXTRAVERSION,
        +1,
        "Am the life of the party.",
        Provenance.IPIP,
        1,
        Facet.E_ENTHUSIASM,
    ),
    Item(
        "bf_a_01",
        Domain.AGREEABLENESS,
        -1,
        "Feel little concern for others.",
        Provenance.IPIP,
        2,
        Facet.A_COMPASSION,
    ),
    Item(
        "bf_c_01",
        Domain.CONSCIENTIOUSNESS,
        +1,
        "Am always prepared.",
        Provenance.IPIP,
        3,
        Facet.C_INDUSTRIOUSNESS,
    ),
    Item(
        "bf_es_01",
        Domain.EMOTIONAL_STABILITY,
        -1,
        "Get stressed out easily.",
        Provenance.IPIP,
        4,
        Facet.ES_FREEDOM_FROM_WORRY,
    ),
    Item(
        "bf_o_01",
        Domain.OPENNESS,
        +1,
        "Have a rich vocabulary.",
        Provenance.IPIP,
        5,
        Facet.O_INTELLECT,
    ),
    Item(
        "bf_e_02",
        Domain.EXTRAVERSION,
        -1,
        "Don't talk a lot.",
        Provenance.IPIP,
        6,
        Facet.E_ASSERTIVENESS,
    ),
    Item(
        "bf_a_02",
        Domain.AGREEABLENESS,
        +1,
        "Am interested in people.",
        Provenance.IPIP,
        7,
        Facet.A_COMPASSION,
    ),
    Item(
        "bf_c_02",
        Domain.CONSCIENTIOUSNESS,
        -1,
        "Leave my belongings around.",
        Provenance.IPIP,
        8,
        Facet.C_ORDERLINESS,
    ),
    Item(
        "bf_es_02",
        Domain.EMOTIONAL_STABILITY,
        +1,
        "Am relaxed most of the time.",
        Provenance.IPIP,
        9,
        Facet.ES_FREEDOM_FROM_WORRY,
    ),
    Item(
        "bf_o_02",
        Domain.OPENNESS,
        -1,
        "Have difficulty understanding abstract ideas.",
        Provenance.IPIP,
        10,
        Facet.O_INTELLECT,
    ),
    Item(
        "bf_e_03",
        Domain.EXTRAVERSION,
        +1,
        "Feel comfortable around people.",
        Provenance.IPIP,
        11,
        Facet.E_ENTHUSIASM,
    ),
    Item(
        "bf_a_03",
        Domain.AGREEABLENESS,
        -1,
        "Insult people.",
        Provenance.IPIP,
        12,
        Facet.A_POLITENESS,
    ),
    Item(
        "bf_c_03",
        Domain.CONSCIENTIOUSNESS,
        +1,
        "Pay attention to details.",
        Provenance.IPIP,
        13,
        Facet.C_ORDERLINESS,
    ),
    Item(
        "bf_es_03",
        Domain.EMOTIONAL_STABILITY,
        -1,
        "Worry about things.",
        Provenance.IPIP,
        14,
        Facet.ES_FREEDOM_FROM_WORRY,
    ),
    Item(
        "bf_o_03",
        Domain.OPENNESS,
        +1,
        "Have a vivid imagination.",
        Provenance.IPIP,
        15,
        Facet.O_IMAGINATION,
    ),
    Item(
        "bf_e_04",
        Domain.EXTRAVERSION,
        -1,
        "Keep in the background.",
        Provenance.IPIP,
        16,
        Facet.E_ASSERTIVENESS,
    ),
    Item(
        "bf_a_04",
        Domain.AGREEABLENESS,
        +1,
        "Sympathize with others' feelings.",
        Provenance.IPIP,
        17,
        Facet.A_COMPASSION,
    ),
    Item(
        "bf_c_04",
        Domain.CONSCIENTIOUSNESS,
        -1,
        "Make a mess of things.",
        Provenance.IPIP,
        18,
        Facet.C_ORDERLINESS,
    ),
    Item(
        "bf_es_04",
        Domain.EMOTIONAL_STABILITY,
        +1,
        "Seldom feel blue.",
        Provenance.IPIP,
        19,
        Facet.ES_FREEDOM_FROM_WORRY,
    ),
    Item(
        "bf_o_04",
        Domain.OPENNESS,
        -1,
        "Am not interested in abstract ideas.",
        Provenance.IPIP,
        20,
        Facet.O_INTELLECT,
    ),
    Item(
        "bf_e_05",
        Domain.EXTRAVERSION,
        +1,
        "Start conversations.",
        Provenance.IPIP,
        21,
        Facet.E_ASSERTIVENESS,
    ),
    Item(
        "bf_a_05",
        Domain.AGREEABLENESS,
        -1,
        "Am not interested in other people's problems.",
        Provenance.IPIP,
        22,
        Facet.A_COMPASSION,
    ),
    Item(
        "bf_c_05",
        Domain.CONSCIENTIOUSNESS,
        +1,
        "Get chores done right away.",
        Provenance.IPIP,
        23,
        Facet.C_INDUSTRIOUSNESS,
    ),
    Item(
        "bf_es_05",
        Domain.EMOTIONAL_STABILITY,
        -1,
        "Am easily disturbed.",
        Provenance.IPIP,
        24,
        Facet.ES_COMPOSURE,
    ),
    Item(
        "bf_o_05",
        Domain.OPENNESS,
        +1,
        "Have excellent ideas.",
        Provenance.IPIP,
        25,
        Facet.O_IMAGINATION,
    ),
    Item(
        "bf_e_06",
        Domain.EXTRAVERSION,
        -1,
        "Have little to say.",
        Provenance.IPIP,
        26,
        Facet.E_ASSERTIVENESS,
    ),
    Item(
        "bf_a_06",
        Domain.AGREEABLENESS,
        +1,
        "Have a soft heart.",
        Provenance.IPIP,
        27,
        Facet.A_COMPASSION,
    ),
    Item(
        "bf_c_06",
        Domain.CONSCIENTIOUSNESS,
        -1,
        "Often forget to put things back in their proper place.",
        Provenance.IPIP,
        28,
        Facet.C_ORDERLINESS,
    ),
    Item(
        "bf_es_06",
        Domain.EMOTIONAL_STABILITY,
        -1,
        "Get upset easily.",
        Provenance.IPIP,
        29,
        Facet.ES_COMPOSURE,
    ),
    Item(
        "bf_o_06",
        Domain.OPENNESS,
        -1,
        "Do not have a good imagination.",
        Provenance.IPIP,
        30,
        Facet.O_IMAGINATION,
    ),
    Item(
        "bf_e_07",
        Domain.EXTRAVERSION,
        +1,
        "Talk to a lot of different people at parties.",
        Provenance.IPIP,
        31,
        Facet.E_ENTHUSIASM,
    ),
    Item(
        "bf_a_07",
        Domain.AGREEABLENESS,
        -1,
        "Am not really interested in others.",
        Provenance.IPIP,
        32,
        Facet.A_COMPASSION,
    ),
    Item(
        "bf_c_07",
        Domain.CONSCIENTIOUSNESS,
        +1,
        "Like order.",
        Provenance.IPIP,
        33,
        Facet.C_ORDERLINESS,
    ),
    Item(
        "bf_es_07",
        Domain.EMOTIONAL_STABILITY,
        -1,
        "Change my mood a lot.",
        Provenance.IPIP,
        34,
        Facet.ES_COMPOSURE,
    ),
    Item(
        "bf_o_07",
        Domain.OPENNESS,
        +1,
        "Am quick to understand things.",
        Provenance.IPIP,
        35,
        Facet.O_INTELLECT,
    ),
    Item(
        "bf_e_08",
        Domain.EXTRAVERSION,
        -1,
        "Don't like to draw attention to myself.",
        Provenance.IPIP,
        36,
        Facet.E_ASSERTIVENESS,
    ),
    Item(
        "bf_a_08",
        Domain.AGREEABLENESS,
        +1,
        "Take time out for others.",
        Provenance.IPIP,
        37,
        Facet.A_COMPASSION,
    ),
    Item(
        "bf_c_08",
        Domain.CONSCIENTIOUSNESS,
        -1,
        "Shirk my duties.",
        Provenance.IPIP,
        38,
        Facet.C_INDUSTRIOUSNESS,
    ),
    Item(
        "bf_es_08",
        Domain.EMOTIONAL_STABILITY,
        -1,
        "Have frequent mood swings.",
        Provenance.IPIP,
        39,
        Facet.ES_COMPOSURE,
    ),
    Item(
        "bf_o_08",
        Domain.OPENNESS,
        +1,
        "Use difficult words.",
        Provenance.IPIP,
        40,
        Facet.O_INTELLECT,
    ),
    Item(
        "bf_e_09",
        Domain.EXTRAVERSION,
        +1,
        "Don't mind being the center of attention.",
        Provenance.IPIP,
        41,
        Facet.E_ASSERTIVENESS,
    ),
    Item(
        "bf_a_09",
        Domain.AGREEABLENESS,
        +1,
        "Feel others' emotions.",
        Provenance.IPIP,
        42,
        Facet.A_COMPASSION,
    ),
    Item(
        "bf_c_09",
        Domain.CONSCIENTIOUSNESS,
        +1,
        "Follow a schedule.",
        Provenance.IPIP,
        43,
        Facet.C_ORDERLINESS,
    ),
    Item(
        "bf_es_09",
        Domain.EMOTIONAL_STABILITY,
        -1,
        "Get irritated easily.",
        Provenance.IPIP,
        44,
        Facet.ES_COMPOSURE,
    ),
    Item(
        "bf_o_09",
        Domain.OPENNESS,
        +1,
        "Spend time reflecting on things.",
        Provenance.IPIP,
        45,
        Facet.O_IMAGINATION,
    ),
    Item(
        "bf_e_10",
        Domain.EXTRAVERSION,
        -1,
        "Am quiet around strangers.",
        Provenance.IPIP,
        46,
        Facet.E_ENTHUSIASM,
    ),
    Item(
        "bf_a_10",
        Domain.AGREEABLENESS,
        +1,
        "Make people feel at ease.",
        Provenance.IPIP,
        47,
        Facet.A_POLITENESS,
    ),
    Item(
        "bf_c_10",
        Domain.CONSCIENTIOUSNESS,
        +1,
        "Am exacting in my work.",
        Provenance.IPIP,
        48,
        Facet.C_INDUSTRIOUSNESS,
    ),
    Item(
        "bf_es_10",
        Domain.EMOTIONAL_STABILITY,
        -1,
        "Often feel blue.",
        Provenance.IPIP,
        49,
        Facet.ES_FREEDOM_FROM_WORRY,
    ),
    Item(
        "bf_o_10",
        Domain.OPENNESS,
        +1,
        "Am full of ideas.",
        Provenance.IPIP,
        50,
        Facet.O_IMAGINATION,
    ),
)

ITEMS_BY_ID: Mapping[str, Item] = {item.item_id: item for item in ITEMS}

ITEMS_BY_DOMAIN: Mapping[Domain, tuple[Item, ...]] = {
    domain: tuple(item for item in ITEMS if item.domain is domain) for domain in Domain
}

ITEMS_BY_FACET: Mapping[Facet, tuple[Item, ...]] = {
    facet: tuple(item for item in ITEMS if item.facet is facet) for facet in Facet
}

# Bank-level invariants, asserted at import so a bad edit fails loudly at deploy
# rather than quietly at scoring time. 50 items, 10 per domain, unique ids.
assert len(ITEMS) == 50, "the IPIP-50 has fifty items"
assert len(ITEMS_BY_ID) == len(ITEMS), "duplicate item_id in the bank"
assert all(len(items) == 10 for items in ITEMS_BY_DOMAIN.values()), (
    "ten items per domain"
)
assert all(item.keyed in (+1, -1) for item in ITEMS), "keying must be +1 or -1"
assert {item.ipip_number for item in ITEMS} == set(range(1, 51)), (
    "IPIP numbering must be 1..50"
)


def keying_balance(domain: Domain | None = None) -> tuple[int, int]:
    """``(plus_keyed, minus_keyed)`` for one domain, or for the whole bank.

    Worth calling rather than trusting a comment. The counts read off the IPIP
    scoring formulas we verified against are E 5/5, A 6/4, C 6/4, Emotional
    Stability 2/8, Openness 7/3, and 26/24 over the bank; if this function
    disagrees, someone mistyped a sign.
    """
    items = ITEMS if domain is None else ITEMS_BY_DOMAIN[domain]
    plus = sum(1 for item in items if item.keyed > 0)
    return plus, len(items) - plus


# Domains whose keying is lopsided enough that an acquiescent (or contrarian)
# respondent shifts them for reasons unrelated to their personality. Emotional
# Stability at 2/8 is the severe case; nothing else in the bank is worse than 7/3.
# :func:`response_quality` names these when it raises an acquiescence flag.
MIN_BALANCED_KEYING_PER_SIDE = 3

ACQUIESCENCE_SENSITIVE_DOMAINS: frozenset[Domain] = frozenset(
    domain
    for domain in Domain
    if min(keying_balance(domain)) < MIN_BALANCED_KEYING_PER_SIDE
)

# The same domains, named for a second and independent reason: the keying-gap check
# compares the *mean* of a domain's plus-keyed items against the mean of its
# minus-keyed items, and a mean over two items is not a stable estimate of anything.
# Emotional Stability is keyed 2/8, so one misread positive item moves the plus mean
# by a full 2.0 scale points, while the same misread in Extraversion (5/5) moves it
# by 0.8 and in Openness (7/3) by 0.57. Applying one flat threshold across all five
# therefore does not apply one standard: it makes the 2/8 domain roughly two and a
# half times easier to fail, for reasons that live in Goldberg's item bank rather
# than in the respondent.
#
# This was not hypothetical. The first real sitting scored a 1.75 gap here and had
# Emotional Stability *and* the derived 16-type layer withheld off a single item
# ("Seldom feel blue", the one item in the bank that is both idiomatic and
# double-barrelled); dropping that item alone took the gap to 0.25. Every other
# domain in the same sitting was clean, and Extraversion was a perfect 0.00 across
# all ten items — the signature of someone reading carefully, refused by a
# threshold that could not tell 2 items from 5.
#
# So a gap in these domains is reported and hedged, never blocking. It is the same
# reasoning as :data:`ADVISORY_FLAGS`: the statistic is too weak to invalidate a
# sitting, so it asks a question instead.
KEYING_GAP_UNRELIABLE_DOMAINS: frozenset[Domain] = ACQUIESCENCE_SENSITIVE_DOMAINS


# ---------------------------------------------------------------------------
# Facet eligibility
# ---------------------------------------------------------------------------

# An aspect needs enough items to mean anything, and it needs to be a *part* of
# its domain rather than a rename of it. Three items is the floor at which a
# sub-score is worth printing; above 75% of the domain it is measuring the domain
# again under a second name, which invites a reader to treat one signal as two.
MIN_ITEMS_PER_FACET = 3
MAX_FACET_SHARE = 0.75


def _facet_is_reportable(facet: Facet) -> bool:
    items = ITEMS_BY_FACET[facet]
    if len(items) < MIN_ITEMS_PER_FACET:
        return False
    domain_size = len(ITEMS_BY_DOMAIN[items[0].domain])
    return len(items) / domain_size <= MAX_FACET_SHARE


# Computed once, at import, so the eligibility rule and the shipped facet list can
# never drift apart. Agreeableness contributes nothing here: A_COMPASSION holds 8
# of its 10 items (too much of the domain) and A_POLITENESS holds 2 (too few), so
# Agreeableness is reported as a domain score only. Documented in
# docs/smart_teams/instruments/big_five_and_type16.md.
REPORTABLE_FACETS: tuple[Facet, ...] = tuple(
    f for f in Facet if _facet_is_reportable(f)
)
UNREPORTABLE_FACETS: tuple[Facet, ...] = tuple(
    f for f in Facet if not _facet_is_reportable(f)
)

FACETS_BY_DOMAIN: Mapping[Domain, tuple[Facet, ...]] = {
    domain: tuple(
        facet
        for facet in REPORTABLE_FACETS
        if ITEMS_BY_FACET[facet][0].domain is domain
    )
    for domain in Domain
}


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------

BANDS_ARE_NOT_PERCENTILES = (
    "Band labels describe where a score falls between the floor and the ceiling of "
    "this instrument. They are NOT percentiles: the company has no norm sample of its own "
    "and no licence to anyone else's, so nothing here tells you how a person "
    "compares to a population. Two people's bands can be compared to each other "
    "because they answered the same items — that is all."
)


class Band(str, Enum):
    """Five positions on the scale. A label, deliberately not a verdict.

    The wording is chosen so a person can read their own band and say "no, that's
    not me" without having been graded. There is no good or bad end of any of
    these domains at work, and the phrasing must never imply one.
    """

    CLEARLY_LOWER = "clearly_lower"
    SOMEWHAT_LOWER = "somewhat_lower"
    MIDDLE = "middle"
    SOMEWHAT_HIGHER = "somewhat_higher"
    CLEARLY_HIGHER = "clearly_higher"


# Lower bound (inclusive) of each band on the 0–100 scale, highest first. Even
# fifths: with no norm sample there is nothing to justify uneven cutoffs, and
# pretending otherwise would be false precision.
BAND_CUTOFFS: tuple[tuple[float, Band], ...] = (
    (80.0, Band.CLEARLY_HIGHER),
    (60.0, Band.SOMEWHAT_HIGHER),
    (40.0, Band.MIDDLE),
    (20.0, Band.SOMEWHAT_LOWER),
    (0.0, Band.CLEARLY_LOWER),
)

BAND_LABELS: Mapping[Band, str] = {
    Band.CLEARLY_LOWER: "clearly toward the lower end",
    Band.SOMEWHAT_LOWER: "somewhat toward the lower end",
    Band.MIDDLE: "in the middle",
    Band.SOMEWHAT_HIGHER: "somewhat toward the higher end",
    Band.CLEARLY_HIGHER: "clearly toward the higher end",
}

# The middle band is the one most often over-read. A score of 50 does not mean
# "average person"; it means the items pulled in both directions, which for most
# work traits is a genuine finding — the person adapts — and not a missing answer.
BAND_CAVEATS: Mapping[Band, str] = {
    Band.MIDDLE: "Items pulled both ways here, which usually means it depends on the situation rather than that the answer is missing.",
}


def band_for(score: float) -> Band:
    """Band containing ``score`` (0–100). Cutoffs are inclusive at the lower edge."""
    for lower, band in BAND_CUTOFFS:
        if score >= lower:
            return band
    raise ValueError(f"score {score!r} is below the 0–100 scale")


def describe_domain(domain: Domain, band: Band) -> str:
    """One descriptive sentence for a guide: what the band says, hedged honestly."""
    sentence = f"{DOMAIN_LABELS[domain]} sits {BAND_LABELS[band]} of this scale."
    caveat = BAND_CAVEATS.get(band)
    return f"{sentence} {caveat}" if caveat else sentence


# ---------------------------------------------------------------------------
# Response quality
# ---------------------------------------------------------------------------
#
# These thresholds are the Smart Teams house rules for "did this person actually
# read the items". The DISC-style bank scores the *same sitting* as this one, so a
# careless interview must not come out flagged on one instrument and clean on the
# other — whichever answer a reviewer sees first would become the truth. The cutoffs
# are therefore published as :data:`SHARED_RESPONSE_QUALITY_THRESHOLDS` for other
# instrument modules to import, rather than as numbers a second module is asked to
# copy. Nothing here is a comment telling a human to keep two files in sync; if the
# numbers move, they move once.
#
# A flag is never a reason to discard someone's interview on their behalf. It
# feeds :attr:`ResponseQuality.is_confident`, which the profile builder surfaces as
# "this looks rushed — want to redo the scales?" and, via
# :attr:`ResponseQuality.blocking_flags`, suppresses derived layers (see
# :func:`rubik_core.smart_teams.instruments.type16.derive`) rather than deleting
# anything.

# Longest run of identical raw responses tolerated. The bank interleaves domains
# 1→5, so ten in a row spans two full cycles of five unrelated traits — possible
# for a real person, but not likely, and worth asking about.
MAX_IDENTICAL_RUN = 10

# Standard deviation across all raw responses. Someone who genuinely answered
# fifty mixed-keying self-descriptions moves around; a flat line usually means the
# mouse, not the person.
MIN_RESPONSE_SD = 0.40

# Share of answers parked on the neutral midpoint. Some neutrality is honest; a
# majority of it means the scale scores carry almost no information.
MAX_NEUTRAL_SHARE = 0.60

# Acquiescence index: mean raw response before reverse-scoring, minus the neutral
# midpoint, so 0.0 is balanced, positive is yes-saying, negative is nay-saying —
# but computed over keying-balanced item pairs only (:func:`_balanced_acquiescence`)
# so that the respondent's actual trait level cancels out instead of registering as
# a response style. ±0.75 (three quarters of a scale point) is where the imbalance
# in Emotional Stability starts to move that domain on its own.
MAX_ACQUIESCENCE = 0.75

# Within a domain, the plus-keyed and minus-keyed items should tell the same story
# once reverse-scored. A large gap means the respondent agreed with both "Don't
# talk a lot" and "Start conversations" — random or inattentive responding, and
# the one check that catches a bot or a rushed skim that the SD check misses.
# Measured in scale points (1–5), so 1.5 is a substantial contradiction.
#
# Compared against both keying halves of a domain, which means it is only as
# trustworthy as the smaller half is large — see
# :data:`KEYING_GAP_UNRELIABLE_DOMAINS`, where exceeding it raises an advisory flag
# rather than a blocking one.
MAX_KEYING_GAP = 1.5

# Minimum answered items per domain before we will publish a number for it. Below
# this the score is arithmetic on too little.
MIN_ANSWERED_PER_DOMAIN = 7


@dataclass(frozen=True, slots=True)
class ResponseQualityThresholds:
    """The carelessness cutoffs one sitting is judged by, in one importable object.

    Exists so the cross-instrument contract is a dependency instead of a comment.
    Every module that scores part of the same Work Style Interview sitting reads its
    cutoffs from :data:`SHARED_RESPONSE_QUALITY_THRESHOLDS`; a second module that
    hardcoded its own copy would eventually disagree with this one, and a person
    would be told their answers looked rushed on one page and fine on the next.

    Deliberately excluded: :data:`MIN_ANSWERED_PER_DOMAIN`, which is a fact about
    this bank being ten items per domain rather than a house rule, and every
    index that only exists in one bank (DISC's forced-choice position share has no
    counterpart here, and this bank's per-domain keying gap has none there). Sharing
    a threshold only makes sense where both banks compute the same statistic.
    """

    max_identical_run: int
    min_response_sd: float
    max_neutral_share: float
    max_acquiescence: float
    max_keying_gap: float


SHARED_RESPONSE_QUALITY_THRESHOLDS = ResponseQualityThresholds(
    max_identical_run=MAX_IDENTICAL_RUN,
    min_response_sd=MIN_RESPONSE_SD,
    max_neutral_share=MAX_NEUTRAL_SHARE,
    max_acquiescence=MAX_ACQUIESCENCE,
    max_keying_gap=MAX_KEYING_GAP,
)


class QualityFlag(str, Enum):
    """What looked off about a set of responses. Advisory, never destructive."""

    STRAIGHT_LINING = "straight_lining"
    LOW_VARIANCE = "low_variance"
    NEUTRAL_OVERUSE = "neutral_overuse"
    ACQUIESCENCE = "acquiescence"
    NAY_SAYING = "nay_saying"
    KEYING_INCONSISTENCY = "keying_inconsistency"
    KEYING_INCONSISTENCY_IMBALANCED = "keying_inconsistency_imbalanced"
    INCOMPLETE = "incomplete"


QUALITY_FLAG_NOTES: Mapping[QualityFlag, str] = {
    QualityFlag.STRAIGHT_LINING: "A long run of identical answers — the scales may have been clicked through rather than read.",
    QualityFlag.LOW_VARIANCE: "Almost every answer was the same value, so the scores carry very little information.",
    QualityFlag.NEUTRAL_OVERUSE: "Most answers sat on the neutral midpoint, which leaves the scores close to the middle by construction.",
    QualityFlag.ACQUIESCENCE: "A consistent lean toward agreeing, measured only on items that come in matched positive/negative pairs so that a real trait score is not mistaken for a habit of agreeing. Advisory: Emotional Stability is keyed 2 positive / 8 negative, so if this is a response style it pushes that domain downward on its own — worth asking about, not grounds for discarding the answers.",
    QualityFlag.NAY_SAYING: "A consistent lean toward disagreeing, measured only on items that come in matched positive/negative pairs so that a real trait score is not mistaken for a habit of disagreeing. Advisory: Emotional Stability is keyed 2 positive / 8 negative, so if this is a response style it pushes that domain upward on its own — worth asking about, not grounds for discarding the answers.",
    QualityFlag.KEYING_INCONSISTENCY: "Positively and negatively worded items in the same domain disagreed with each other, which usually means some were read quickly.",
    QualityFlag.KEYING_INCONSISTENCY_IMBALANCED: "Positively and negatively worded items disagreed in a domain that has too few items on one side for the comparison to be reliable — Emotional Stability is keyed 2 positive / 8 negative, so a single misread positive item produces this on its own. Advisory: worth re-reading those items, not grounds for withholding the domain.",
    QualityFlag.INCOMPLETE: "Not enough items were answered in at least one domain to score it.",
}


# Flags that describe a *possible* response style rather than evidence of not
# reading. :func:`_balanced_acquiescence` removes the trait confound from the index
# these two are raised on, but no within-person statistic can remove it entirely: a
# person who genuinely agrees with more self-descriptions than they reject is
# indistinguishable, from their answers alone, from a person who tends to agree.
# So they surface as a question and they do not gate anything — see
# :attr:`ResponseQuality.blocking_flags`.
ADVISORY_FLAGS: frozenset[QualityFlag] = frozenset(
    {
        QualityFlag.ACQUIESCENCE,
        QualityFlag.NAY_SAYING,
        # A keying gap confined to a domain whose smaller keying side is under
        # :data:`MIN_BALANCED_KEYING_PER_SIDE` items. Same argument as the two
        # above: the statistic cannot carry the weight of withholding a domain.
        QualityFlag.KEYING_INCONSISTENCY_IMBALANCED,
    }
)


@dataclass(frozen=True, slots=True)
class ResponseQuality:
    """Whether a set of responses looks like someone reading, with the evidence.

    Every threshold comparison keeps its measured value so a reviewer can see how
    close to the line a session was, instead of being handed a bare boolean.
    """

    answered: int
    expected: int
    longest_identical_run: int
    response_sd: float
    neutral_share: float
    acquiescence_index: float
    acquiescence_items: int
    raw_mean_drift: float
    worst_keying_gap: float
    worst_keying_gap_domain: Domain | None
    flags: frozenset[QualityFlag]

    @property
    def is_confident(self) -> bool:
        """True when no check tripped at all — the strictest available read."""
        return not self.flags

    @property
    def blocking_flags(self) -> frozenset[QualityFlag]:
        """Flags that should stop a derived layer being published.

        Everything except :data:`ADVISORY_FLAGS`. A lean toward agreeing is a thing
        to ask the person about; it is not a reason to withhold their letters, and
        treating it as one would silently penalise anyone whose true scores are high
        on the domains this bank words positively.
        """
        return frozenset(self.flags - ADVISORY_FLAGS)

    @property
    def has_blocking_flags(self) -> bool:
        return bool(self.blocking_flags)

    @property
    def notes(self) -> tuple[str, ...]:
        """Flag explanations in a stable order, ready for the review screen."""
        return tuple(QUALITY_FLAG_NOTES[f] for f in QualityFlag if f in self.flags)

    def as_dict(self) -> dict:
        return {
            "answered": self.answered,
            "expected": self.expected,
            "longest_identical_run": self.longest_identical_run,
            "response_sd": round(self.response_sd, 3),
            "neutral_share": round(self.neutral_share, 3),
            "acquiescence_index": round(self.acquiescence_index, 3),
            "acquiescence_items": self.acquiescence_items,
            "raw_mean_drift": round(self.raw_mean_drift, 3),
            "worst_keying_gap": round(self.worst_keying_gap, 3),
            "worst_keying_gap_domain": (
                self.worst_keying_gap_domain.value
                if self.worst_keying_gap_domain
                else None
            ),
            "flags": sorted(f.value for f in self.flags),
            "advisory_flags": sorted(f.value for f in self.flags & ADVISORY_FLAGS),
            "blocking_flags": sorted(f.value for f in self.blocking_flags),
            "is_confident": self.is_confident,
        }


def _longest_identical_run(values: Sequence[int]) -> int:
    longest = run = 0
    previous: int | None = None
    for value in values:
        run = run + 1 if value == previous else 1
        previous = value
        longest = max(longest, run)
    return longest


def _keying_gap(items: Iterable[Item], responses: Mapping[str, int]) -> float | None:
    """How far the plus- and minus-keyed halves of a domain disagree, in scale points.

    Both halves are reverse-scored first, so agreement means the two means land in
    the same place. Returns ``None`` when either half went unanswered — that is an
    incompleteness problem, not an inconsistency one.
    """
    plus: list[int] = []
    minus: list[int] = []
    for item in items:
        response = responses.get(item.item_id)
        if response is None:
            continue
        (plus if item.keyed > 0 else minus).append(item_score(item, response))
    if not plus or not minus:
        return None
    return abs(statistics.fmean(plus) - statistics.fmean(minus))


def _validated(responses: Mapping[str, int]) -> dict[str, int]:
    """Reject unknown ids and out-of-scale values loudly.

    A typo'd item id that silently scored as "unanswered" would quietly shift a
    domain and nobody would ever notice, so this is deliberately strict — the same
    call ``disc.py``, ``motivators.py`` and ``enneagram.py`` make. All four
    instrument modules in this package now agree on it; a caller rescoring answers
    stored against an older bank needs an explicit migration rather than a silent
    partial score, which is the whole point.

    ``None`` is tolerated and dropped: the interview is resumable, so an unanswered
    item legitimately arrives as a null and is not the same thing as a bad id.
    """
    clean: dict[str, int] = {}
    for item_id, value in responses.items():
        if item_id not in ITEMS_BY_ID:
            raise ValueError(f"unknown Big Five item id: {item_id!r}")
        if value is None:
            continue
        if not RESPONSE_MIN <= value <= RESPONSE_MAX:
            raise ValueError(
                f"{item_id}: response {value!r} is outside the "
                f"{RESPONSE_MIN}–{RESPONSE_MAX} scale"
            )
        clean[item_id] = value
    return clean


def _balanced_acquiescence(responses: Mapping[str, int]) -> tuple[float, int]:
    """``(index, items_used)`` — yes-saying measured with the trait divided out.

    The naive index (mean raw response minus the midpoint over the whole bank) has
    a defect that matters precisely on the domain the flag warns about. Emotional
    Stability is keyed 2 plus / 8 minus, so a respondent who genuinely resets
    slowly under pressure agrees with eight of its ten items *because that is true
    of them*, lifting the bank-wide raw mean and looking like a yes-sayer. The flag
    then blames a response style for the very trait score it was supposed to
    protect, and the people most likely to be told their answers look unreliable
    are the ones lowest on stability.

    So the index is computed over keying-balanced *pairs* instead. Within one
    domain, take ``k = min(plus_answered, minus_answered)`` items from each side:
    across those 2k items a real trait pushes the plus half up and the minus half
    down by the same amount, and the two cancel, leaving only the part of the
    answers that leans one way regardless of wording. Items are taken in bank order
    so the subset is deterministic for a given set of answers.

    Emotional Stability contributes 4 items rather than 10 under this rule, which is
    the honest trade: fewer items, but a number that means what the flag says it
    means. Returns ``(0.0, 0)`` when nothing balanced was answered, which reads as
    "no evidence of a lean" — the flag is never raised on an empty subset.

    Measured, not asserted: sweeping all 5^5 = 3,125 perfectly trait-consistent
    sittings (every plus item answered ``v`` and every minus item ``6 - v``, per
    domain — zero response style by construction) the naive whole-bank index climbs
    to +0.56, three quarters of the way to :data:`MAX_ACQUIESCENCE`, and it peaks on
    exactly the profile that is *low* on Emotional Stability. This function returns
    0.0 for all 3,125 of them. The sweep is a dozen lines and is written out in
    docs/smart_teams/instruments/big_five_and_type16.md; it is not in the test
    suite yet, because this module has no test file yet.
    """
    used: list[int] = []
    for items in ITEMS_BY_DOMAIN.values():
        plus = [i.item_id for i in items if i.keyed > 0 and i.item_id in responses]
        minus = [i.item_id for i in items if i.keyed < 0 and i.item_id in responses]
        pairs = min(len(plus), len(minus))
        if not pairs:
            continue
        used.extend(responses[i] for i in plus[:pairs])
        used.extend(responses[i] for i in minus[:pairs])
    if not used:
        return 0.0, 0
    return statistics.fmean(used) - RESPONSE_NEUTRAL, len(used)


def response_quality(responses: Mapping[str, int]) -> ResponseQuality:
    """Run the house checks over one sitting's raw responses.

    ``responses`` maps :attr:`Item.item_id` to a raw 1–5 value, exactly as stored
    on ``st_interviews``. Unknown ids and out-of-range values raise — see
    :func:`_validated`.
    """
    known = _validated(responses)

    # Presentation order matters for the run check: a run is only meaningful over
    # the sequence the person actually saw, which is the bank's own order.
    ordered = [known[i.item_id] for i in ITEMS if i.item_id in known]
    flags: set[QualityFlag] = set()

    longest_run = _longest_identical_run(ordered)
    if longest_run >= MAX_IDENTICAL_RUN:
        flags.add(QualityFlag.STRAIGHT_LINING)

    sd = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
    if ordered and sd < MIN_RESPONSE_SD:
        flags.add(QualityFlag.LOW_VARIANCE)

    neutral_share = (
        sum(1 for v in ordered if v == RESPONSE_NEUTRAL) / len(ordered)
        if ordered
        else 0.0
    )
    if neutral_share > MAX_NEUTRAL_SHARE:
        flags.add(QualityFlag.NEUTRAL_OVERUSE)

    acquiescence, acquiescence_items = _balanced_acquiescence(known)
    if acquiescence > MAX_ACQUIESCENCE:
        flags.add(QualityFlag.ACQUIESCENCE)
    elif acquiescence < -MAX_ACQUIESCENCE:
        flags.add(QualityFlag.NAY_SAYING)

    # Reported, never flagged on. This is the whole-bank raw mean drift — the naive
    # index — kept beside the balanced one so a reviewer can see when the two
    # disagree. A wide gap between them is the signature of a real trait score, not
    # a response style, and that is exactly the case the flag must not fire on.
    raw_mean_drift = statistics.fmean(ordered) - RESPONSE_NEUTRAL if ordered else 0.0

    # Reported as the worst gap anywhere, so the number stays honest — but which
    # flag it raises depends on whether the domain it came from has enough items on
    # both keying sides for the comparison to mean anything. See
    # :data:`KEYING_GAP_UNRELIABLE_DOMAINS`.
    worst_gap = 0.0
    worst_gap_domain: Domain | None = None
    worst_reliable_gap = 0.0
    worst_unreliable_gap = 0.0
    for domain, items in ITEMS_BY_DOMAIN.items():
        gap = _keying_gap(items, known)
        if gap is None:
            continue
        if gap > worst_gap:
            worst_gap, worst_gap_domain = gap, domain
        if domain in KEYING_GAP_UNRELIABLE_DOMAINS:
            worst_unreliable_gap = max(worst_unreliable_gap, gap)
        else:
            worst_reliable_gap = max(worst_reliable_gap, gap)
    if worst_reliable_gap > MAX_KEYING_GAP:
        flags.add(QualityFlag.KEYING_INCONSISTENCY)
    if worst_unreliable_gap > MAX_KEYING_GAP:
        flags.add(QualityFlag.KEYING_INCONSISTENCY_IMBALANCED)

    for items in ITEMS_BY_DOMAIN.values():
        if sum(1 for i in items if i.item_id in known) < MIN_ANSWERED_PER_DOMAIN:
            flags.add(QualityFlag.INCOMPLETE)
            break

    return ResponseQuality(
        answered=len(known),
        expected=len(ITEMS),
        longest_identical_run=longest_run,
        response_sd=sd,
        neutral_share=neutral_share,
        acquiescence_index=acquiescence,
        acquiescence_items=acquiescence_items,
        raw_mean_drift=raw_mean_drift,
        worst_keying_gap=worst_gap,
        worst_keying_gap_domain=worst_gap_domain,
        flags=frozenset(flags),
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def item_score(item: Item, response: int) -> int:
    """Reverse-score if needed, per IPIP's instructions.

    IPIP: "For - keyed items, the response 'Very Inaccurate' is assigned a value of
    5 … and 'Very Accurate' a value of 1." That is the reflection below, written as
    ``min + max - response`` so it stays correct if the scale ever widens.
    """
    if not RESPONSE_MIN <= response <= RESPONSE_MAX:
        raise ValueError(
            f"response {response!r} for {item.item_id} is outside the {RESPONSE_MIN}–{RESPONSE_MAX} scale"
        )
    if item.keyed > 0:
        return response
    return RESPONSE_MIN + RESPONSE_MAX - response


def _scale_to_100(total: int, count: int) -> float:
    """Map a keyed sum onto 0–100 given how many items contributed.

    IPIP scores by summing, which makes a raw total meaningless without knowing
    the item count — and ours varies, because a domain can be scored on 7 of 10
    answered items. Normalising against the floor (``count × RESPONSE_MIN``) and
    the span keeps a partially answered domain on the same scale as a full one.
    """
    floor = count * RESPONSE_MIN
    span = count * (RESPONSE_MAX - RESPONSE_MIN)
    return (total - floor) / span * 100.0


@dataclass(frozen=True, slots=True)
class FacetScore:
    """A content-clustered sub-score. Weaker evidence than its domain — see :class:`Facet`."""

    facet: Facet
    score: float
    band: Band
    items_answered: int
    items_total: int
    keying_balanced: bool

    @property
    def label(self) -> str:
        return FACET_LABELS[self.facet]

    def as_dict(self) -> dict:
        return {
            "facet": self.facet.value,
            "label": self.label,
            "score": round(self.score, 1),
            "band": self.band.value,
            "items_answered": self.items_answered,
            "items_total": self.items_total,
            "keying_balanced": self.keying_balanced,
        }


@dataclass(frozen=True, slots=True)
class DomainScore:
    """One domain on 0–100, with the arithmetic behind it kept visible."""

    domain: Domain
    score: float
    band: Band
    raw_total: int
    items_answered: int
    items_total: int
    facets: tuple[FacetScore, ...]

    @property
    def label(self) -> str:
        return DOMAIN_LABELS[self.domain]

    @property
    def description(self) -> str:
        return describe_domain(self.domain, self.band)

    def as_dict(self) -> dict:
        return {
            "domain": self.domain.value,
            "label": self.label,
            "score": round(self.score, 1),
            "band": self.band.value,
            "description": self.description,
            "raw_total": self.raw_total,
            "items_answered": self.items_answered,
            "items_total": self.items_total,
            "facets": [f.as_dict() for f in self.facets],
        }


@dataclass(frozen=True, slots=True)
class BigFiveResult:
    """Scored domains plus the quality read on the sitting that produced them.

    ``domains`` omits any domain that fell below :data:`MIN_ANSWERED_PER_DOMAIN`;
    :attr:`unscored_domains` names them. Callers must handle a missing domain
    rather than defaulting it to 50 — a midpoint stands in for "balanced", and
    "we don't know" is a different statement.
    """

    domains: Mapping[Domain, DomainScore]
    quality: ResponseQuality

    @property
    def unscored_domains(self) -> tuple[Domain, ...]:
        return tuple(d for d in Domain if d not in self.domains)

    @property
    def is_complete(self) -> bool:
        return not self.unscored_domains

    @property
    def neuroticism_equivalent(self) -> float | None:
        """Emotional Stability flipped into the conventional Neuroticism direction.

        For comparison against published Big Five research only. Do not put this
        number, or the word "neuroticism", in front of the person or their
        colleagues; the work-facing label is Emotional Stability.
        """
        scored = self.domains.get(Domain.EMOTIONAL_STABILITY)
        return None if scored is None else 100.0 - scored.score

    def score_of(self, domain: Domain) -> float | None:
        scored = self.domains.get(domain)
        return None if scored is None else scored.score

    def as_dict(self) -> dict:
        return {
            "instrument": "ipip_50",
            "domains": {d.value: s.as_dict() for d, s in self.domains.items()},
            "unscored_domains": [d.value for d in self.unscored_domains],
            "quality": self.quality.as_dict(),
            "bands_note": BANDS_ARE_NOT_PERCENTILES,
        }


def _score_facet(facet: Facet, responses: Mapping[str, int]) -> FacetScore | None:
    items = ITEMS_BY_FACET[facet]
    scored = [
        (item, responses[item.item_id]) for item in items if item.item_id in responses
    ]
    if len(scored) < MIN_ITEMS_PER_FACET:
        return None
    total = sum(item_score(item, response) for item, response in scored)
    score = _scale_to_100(total, len(scored))
    keyed = {item.keyed for item, _ in scored}
    return FacetScore(
        facet=facet,
        score=score,
        band=band_for(score),
        items_answered=len(scored),
        items_total=len(items),
        # An all-one-direction facet (ES_COMPOSURE is 0 plus / 5 minus) moves with
        # acquiescence on its own, so the flag travels with the number.
        keying_balanced=len(keyed) > 1,
    )


def score(responses: Mapping[str, int]) -> BigFiveResult:
    """Score one sitting: raw responses in, domains and facets out.

    ``responses`` maps :attr:`Item.item_id` to a raw 1–5 value. Unknown ids and
    out-of-range values raise (:func:`_validated`) — an id this bank does not know
    is a caller bug or an un-migrated stored session, and scoring the remainder
    would publish a domain quietly computed from fewer items than it claims.

    Deterministic and side-effect free — same responses, same numbers, forever.
    That matters for the retake flow: a profile version has to keep meaning the
    same thing after we ship changes elsewhere in Smart Teams.
    """
    known = _validated(responses)

    scored_domains: dict[Domain, DomainScore] = {}
    for domain, items in ITEMS_BY_DOMAIN.items():
        answered = [(i, known[i.item_id]) for i in items if i.item_id in known]
        if len(answered) < MIN_ANSWERED_PER_DOMAIN:
            logger.info(
                "big_five.score leaving %s unscored: %d of %d items answered (need %d)",
                domain.value,
                len(answered),
                len(items),
                MIN_ANSWERED_PER_DOMAIN,
            )
            continue
        total = sum(item_score(item, response) for item, response in answered)
        domain_score = _scale_to_100(total, len(answered))
        facets = tuple(
            facet_score
            for facet in FACETS_BY_DOMAIN[domain]
            if (facet_score := _score_facet(facet, known)) is not None
        )
        scored_domains[domain] = DomainScore(
            domain=domain,
            score=domain_score,
            band=band_for(domain_score),
            raw_total=total,
            items_answered=len(answered),
            items_total=len(items),
            facets=facets,
        )

    return BigFiveResult(domains=scored_domains, quality=response_quality(known))
