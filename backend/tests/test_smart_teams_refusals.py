"""What a Smart Teams layer says when it has decided it does not know.

This file exists because of one defect class, found twice in the same review and
both times in code that passed its own tests: a layer that has refused to state
a finding still emits prose that reads as certain. That is the worst thing this
feature can do, because the output is a document about a person that the person
is then asked to approve. A wrong number in an admin table is a bug; a confident
sentence about somebody's character built on a coin flip is something they carry
around.

The two originals, both reproduced here before they were fixed:

* ``type16`` asserted a full work-style portrait off a coin-flip axis.
  ``WORK_STYLE`` is keyed on all four letters at once, and ``derive()`` says in
  as many words that the tie-break direction on a midpoint score carries no
  meaning. An extraversion score of 49.9 selected the INFJ paragraph and 50.1
  selected the ENFJ paragraph — two substantively different descriptions of the
  same person, a fifth of a point apart. The middle band is 40-60, a fifth of
  the scale, so this was the ordinary case.
* ``contracts._motivators_verdict`` closed the gate on a part-answered
  motivators section and then set the hedge to ``headline()``, which names a top
  two. With nine of thirty items answered the subject read "Driven most by
  Understanding and Standing and Autonomy … Guiding Framework moves them least";
  with *one* item answered it still named two; with none it read "their answers
  weigh all six about equally", a claim about answers that do not exist.

So the assertions here are on **the actual strings a person would read**, not on
flags. A flag being right is not the property under test — the property is that
the sentence rendered beside it does not overstate it. ``LayerCard`` in
``frontend/src/components/smartTeams/shared.tsx`` renders ``layer.hedge`` as the
*only* body text when ``reportable`` is false, which is why a hedge is held to a
higher standard than the prose behind an open gate: it is read precisely because
the gate is shut.

Two of the strings pinned below are pinned as they *are*, with the residual
named in the test rather than quietly accepted — see
``test_motivators_probe_pending_hedges_still_name_the_ranking``. Pinning a
string does not endorse it; it stops it moving without somebody noticing.

Nothing here touches an LLM or the network. Every function under test is pure
arithmetic on literal answers, and ``_no_network`` proves it rather than
asserting it in a comment.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from rubik_core.smart_teams import contracts as ct
from rubik_core.smart_teams.instruments import big_five as bf
from rubik_core.smart_teams.instruments import disc, type16
from rubik_core.smart_teams.instruments import enneagram as enn
from rubik_core.smart_teams.instruments import motivators as mot
from rubik_core.smart_teams.instruments import strengths as st

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every scoring path in this file is pure. Make that load-bearing.

    The instruments do their own arithmetic and the only LLM in Smart Teams
    writes prose *from* their output, so a test in here that reaches the network
    means a call moved into a scoring path — which is exactly the change that
    would make a hedge non-deterministic. Fail loudly instead.
    """

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "a Smart Teams scoring path opened a socket; refusal wording must "
            "be deterministic and offline"
        )

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)


# ── Big Five fixtures, hand-built so a single score can be placed exactly ─────
# ``bf.score()`` cannot produce an extraversion of exactly 50.0 on demand, and
# the whole point of the type16 finding is what happens at exactly 50.0 and a
# tenth either side of it. So the DomainScores are constructed directly, with a
# quality read that raises no flag at all — the defect must be shown with
# nothing else wrong with the sitting.

_CLEAN_QUALITY = bf.ResponseQuality(
    answered=50,
    expected=50,
    longest_identical_run=2,
    response_sd=1.2,
    neutral_share=0.1,
    acquiescence_index=0.0,
    acquiescence_items=20,
    raw_mean_drift=0.0,
    worst_keying_gap=0.2,
    worst_keying_gap_domain=None,
    flags=frozenset(),
)


def _domain(domain: bf.Domain, score: float) -> bf.DomainScore:
    return bf.DomainScore(
        domain=domain,
        score=score,
        band=bf.band_for(score),
        raw_total=0,
        items_answered=10,
        items_total=10,
        facets=(),
    )


def _big_five(scores: dict[bf.Domain, float]) -> bf.BigFiveResult:
    return bf.BigFiveResult(
        domains={d: _domain(d, s) for d, s in scores.items()},
        quality=_CLEAN_QUALITY,
    )


def _clear_on_every_domain(**overrides: float) -> dict[bf.Domain, float]:
    """Every domain at 87.5 — clearly-higher band, no coin flip anywhere."""
    scores = {domain: 87.5 for domain in bf.Domain}
    for name, value in overrides.items():
        scores[bf.Domain[name.upper()]] = value
    return scores


ITEM_IDS = tuple(item.item_id for item in mot.LIKERT_ITEMS)
MOTIVATOR_LABELS = tuple(mot.MOTIVATOR_NAMES.values())


def _names_a_motivator(text: str) -> list[str]:
    return [label for label in MOTIVATOR_LABELS if label in text]


# ── type16: a coin-flip axis yields no portrait ───────────────────────────────


def test_a_coin_flip_axis_yields_no_portrait() -> None:
    """The original repro, as a regression test.

    49.9 and 50.1 are the same person to any honest reading of a Big Five
    score, and they used to produce two different paragraphs about them. Both
    now produce none. The control at 87.5 still produces one, because the fix
    had to be a withholding and not a deletion.
    """
    below = type16.derive(_big_five(_clear_on_every_domain(extraversion=49.9)))
    above = type16.derive(_big_five(_clear_on_every_domain(extraversion=50.1)))
    exact = type16.derive(_big_five(_clear_on_every_domain(extraversion=50.0)))

    # The letters really do differ. That is the point: the letter is a coin flip.
    assert below.letters == "INFJ"
    assert above.letters == "ENFJ"
    assert exact.letters == "INFJ"

    for result in (below, above, exact):
        assert result.coin_flip_axes == (type16.Axis.EI,)
        assert result.is_all_coin_flips is False
        assert result.work_style is None, (
            "a portrait keyed on a meaningless letter is a portrait chosen by "
            "the tie-break direction derive() says carries no meaning"
        )

    control = type16.derive(_big_five(_clear_on_every_domain()))
    assert control.coin_flip_axes == ()
    assert control.work_style == type16.WORK_STYLE["ENFJ"]


def test_two_neighbouring_scores_no_longer_swap_the_description() -> None:
    """The specific harm: not "a portrait", but *different* portraits.

    Asserted as an equality between the two sides of the midpoint, because that
    is the sentence a reviewer would have to compare by hand otherwise.
    """
    below = type16.derive(_big_five(_clear_on_every_domain(extraversion=49.9)))
    above = type16.derive(_big_five(_clear_on_every_domain(extraversion=50.1)))
    assert below.work_style == above.work_style is None
    assert below.describe() != above.describe()  # the letters still differ
    for result in (below, above):
        # No paragraph from the table survives anywhere in the prose.
        assert not [
            code
            for code, paragraph in type16.WORK_STYLE.items()
            if paragraph in result.describe()
        ]


def test_the_coin_flip_description_says_why_and_names_the_axis() -> None:
    """Withholding has to be legible, or it reads as a rendering failure."""
    result = type16.derive(_big_five(_clear_on_every_domain(extraversion=50.0)))
    description = result.describe()

    assert type16.WORK_STYLE_WITHHELD_NOTE in description
    # The axis and the score behind it, so the person can go read the real one.
    assert "E/I sat in the middle band (50 on outward energy)" in description
    assert type16.DOMAIN_WITHOUT_A_LETTER_NOTE in description
    # And the code is still qualified — the derived-from guard is untouched.
    assert type16.find_unqualified_codes(description) == ()
    assert "(derived from Big Five" in description


def test_no_caller_path_reaches_a_coin_flip_portrait() -> None:
    """Through the contract, which is how the guide generator gets here.

    ``layer_of`` is the only sanctioned way into a stored layer, and a
    one-coin-flip type16 layer is deliberately still *reportable* — three of its
    four letters mean something. So this is the path that mattered: gate open,
    portrait absent.
    """
    result = type16.derive(_big_five(_clear_on_every_domain(extraversion=50.0)))
    envelope = ct.envelope_for("type16", result)
    verdict = ct.read_verdict(envelope)

    assert verdict.reportable is True
    assert verdict.confidence == "low"
    assert verdict.reasons == (ct.REASON_COIN_FLIP,)

    layer = ct.layer_of(envelope)
    assert layer is not None
    assert layer["work_style"] is None
    assert type16.WORK_STYLE_WITHHELD_NOTE in layer["description"]
    for paragraph in type16.WORK_STYLE.values():
        assert paragraph not in layer["description"]


def test_nothing_outside_type16_looks_up_the_work_style_table() -> None:
    """The structural half of the fix.

    ``Type16Result.work_style`` decides whether the four letters are safe to key
    a portrait on. A subscript of ``WORK_STYLE`` anywhere else skips that
    decision, which is precisely the bug — so the guard is a repo grep rather
    than a comment. ``.get(`` is included because that is the spelling the
    broken version used.

    ``--untracked`` is not optional. The whole ST-3/ST-4 build is untracked
    working-tree files right now, and a default ``git grep`` would have searched
    none of them — a guard that passes because it looked nowhere.
    """
    hits = subprocess.run(
        [
            "git",
            "grep",
            "-n",
            "--untracked",
            "-E",
            r"WORK_STYLE(\[|\.get\()",
            "--",
            "*.py",
            "*.ts",
            "*.tsx",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()

    # The grep must still find the sanctioned accessor, or the pattern has
    # drifted and this test is passing by looking for nothing.
    assert any(
        line.startswith("rubik_core/smart_teams/instruments/type16.py:")
        and "return WORK_STYLE[self.letters]" in line
        for line in hits
    ), f"the guard pattern no longer matches the one sanctioned lookup: {hits}"

    offenders = [
        line
        for line in hits
        if not line.startswith("rubik_core/smart_teams/instruments/type16.py:")
        and not line.startswith("backend/tests/test_smart_teams_refusals.py:")
        # disc.WORK_STYLES is a different table with a different guard.
        and "WORK_STYLES" not in line
    ]
    assert offenders == [], (
        "these reach the sixteen work-style paragraphs without going through "
        f"Type16Result.work_style: {offenders}"
    )


# ── Motivators: the hedge may not name what the gate closed on ────────────────


@pytest.mark.parametrize(
    ("answered_count", "expected_share"),
    [(0, "0%"), (1, "3%"), (9, "30%")],
)
def test_an_incomplete_motivators_hedge_names_no_orientation(
    answered_count: int, expected_share: str
) -> None:
    """Three answer counts, because all three produced a different wrong claim.

    Nine answered named two orientations and a bottom. One answered named two,
    one of which had *zero* answers behind it. Zero answered asserted that
    "their answers weigh all six about equally" — the flat-profile sentence,
    reached because six axes defaulted to 50.0 are trivially level.
    """
    result = mot.score_motivators({item: 5 for item in ITEM_IDS[:answered_count]})
    assert result.probe_reason == "incomplete"

    verdict = ct.verdict_for("motivators", result)
    assert verdict.reportable is False
    assert verdict.reasons == (ct.REASON_INSUFFICIENT_ANSWERS,)
    assert verdict.hedge == (
        f"Only {answered_count} of 30 items answered ({expected_share}); not "
        "enough to rank what drives them. Nothing else on the profile is "
        "affected, and this section can be finished at any time."
    )
    assert _names_a_motivator(verdict.hedge) == []
    # And it does not smuggle the claim back in as a positional word.
    assert "Driven most by" not in verdict.hedge
    assert "moves them least" not in verdict.hedge


def test_the_incomplete_hedge_matches_the_two_layers_that_got_it_right() -> None:
    """DISC and the Enneagram already refused this way. Three, one product.

    Not a style point. The three sentences are the same claim in the same slot,
    and a person completing two sections and abandoning a third sees more than
    one of them.
    """
    motivators_hedge = ct.verdict_for(
        "motivators", mot.score_motivators({ITEM_IDS[0]: 5})
    ).hedge
    disc_hedge = ct.verdict_for("disc", disc.score_disc({}, {})).hedge
    enneagram_hedge = ct.verdict_for(
        "enneagram",
        enn.score_enneagram(
            {item.item_id: 4 for item in enn.LIKERT_ITEMS[:3]},
        ),
    ).hedge

    assert (
        disc_hedge == "Only 0 of 48 items answered (0%); not enough to place a style."
    )
    assert enneagram_hedge is not None
    assert enneagram_hedge.startswith(
        "Not enough answers in this section to suggest a type."
    )
    assert motivators_hedge is not None
    assert motivators_hedge.startswith("Only 1 of 30 items answered (3%);")
    for hedge in (motivators_hedge, disc_hedge, enneagram_hedge):
        assert "not enough" in hedge.lower()


def test_a_level_motivators_hedge_names_no_orientation() -> None:
    """The all-midpoint respondent. Already correct — pinned so it stays."""
    result = mot.score_motivators({item: 3 for item in ITEM_IDS})
    assert result.probe_reason == "level_profile"

    verdict = ct.verdict_for("motivators", result)
    assert verdict.reportable is False
    assert verdict.reasons == (ct.REASON_FLAT_PROFILE,)
    assert verdict.hedge == (
        "No orientation clearly leads — their answers weigh all six about "
        "equally, so this needs a conversation rather than a ranking."
    )
    assert _names_a_motivator(verdict.hedge) == []


def _leaders_tied_responses() -> dict[str, int]:
    """Practical Return and Understanding pinned at the top, level with each
    other and 25 points clear of third."""
    responses: dict[str, int] = {}
    for item in mot.LIKERT_ITEMS:
        if item.motivator in ("economic", "theoretical"):
            responses[item.item_id] = 1 if item.reverse else 5
        elif item.motivator == "social":
            responses[item.item_id] = 5 if item.reverse else 1
        else:
            responses[item.item_id] = 2 if item.reverse else 4
    return responses


def _contested_second_responses() -> dict[str, int]:
    """One clear leader; second and third level with each other."""
    responses: dict[str, int] = {}
    for item in mot.LIKERT_ITEMS:
        if item.motivator == "economic":
            responses[item.item_id] = 1 if item.reverse else 5
        elif item.motivator in ("theoretical", "social"):
            responses[item.item_id] = 2 if item.reverse else 4
        else:
            responses[item.item_id] = 5 if item.reverse else 1
    return responses


def test_motivators_probe_pending_hedges_name_only_what_they_establish() -> None:
    """Neither reason may publish a ranking the gate has just closed.

    These two reasons fire on a *fully* answered section, so unlike the
    incomplete case there is something real behind the names. They are still
    hedges on a closed gate, though, and a closed gate is what makes the hedge
    the only body text the subject sees.

    Where they stand:

    * ``top_two_unordered`` names a pair the answers do establish (25 points
      clear of third) and joins it with "and", asserting no order. The joiner is
      the load-bearing word and is asserted below, because ``headline()`` swaps
      it for "then" once the margin opens up.
    * ``runner_up_contested`` used to say "Practical Return **then**
      Understanding (the second place is a close call)" — asserting an order
      into second place at the exact moment second place is what is contested,
      and doing it in the one slot a refused layer is guaranteed to show its
      subject. ``headline()`` now names the leader, which the answers do
      establish, and says plainly that the rest of the order is not settled.
    """
    unordered = mot.score_motivators(_leaders_tied_responses())
    assert unordered.probe_reason == "top_two_unordered"
    unordered_hedge = ct.verdict_for("motivators", unordered).hedge
    assert unordered_hedge == (
        "Driven most by Practical Return and Understanding (their answers put "
        "these two close together); Human Impact moves them least."
    )
    assert " then " not in unordered_hedge, "the pair is unordered; do not order it"

    contested = mot.score_motivators(_contested_second_responses())
    assert contested.probe_reason == "runner_up_contested"
    contested_hedge = ct.verdict_for("motivators", contested).hedge
    assert contested_hedge == (
        "Driven most by Practical Return; which orientation comes second is "
        "not settled by these answers."
    )
    # The leader is established and may be named; second place is precisely
    # what is contested, so no order word may appear and no runner-up may be
    # named. "then" asserting an order into the contested slot was the defect.
    assert " then " not in contested_hedge
    assert "Understanding" not in contested_hedge


def test_a_zero_answer_motivator_axis_never_reaches_a_reportable_ranking() -> None:
    """An unanswered axis is unknown, not average.

    ``score_motivators`` still parks a zero-answer axis at emphasis 50.0 and
    ranks it — asserted here rather than glossed, because that is the live
    behaviour and pretending otherwise is worse than the behaviour. What is
    guaranteed is that it can never be *read*: a zero-answer axis is thin, a
    thin axis makes the result incomplete, and an incomplete result closes the
    gate, so ``layer_of`` returns ``None`` and the hedge names nothing.

    Removing the 50.0 default from ``score_motivators`` is the durable fix and
    it lands in ``motivators.py``, which owns ``bottom``, ``top_two``,
    ``headline()``, ``as_dict()`` and ``ProbeQuestion.render`` — all of which
    index a six-long ranking today. That is outside this change; see the fix
    report.
    """
    result = mot.score_motivators({ITEM_IDS[0]: 5})
    unanswered = [entry for entry in result.ranked if entry.answered == 0]

    # The live behaviour, stated.
    assert len(unanswered) == 5
    assert {entry.emphasis for entry in unanswered} == {50.0}
    assert all(entry.thin for entry in unanswered)
    assert result.is_complete is False

    # The guarantee that actually protects the person.
    envelope = ct.envelope_for("motivators", result)
    verdict = ct.read_verdict(envelope)
    assert verdict.reportable is False
    assert ct.layer_of(envelope) is None
    assert verdict.hedge is not None
    assert _names_a_motivator(verdict.hedge) == []


# ── Every refusal reason, on every layer, read as a string ────────────────────


def _straight_lined_disc() -> disc.DiscResult:
    return disc.score_disc(
        {item.item_id: 3 for item in disc.LIKERT_ITEMS},
        {pair.item_id: "a" for pair in disc.FORCED_CHOICE_PAIRS},
    )


def test_disc_refusal_hedges_name_no_style() -> None:
    """DISC's style name is the most quotable line in the guide. Both refusals."""
    empty = ct.verdict_for("disc", disc.score_disc({}, {}))
    assert empty.reportable is False
    assert empty.reasons == (
        ct.REASON_INSUFFICIENT_ANSWERS,
        ct.REASON_RETAKE_RECOMMENDED,
        ct.REASON_NO_LAYER_DATA,
    )
    assert empty.hedge == (
        "Only 0 of 48 items answered (0%); not enough to place a style."
    )

    lined = ct.verdict_for("disc", _straight_lined_disc())
    assert lined.reportable is False
    assert lined.reasons == (ct.REASON_RETAKE_RECOMMENDED, ct.REASON_FLAT_PROFILE)
    assert lined.hedge == (
        "24 identical answers in a row across items that alternate dimensions "
        "and include reverse-worded statements."
    )

    style_names = {style.name for style in disc.WORK_STYLES.values()}
    for verdict in (empty, lined):
        assert verdict.hedge is not None
        assert [name for name in style_names if name in verdict.hedge] == []


def test_big_five_refusal_hedges_name_no_domain_or_score() -> None:
    """Big Five refuses by absence, and its notes are generic by construction."""
    nothing = ct.verdict_for("big_five", bf.score({}))
    assert nothing.reportable is False
    assert nothing.reasons == (ct.REASON_NO_LAYER_DATA,)
    assert nothing.hedge == (
        "Not enough items were answered in at least one domain to score it."
    )

    neutral = ct.verdict_for("big_five", bf.score({i.item_id: 3 for i in bf.ITEMS}))
    assert neutral.reportable is False
    assert neutral.reasons == (ct.REASON_QUALITY_FLAGGED,)
    assert neutral.hedge is not None
    assert neutral.hedge.startswith("A long run of identical answers")

    labels = set(bf.DOMAIN_LABELS.values())
    for verdict in (nothing, neutral):
        assert verdict.hedge is not None
        assert [label for label in labels if label in verdict.hedge] == []


def test_type16_refusal_hedges_are_authored_and_carry_no_portrait() -> None:
    """All three type16 refusals, including the two that used to hedge with
    ``None`` and render as the frontend's generic shrug."""
    unresolved = ct.verdict_for(
        "type16",
        type16.derive(
            _big_five({d: 87.5 for d in bf.Domain if d is not bf.Domain.EXTRAVERSION})
        ),
    )
    assert unresolved.reportable is False
    assert unresolved.reasons == (ct.REASON_UNRESOLVED,)
    assert unresolved.hedge == (
        "No letters for now: this framework needs a score for Outward energy, "
        "and that part of the interview was not answered enough to score. The "
        "domains that were scored are unaffected."
    )

    all_coin_flips = ct.verdict_for(
        "type16", type16.derive(_big_five({d: 50.0 for d in bf.Domain}))
    )
    assert all_coin_flips.reportable is False
    assert all_coin_flips.reasons == (ct.REASON_COIN_FLIP,)
    assert all_coin_flips.hedge is not None
    assert "the code is four coin flips and describes nothing in particular" in (
        all_coin_flips.hedge
    )

    suppressed = ct.verdict_for(
        "type16", type16.derive(bf.score({i.item_id: 3 for i in bf.ITEMS}))
    )
    assert suppressed.reportable is False
    assert suppressed.reasons[0] == ct.REASON_SUPPRESSED_BY_SOURCE
    assert suppressed.hedge is not None
    assert suppressed.hedge.startswith("No letters for this version of the profile.")

    for verdict in (unresolved, all_coin_flips, suppressed):
        assert verdict.hedge is not None
        assert [
            code
            for code, paragraph in type16.WORK_STYLE.items()
            if paragraph in verdict.hedge
        ] == []
        assert type16.find_unqualified_codes(verdict.hedge) == ()


def test_strengths_refusal_hedges_name_no_theme() -> None:
    """Strengths refuses on theme count, and its note is the hedge."""
    not_taken = ct.verdict_for("strengths", st.StrengthsRead.not_taken())
    assert not_taken.reportable is False
    assert not_taken.reasons == (ct.REASON_NO_LAYER_DATA,)
    assert not_taken.hedge == (
        "The strengths section fills in from your interview answers."
    )

    thin = ct.verdict_for(
        "strengths",
        st.StrengthsRead(
            themes=(),
            thin_transcript=True,
            note=(
                "Your answers did not give us enough to name strengths yet. "
                "Adding a story or two to the interview is usually all it takes."
            ),
        ),
    )
    assert thin.reportable is False
    assert thin.reasons == (ct.REASON_THIN_TRANSCRIPT, ct.REASON_NO_LAYER_DATA)
    assert thin.hedge is not None
    assert thin.hedge.startswith("Your answers did not give us enough to name")

    labels = {spec.name for spec in st.THEMES.values()}
    for verdict in (not_taken, thin):
        assert verdict.hedge is not None
        assert [label for label in labels if label in verdict.hedge] == []


def test_enneagram_refusal_hedges_by_provenance() -> None:
    """No refusal reason may name a type.

    The Enneagram's designed refusal — two candidates shown, neither ranked,
    "tell us which fits" — is legitimate only when the answers actually put two
    types ahead of the other seven. On a flat answer set they do not: every type
    centres on the person's own mean, all nine land on the same emphasis, and the
    "top two" are ``TYPE_ORDER[:2]``. That shipped as a two-type question with
    full portraits, and picking one stored a self-identified type at high
    confidence — so every straight-liner in the company was funnelled into the
    same published type by declaration order.

    ``score_enneagram`` now checks whether the bank differentiated at all before
    it interprets the margin, and returns no candidates when it did not.
    """
    type_labels = {spec.label for spec in enn.TYPES.values()}

    not_taken = ct.verdict_for("enneagram", enn.EnneagramRead.not_taken())
    assert not_taken.reportable is False
    assert not_taken.reasons == (ct.REASON_NOT_TAKEN,)
    assert not_taken.hedge == "This section of the interview is not finished yet."

    declined = ct.verdict_for("enneagram", enn.EnneagramRead.declined())
    assert declined.reportable is False
    assert declined.reasons == (ct.REASON_DECLINED,)
    assert declined.hedge == "This person chose not to include an Enneagram type."

    too_few = ct.verdict_for(
        "enneagram",
        enn.score_enneagram({item.item_id: 4 for item in enn.LIKERT_ITEMS[:3]}),
    )
    assert too_few.reportable is False
    assert too_few.reasons == (ct.REASON_UNRESOLVED,)
    assert too_few.hedge is not None
    assert too_few.hedge.startswith(
        "Not enough answers in this section to suggest a type."
    )

    for verdict in (not_taken, declined, too_few):
        assert verdict.hedge is not None
        assert [label for label in type_labels if label in verdict.hedge] == []

    flat = ct.verdict_for(
        "enneagram",
        enn.score_enneagram({item.item_id: 3 for item in enn.LIKERT_ITEMS}),
    )
    assert flat.reportable is False
    assert flat.reasons == (ct.REASON_UNRESOLVED,)
    assert flat.hedge is not None
    # NO type may be named here. On a flat sitting every type centres to the
    # same emphasis, so any name in this sentence is TYPE_ORDER declaration
    # order. score_enneagram's differentiation gate returns empty candidates
    # for exactly this case, and an empty candidate tuple makes the
    # confirmation step offer the whole list instead of two arbitrary types.
    assert [label for label in type_labels if label in flat.hedge] == []
    flat_read = enn.score_enneagram({item.item_id: 3 for item in enn.LIKERT_ITEMS})
    assert flat_read.candidates == ()
    assert flat_read.provenance == "unresolved"
    # Every straight-lined value, not just the midpoint — an all-1s or all-5s
    # sitting is the same absence of signal.
    for value in (1, 2, 4, 5):
        straight = enn.score_enneagram(
            {item.item_id: value for item in enn.LIKERT_ITEMS}
        )
        assert straight.candidates == (), value
        assert straight.type_number is None, value


# ── The all-midpoint respondent ───────────────────────────────────────────────


def test_the_all_midpoint_respondent_refuses_on_every_layer() -> None:
    """Somebody who clicked "3" down every scale. Already held — pinned.

    This is the answer set that produced the original fabricated-ranking bug in
    ``motivators`` and it is the one a straight-liner actually generates, so it
    is the single most valuable regression pin in the file. Every scored layer
    must refuse, and no hedge may name a result.
    """
    big_five_result = bf.score({item.item_id: 3 for item in bf.ITEMS})
    verdicts = {
        "disc": ct.verdict_for("disc", _straight_lined_disc()),
        "big_five": ct.verdict_for("big_five", big_five_result),
        "type16": ct.verdict_for("type16", type16.derive(big_five_result)),
        "motivators": ct.verdict_for(
            "motivators", mot.score_motivators({i: 3 for i in ITEM_IDS})
        ),
        "enneagram": ct.verdict_for(
            "enneagram",
            enn.score_enneagram({item.item_id: 3 for item in enn.LIKERT_ITEMS}),
        ),
        # Strengths comes from a transcript, not a scale, so an all-midpoint
        # sitting has nothing to say about it. Included because ScoringOutput
        # requires all six and an omitted layer is the thing contracts.py
        # refuses to allow.
        "strengths": ct.verdict_for("strengths", st.StrengthsRead.not_taken()),
    }

    for framework, verdict in verdicts.items():
        assert verdict.reportable is False, f"{framework} published a flat sitting"
        assert verdict.confidence == "none", framework
        assert verdict.reasons, f"{framework} refused without saying why"
        # A refused layer with no hedge falls back to the frontend's generic
        # sentence. Acceptable, but every layer here should do better.
        assert verdict.hedge, f"{framework} refused with nothing to show the person"

    assert _names_a_motivator(verdicts["motivators"].hedge or "") == []
    assert [
        paragraph
        for paragraph in type16.WORK_STYLE.values()
        if paragraph in (verdicts["type16"].hedge or "")
    ] == []
    assert [
        style.name
        for style in disc.WORK_STYLES.values()
        if style.name in (verdicts["disc"].hedge or "")
    ] == []


# ── Model prose never reaches a person unscreened ─────────────────────────────


def test_a_model_rationale_about_advancement_drops_the_theme() -> None:
    """The advisory guard has to run on runtime prose, not just authored prose.

    ``strengths._check_taxonomy`` screens the authored taxonomy at import, which
    covered every string in the module and none of the strings that actually
    reach a guide: ``why`` is written by the model at extraction time and
    rendered close to verbatim by ``guide_section()`` into a document colleagues
    read. A theme explained in terms of who is ready for promotion is the
    advisory boundary breaking, and it was reaching the page unchecked.
    """
    theme_id = next(iter(st.THEMES))
    quote = "I ended up being the one who kept the release plan honest."
    transcript = (st.Turn(speaker=st.Speaker.INTERVIEWEE, text=quote),)

    clean = st.validate_extraction(
        {
            "themes": [
                {
                    "theme_id": theme_id,
                    "confidence": "high",
                    "evidence_quote": quote,
                    "why": "Keeps a plan honest when it starts to slip.",
                }
            ]
        },
        transcript,
    )
    assert [t.theme_id for t in clean.themes] == [theme_id]

    tainted = st.validate_extraction(
        {
            "themes": [
                {
                    "theme_id": theme_id,
                    "confidence": "high",
                    "evidence_quote": quote,
                    "why": "Clearly ready for promotion and the best in the company.",
                }
            ]
        },
        transcript,
    )
    assert tainted.themes == (), "advancement prose must drop the theme"


def test_the_strengths_refusal_hedge_is_authored_not_model_written() -> None:
    """A refused layer's hedge is the only body text its subject sees.

    ``validate_extraction`` used to lift the model's own ``notes`` field verbatim
    into ``StrengthsRead.note``, which ``contracts`` passes straight through as
    the hedge — so on the one surface guaranteed to be read, the sentence about
    a person's own strengths was unconstrained model output screened by nothing.
    """
    transcript = (st.Turn(speaker=st.Speaker.INTERVIEWEE, text="Not much to say."),)
    read = st.validate_extraction(
        {
            "themes": [],
            "notes": "This person is a top performer ready for more responsibility.",
        },
        transcript,
    )
    assert read.themes == ()
    assert read.note is not None
    assert "top performer" not in read.note
    assert "ready for more" not in read.note
    assert read.note.startswith("Your answers did not give us enough")
