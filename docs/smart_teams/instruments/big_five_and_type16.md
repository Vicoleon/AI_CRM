# Big Five backbone & the derived four-letter layer — sources, provenance, and design

**Ticket:** ST-1 (instrument design) · **Modules:** `rubik_core/smart_teams/instruments/big_five.py`,
`rubik_core/smart_teams/instruments/type16.py` · **Researched and verified:** 2026-08-19

Two layers of the Cubie Work Style Interview are covered here, and they belong in
one document because the second is arithmetic on the first. `big_five.py` is the
only layer of a Smart Teams profile whose items come from a published, validated,
public-domain instrument — everything else in `instruments/` is authored in-house
on a public-domain *model*. `type16.py` measures nothing at all: it restates four
of the five Big Five domains as letters, because people ask for their letters.

The DISC-style layer's provenance argument lives in `disc.md`; motivators and the
Enneagram in `motivators_and_enneagram.md`; the strengths taxonomy in
`strengths.md`. `ADVISORY_NOTICE` in `disc.py` is the single source of truth for
the in-product disclaimer, and `type16.py` imports it rather than restating it.

**One thing to read before anything else.** A four-letter code is the weakest
thing in a Smart Teams profile and the most quotable. It is derived from a
self-report, it throws away the continuous score it came from, and the framework
it borrows its shape from has no letter for one of the five domains we actually
measured. It must never be the headline of a guide. See
[§5 Limitations](#5-limitations), which is the part of this document that matters
most.

---

## 1. Sources

Every URL below was opened during the 2026-08-19 pass unless the row says
otherwise. Rows marked **not fetched** are cited as the canonical reference but
were not read in this pass; nothing in either module rests on them alone.

### The Big Five model and the IPIP item pool

| Source | URL | Status |
|---|---|---|
| IPIP — "Administering IPIP Measures, with a 50-item Sample Questionnaire". The canonical home of the IPIP-50 and its scoring key | https://ipip.ori.org/new_ipip-50-item-scale.htm | **not fetched** (see §2) |
| openpsychometrics.org printable "Big Five Personality Test … courtesy ipip.ori.org" — all 50 item texts plus the per-item scoring formulas | https://openpsychometrics.org/printable/big-five-personality-test.pdf | verified, item-by-item |
| openpsychometrics.org test page — names the instrument as the "IPIP Big-Five Factor Markers", 50 items, citing Goldberg (1992) | https://openpsychometrics.org/tests/IPIP-BFFM/1.php | verified (metadata only; the page does not show items) |
| University of Oregon Personality and Social Dynamics Lab — "IPIP scales are 100% public domain – no permission required, ever" | https://psdlab.uoregon.edu/measuring-the-big-five-personality-domains | verified, quoted |
| Wikipedia — IPIP: "a public domain collection of items for use in personality tests"; attributes the 50-item set to Lewis Goldberg | https://en.wikipedia.org/wiki/International_Personality_Item_Pool | verified |
| Goldberg, L. R. (1992). "The development of markers for the Big-Five factor structure." *Psychological Assessment*, 4(1), 26–42 | — | bibliographic only; not read in this pass |

### The aspect (facet) structure

| Source | URL | Status |
|---|---|---|
| DeYoung, C. G., Quilty, L. C., & Peterson, J. B. (2007). "Between facets and domains: 10 aspects of the Big Five." *Journal of Personality and Social Psychology*, 93(5), 880–896 | https://pubmed.ncbi.nlm.nih.gov/17983306/ | citation verified (volume, pages, title) |
| Big Five Aspect Scales — the ten aspect names and their domains, with the DeYoung et al. citation | https://emilywilloughby.com/research/bfas | verified, used for the aspect names |

### The four-letter derivation

| Source | URL | Status |
|---|---|---|
| McCrae, R. R., & Costa, P. T. (1989). "Reinterpreting the Myers-Briggs Type Indicator From the Perspective of the Five-Factor Model of Personality." *Journal of Personality*, 57(1), 17–40. DOI 10.1111/j.1467-6494.1989.tb00759.x | https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-6494.1989.tb00759.x | **Table 3 read in full text** — see the note below |
| Furnham, A. (2022). "The Big Five Facets and the MBTI: The Relationship between the 30 NEO-PI(R) Facets and the Four Myers-Briggs Type Indicator (MBTI) Scores." *Psychology*, 13, 1504–1516. DOI 10.4236/psych.2022.1310095 (open access, CC-BY) | https://doi.org/10.4236/psych.2022.1310095 · PDF: https://www.scirp.org/pdf/psych_2022092914053553.pdf | verified, full text read |
| Furnham, A. (1996). "The big five versus the big four: the relationship between the MBTI and NEO-PI five factor model of personality." *Personality and Individual Differences* | https://www.sciencedirect.com/science/article/abs/pii/0191886996000335 | abstract only; the finding used here (Neuroticism unmatched) is quoted via Furnham 2022's own literature review |

**On the 1989 numbers.** The Wiley link above is the citation of record and is an
abstract. The Table 3 coefficients quoted in §4.3 were read from a full-text PDF
copy of the article hosted at `https://leadu-library.com/mj/2007/club/MBTI/MBTI-5factor.pdf`,
which reproduces the article including its tables. It is a third-party mirror, not
a publisher copy — recorded here because a number in a psychometric doc should say
where it was actually read, and anyone re-checking these figures should prefer the
publisher's version or a library copy.

### MBTI trademark and proprietary status

| Source | URL | Status |
|---|---|---|
| The Myers-Briggs Company — trademark notice: Myers-Briggs Type Indicator, Myers-Briggs, MBTI and related marks are trademarks or registered trademarks of Myers & Briggs Foundation, Inc.; the company is the exclusive publisher of the MBTI instrument | https://www.themyersbriggs.com/en-us/support/trademarks | verified via search result text |
| The Myers & Briggs Foundation — MBTI guide to permissions and trademarks | https://www.myersbriggs.org/using-type-as-a-professional/mbti-permission-trademarks/ | cited; the page itself was not fetched (see §5.6) |

---

## 2. Provenance audit — what was checked, and what came back

`plan.md` promises a public-domain foundation for every layer. For this layer that
promise is only as good as the check behind it, so the check is written out.

### What was verified

A script compared all 50 items in `big_five.ITEMS` against the item texts and
scoring formulas in the openpsychometrics printable sheet — an independent
reproduction that credits `ipip.ori.org` on its face — on three fields at once:

1. **Wording**, character for character, including punctuation and the
   mid-sentence start that `ITEM_STEM` completes.
2. **Domain assignment**, from which of the five scoring formulas the item number
   appears in.
3. **Keying sign**, from whether the formula adds or subtracts that item.

**Result: 50 of 50 matched. Zero text discrepancies, zero domain discrepancies,
zero sign discrepancies.**

### Independently corroborated, 2026-08-20

The audit above rests on one reproduction, which is a single point of failure for
the claim the whole layer stands on, so it was checked again against two further
sources that publish the scoring key rather than the item texts:

* A published item-number → keying map, which lists the reverse-scored sets as
  E 6/16/26/36/46, A 2/12/22/32, C 8/18/28/38, N 9/19, O 10/20/30. Our bank
  matches all five sets exactly, including the Neuroticism → Emotional Stability
  sign inversion on items 9 and 19.
  <https://psychkit.org/ipip-50-big-five-personality-test/>
* A description of the published key giving the per-factor keying balance — five
  positive and five negative on Factor I, six positive and four negative on
  Factor II — which matches the per-domain table below.
  <https://db.arabpsychology.com/scales/international-personality-item-pool-five-factor-model-50-ipip-big5/>

The second source also states the licensing position plainly: the IPIP is
"explicitly a public-domain resource" requiring "no licensing fees or
permissions". That is the sentence the layer's legal basis rests on.

One discrepancy worth recording rather than smoothing over: the first source's
prose labels Factors II and III as "5 positively keyed, 5 negatively keyed"
while its own item list shows four reverse-scored items on each. Its item list
agrees with our bank and with the second source's 6/4; its summary counts do
not agree with itself. Where a source contradicts itself we followed its item
list, and noted it here rather than quietly picking the convenient half.

`ipip.ori.org` is the canonical home and is cited on the reproductions' faces,
but it is deliberately not fetched by anything here: it hung an earlier
automated attempt for eighteen minutes across six retries.

The script is below rather than described, so the audit is a thing that can be
re-run instead of a claim that has to be believed. Run it from the repository root
with the root on `PYTHONPATH`; on 2026-08-19 it printed
`50 items checked, 0 discrepancies` / `provenance: {'ipip': 50}` /
`keying balance: (26, 24)`.

```python
"""Re-run the IPIP-50 provenance audit. Prints one line per discrepancy, then counts.

SOURCE for both literals below: the openpsychometrics printable sheet cited in §1
("Big Five Personality Test … courtesy ipip.ori.org"). Item texts are its numbered
list; KEYING is its five scoring formulas, transcribed sign for sign.
"""

from collections import Counter

from rubik_core.smart_teams.instruments import big_five as bf

SOURCE_ITEMS = """
1 Am the life of the party.
2 Feel little concern for others.
3 Am always prepared.
4 Get stressed out easily.
5 Have a rich vocabulary.
6 Don't talk a lot.
7 Am interested in people.
8 Leave my belongings around.
9 Am relaxed most of the time.
10 Have difficulty understanding abstract ideas.
11 Feel comfortable around people.
12 Insult people.
13 Pay attention to details.
14 Worry about things.
15 Have a vivid imagination.
16 Keep in the background.
17 Sympathize with others' feelings.
18 Make a mess of things.
19 Seldom feel blue.
20 Am not interested in abstract ideas.
21 Start conversations.
22 Am not interested in other people's problems.
23 Get chores done right away.
24 Am easily disturbed.
25 Have excellent ideas.
26 Have little to say.
27 Have a soft heart.
28 Often forget to put things back in their proper place.
29 Get upset easily.
30 Do not have a good imagination.
31 Talk to a lot of different people at parties.
32 Am not really interested in others.
33 Like order.
34 Change my mood a lot.
35 Am quick to understand things.
36 Don't like to draw attention to myself.
37 Take time out for others.
38 Shirk my duties.
39 Have frequent mood swings.
40 Use difficult words.
41 Don't mind being the center of attention.
42 Feel others' emotions.
43 Follow a schedule.
44 Get irritated easily.
45 Spend time reflecting on things.
46 Am quiet around strangers.
47 Make people feel at ease.
48 Am exacting in my work.
49 Often feel blue.
50 Am full of ideas.
"""

KEYING = {
    bf.Domain.EXTRAVERSION: "+1 -6 +11 -16 +21 -26 +31 -36 +41 -46",
    bf.Domain.AGREEABLENESS: "-2 +7 -12 +17 -22 +27 -32 +37 +42 +47",
    bf.Domain.CONSCIENTIOUSNESS: "+3 -8 +13 -18 +23 -28 +33 -38 +43 +48",
    bf.Domain.EMOTIONAL_STABILITY: "-4 +9 -14 +19 -24 -29 -34 -39 -44 -49",
    bf.Domain.OPENNESS: "+5 -10 +15 -20 +25 -30 +35 +40 +45 +50",
}

texts = dict(
    (int(n), text)
    for n, text in (line.split(" ", 1) for line in SOURCE_ITEMS.strip().splitlines())
)
expected = {
    int(token[1:]): (domain, 1 if token[0] == "+" else -1)
    for domain, formula in KEYING.items()
    for token in formula.split()
}

problems = 0
for item in bf.ITEMS:
    domain, keyed = expected[item.ipip_number]
    if item.text != texts[item.ipip_number]:
        print(f"TEXT  {item.item_id}: {item.text!r} != {texts[item.ipip_number]!r}")
        problems += 1
    if item.domain is not domain or item.keyed != keyed:
        print(
            f"KEY   {item.item_id}: {item.domain}/{item.keyed:+d} != {domain}/{keyed:+d}"
        )
        problems += 1

print(f"{len(bf.ITEMS)} items checked, {problems} discrepancies")
print("provenance:", dict(Counter(i.provenance.value for i in bf.ITEMS)))
print("keying balance:", bf.keying_balance())
```

### The provenance table

| Provenance | Before the audit | After the audit | Change |
|---|---|---|---|
| `ipip` | 50 | 50 | — |
| `authored` | 0 | 0 | — |
| **Total** | **50** | **50** | — |

Nothing was relabelled, because nothing failed. That is the outcome to hope for
and not the outcome to assume: an item labelled `ipip` whose wording we actually
invented would be a provenance lie sitting inside the one layer of this feature
that claims external validity, and it would be invisible to every reader
downstream.

Per-domain, all 50 items, with the keying counts the same script read off the
scoring formulas:

| Domain | Items | + keyed | − keyed | `ipip` | `authored` |
|---|---|---|---|---|---|
| Extraversion | 10 | 5 | 5 | 10 | 0 |
| Agreeableness | 10 | 6 | 4 | 10 | 0 |
| Conscientiousness | 10 | 6 | 4 | 10 | 0 |
| Emotional Stability | 10 | 2 | 8 | 10 | 0 |
| Openness | 10 | 7 | 3 | 10 | 0 |
| **Bank** | **50** | **26** | **24** | **50** | **0** |

`big_five.keying_balance()` recomputes those counts from the bank, so a mistyped
sign shows up as a disagreement with this table rather than as a quietly shifted
score.

### What was *not* verified, stated plainly

- **`ipip.ori.org` was not fetched during this pass.** The official item page and
  keying summary are the source of record and are cited above, but the evidence
  behind the "verbatim" claim is the independent reproduction, not the official
  site. `big_five.py`'s docstring says exactly that. An earlier draft of that
  docstring claimed the items had been "transcribed from the official IPIP site"
  and cross-checked against the official key page; that claim could not be
  substantiated and has been removed.
- **IPIP's permission-page wording is not quoted.** An earlier draft carried a
  verbatim quotation from IPIP's permission page. It has been replaced with the
  public-domain statements from the two sources that *were* read (§1). The
  substance is unchanged — IPIP is public domain, commercial use included — but we
  no longer present unread wording as a quotation.
- **IPIP's scoring-instruction page is not quoted.** The reverse-scoring rule in
  `big_five.item_score` is the standard IPIP procedure (+keyed 1→5, −keyed 5→1) and
  is confirmed by the printable sheet's own formulas. Two verbatim quotations of
  the instruction page have been removed from the module.
- **The response-scale anchors are the weakest provenance in the module.**
  1 = "Very Inaccurate" and 5 = "Very Accurate" are IPIP's anchors according to
  secondary descriptions of its administration procedure; IPIP's own instruction
  page was not read, and the printable sheet the items were verified against
  re-presents the scale as disagree/agree, so it does not corroborate the accuracy
  wording. The three intermediate labels in `RESPONSE_LABELS` are ours. The module
  comment says all of this. No scoring depends on it — the arithmetic uses the 1-5
  values only — but an interview screen that shows these words is showing our
  reconstruction of IPIP's scale, not a transcription of it.

### The legal argument, in one paragraph

The IPIP is public domain, explicitly including commercial use, and needs no
permission from anyone. That is why this layer reproduces someone else's items
verbatim while every other layer in `instruments/` authors its own: for a backbone,
verbatim reuse is not merely permitted but *desirable*, because items we wrote
ourselves would inherit none of the published psychometrics that make this the
layer other layers are checked against.

---

## 3. Big Five scoring math

### 3.1 Response scale

Five points, `1`–`5`, presented as an accuracy scale rather than agree/disagree,
because the items are self-descriptions. `RESPONSE_NEUTRAL = 3`.

### 3.2 Per item

```
item_score(item, r) = r                               if item.keyed > 0
                    = RESPONSE_MIN + RESPONSE_MAX - r if item.keyed < 0
```

Written as a reflection about the scale's centre rather than as `6 - r`, so it
stays correct if the scale ever widens.

### 3.3 Per domain

IPIP scores by summing, which makes a raw total meaningless unless you also know
how many items produced it — and ours varies, because a domain can legitimately be
scored on 7 of its 10 items. So the sum is normalised against the floor and span of
however many items were answered:

```
score = (total - count·RESPONSE_MIN) / (count·(RESPONSE_MAX - RESPONSE_MIN)) · 100
```

A domain with fewer than `MIN_ANSWERED_PER_DOMAIN = 7` answered items is **not
scored** and is named in `BigFiveResult.unscored_domains`. Callers must not default
it to 50: the midpoint means "the items pulled both ways", and "we don't know" is a
different statement.

### 3.4 Polarity

The module scores **Emotional Stability**, keeping IPIP's own direction, in which
"Get stressed out easily" is a minus item. Flipping the sign to score Neuroticism
would mean the `keyed` field no longer matched the source it claims to come from.
`BigFiveResult.neuroticism_equivalent` returns `100 - stability` for anyone
comparing against published research; the work-facing label stays Emotional
Stability, because a guide the whole company reads should not hand someone a
"neuroticism" score.

### 3.5 Bands

Five even fifths of the 0–100 scale: `clearly_lower` (0–20), `somewhat_lower`
(20–40), `middle` (40–60), `somewhat_higher` (60–80), `clearly_higher` (80–100).

**These are not percentiles.** The company has no norm sample and no licence to anyone
else's, so a band says where a score sits between the floor and the ceiling of this
instrument and nothing about how a person compares to a population. Even fifths are
used precisely because there is no norm data to justify uneven cutoffs. The middle
band ships with a caveat string of its own, because "50" is the number most often
over-read.

### 3.6 Aspects (facets), and why Agreeableness has none

The IPIP-50 is a domain-level instrument and was never keyed to facets. Each item
is mapped by *content* onto the ten-aspect structure of DeYoung, Quilty & Peterson
(2007), and an aspect is then reported only if it clears both rules:

- at least `MIN_ITEMS_PER_FACET = 3` items, and
- no more than `MAX_FACET_SHARE = 0.75` of its domain's items.

Agreeableness fails both ways at once — 8 of its 10 items are compassion-flavoured
and 2 are politeness-flavoured — so **Agreeableness is reported as a domain score
and nothing else**. Printing a "politeness" number off two items would have been
the dishonest option. `REPORTABLE_FACETS` and `UNREPORTABLE_FACETS` are computed at
import from the rule itself, so the shipped list cannot drift from the policy.

Four of the ten aspect *names* in `Facet` are ours, not the BFAS's. The BFAS names
the Openness pair "Intellect" and "Openness" (an aspect sharing its domain's name
reads as a typo in a guide, so ours is `O_IMAGINATION`), and names the Neuroticism
pair "Volatility" and "Withdrawal" (named in the direction we deliberately do not
score, so ours are `ES_COMPOSURE` and `ES_FREEDOM_FROM_WORRY`). The ten-aspect
*structure* is DeYoung et al.'s; the labels and the item assignment are not, and a
facet score is a hint about which half of a domain is driving it — never an
independent measurement.

### 3.7 Response quality

Six checks over the raw answers, each keeping its measured value so a reviewer sees
how close to the line a sitting was:

| Check | Threshold | Flag |
|---|---|---|
| Longest run of identical answers | ≥ `MAX_IDENTICAL_RUN` (10) | `straight_lining` |
| Standard deviation of raw answers | < `MIN_RESPONSE_SD` (0.40) | `low_variance` |
| Share of answers on the midpoint | > `MAX_NEUTRAL_SHARE` (0.60) | `neutral_overuse` |
| Balanced acquiescence index | > +`MAX_ACQUIESCENCE` (0.75) / < −0.75 | `acquiescence` / `nay_saying` |
| Worst within-domain keying gap | > `MAX_KEYING_GAP` (1.5 scale points) | `keying_inconsistency` |
| Answered items in every domain | < `MIN_ANSWERED_PER_DOMAIN` (7) | `incomplete` |

The run check walks the bank's own presentation order, which cycles
E → A → C → ES → O ten times; ten identical answers therefore spans two full cycles
of five unrelated traits.

**The shared-threshold contract.** The DISC-style bank scores the *same sitting* as
this one, so a careless interview must not come out flagged on one instrument and
clean on the other — whichever answer a reviewer saw first would become the truth.
The cutoffs are published as
`big_five.SHARED_RESPONSE_QUALITY_THRESHOLDS` (a frozen
`ResponseQualityThresholds`) for other instrument modules to **import**. The
individual module-level constants remain importable too. This replaces an earlier
comment that told a human reader to keep two files' numbers in agreement, which is
the kind of instruction that is true on the day it is written and false a quarter
later. Deliberately not shared: `MIN_ANSWERED_PER_DOMAIN` (a fact about this bank
being ten items per domain, not a house rule) and any index that exists in only one
bank — DISC's forced-choice position share has no counterpart here, and this bank's
per-domain keying gap has none there.

### 3.8 The acquiescence index, and the defect that was fixed

The naive index — mean raw response across the whole bank, minus the midpoint — has
a defect precisely on the domain the flag warns about. Emotional Stability is keyed
2 plus / 8 minus, so a respondent who genuinely resets slowly under pressure agrees
with eight of its ten items *because that is true of them*. That lifts the bank-wide
raw mean and reads as yes-saying. The flag then blames a response style for the very
trait score it exists to protect, and the people most likely to be told their
answers look unreliable are the ones lowest on stability.

`_balanced_acquiescence` fixes it by measuring only keying-balanced **pairs**.
Within each domain it takes `k = min(plus_answered, minus_answered)` items from each
side; across those 2k items a real trait pushes the plus half up and the minus half
down by the same amount, so the trait cancels and only a wording-independent lean
survives. Items are taken in bank order, so the subset is deterministic. On a
complete sitting the index uses 36 of the 50 items (E 10, A 8, C 8, ES 4, O 6).

Measured, with the sweep below: across all 5⁵ = 3,125 perfectly trait-consistent
sittings — every plus item answered `v` and every minus item `6 − v`, per domain,
which is zero response style by construction — the naive index climbs to **+0.56**,
three quarters of the way to the 0.75 threshold, and it peaks on exactly the profile
that is *low* on Emotional Stability. The balanced index returns **0.0** for all
3,125 of them.

```python
import itertools
from rubik_core.smart_teams.instruments import big_five as bf

worst = max(
    (
        bf.response_quality(
            {
                item.item_id: (v if item.keyed > 0 else 6 - v)
                for domain, v in zip(bf.Domain, combo)
                for item in bf.ITEMS_BY_DOMAIN[domain]
            }
        )
        for combo in itertools.product([1, 2, 3, 4, 5], repeat=5)
    ),
    key=lambda q: q.raw_mean_drift,
)
print(worst.raw_mean_drift, worst.acquiescence_index)  # 0.56 0.0
```

The naive figure is still reported, as `ResponseQuality.raw_mean_drift`, next to the
balanced one — a wide gap between the two is the signature of a real trait score
rather than a response style, which is exactly the case the flag must not fire on.

**The flag stays advisory anyway.** No within-person statistic can fully separate
style from trait: a person who genuinely agrees with more self-descriptions than
they reject is indistinguishable, from their answers alone, from a person who tends
to agree. So `acquiescence` and `nay_saying` are listed in
`big_five.ADVISORY_FLAGS`, their `QUALITY_FLAG_NOTES` text says in so many words
that they are worth asking about and are not grounds for discarding answers, and
`ResponseQuality.blocking_flags` excludes them. They surface a question; they gate
nothing.

### 3.9 Unknown item ids raise

`response_quality` and `score` both run `_validated`, which raises `ValueError` on
an item id this bank does not know and on an out-of-scale value; `None` is tolerated
and dropped, because the interview is resumable and an unanswered item legitimately
arrives as a null. `disc.py`, `motivators.py` and `enneagram.py` already did this;
`big_five.py` used to log and continue, which meant a typo'd id silently scored as
"unanswered" and quietly shifted a domain. All four instrument modules now agree.
The cost is real and intended: rescoring answers stored against an older bank needs
an explicit migration rather than a silent partial score.

---

## 4. The derived four-letter layer

### 4.1 What it is

`type16.py` performs no measurement. It reads `BigFiveResult` and returns four
letters, each one a threshold applied to a domain score it already had.

### 4.2 What it is not

Not the MBTI. "Myers-Briggs Type Indicator", "Myers-Briggs" and "MBTI" are
trademarks of Myers & Briggs Foundation, Inc., published exclusively by The
Myers-Briggs Company. Cubie does not administer that instrument, reproduces none of
its items, holds no licence to it, and a person's letters from this module are not
an MBTI type and are not comparable with one. `NOT_AN_MBTI_RESULT` ships that
sentence in every payload so no frontend has to compose it.

We also do not borrow the free-test vocabulary: the one-word nicknames attached to
the sixteen combinations by 16personalities and similar publishers are their
content. **This module gives the sixteen combinations no nicknames at all** —
see `WHY_NO_NICKNAMES`. That is partly legal hygiene and partly product judgement:
a memorable nickname is exactly what turns the weakest layer in the profile into the
line people quote about each other.

The sixteen work-style descriptions in `WORK_STYLE` are written in-house from the
four axis meanings, and deliberately about collaboration mechanics — how the person
wants information, decisions, and disagreement handled. No published profile text,
no ability or potential claims.

### 4.3 The derivation and the evidence behind each letter

| Axis | Source domain | Higher score → | Lower score → |
|---|---|---|---|
| E/I | Extraversion | `E` | `I` |
| N/S | Openness | `N` | `S` |
| T/F | Agreeableness | `F` | `T` |
| J/P | Conscientiousness | `J` | `P` |

McCrae & Costa (1989), Table 3, correlated MBTI **continuous** scores against
self-reported NEO-PI factors in 267 men and 201 women. Their scores are keyed toward
the *second* letter of each pair — introversion, intuition, feeling, perceiving —
which is where the negative signs come from; ours count extraversion and judging.

| MBTI index (keyed toward) | NEO-PI factor | r, men | r, women |
|---|---|---|---|
| E-I (introversion) | Extraversion | −.74 | −.69 |
| S-N (intuition) | Openness | .72 | .69 |
| T-F (feeling) | Agreeableness | .44 | .46 |
| J-P (perceiving) | Conscientiousness | −.49 | −.46 |

Two secondary findings in the same table matter for how much weight this mapping
can carry. J-P also correlates with Openness (.30 men / .26 women) — a real
association our single-domain mapping discards. And Neuroticism's largest
correlation with any of the four indices was .16/.17 (E-I) and .06/.28 (T-F): the
paper's own conclusion is that the four indices measure aspects of **four** of the
five factors.

**The direction is well supported; the strength is not settled.** Furnham (2022)
ran the same comparison on nearly 10,000 adults with the NEO-PI-R and found much
weaker relationships throughout: E-I with Extraversion r = .20; T-F with
Agreeableness r = −.35 (still the strongest correlate of T-F); J-P most strongly
with **Openness** (r = −.16) rather than Conscientiousness; and the S-N ↔ Openness
link — second strongest in 1989 — not confirmed at all (S-N correlated only with
Agreeableness, r = .05). Regressions of the Big Five onto the four scales accounted
for between 1% and 21% of variance. `AXIS_EVIDENCE` carries a per-axis summary of
both studies so the caveat travels with the code rather than living only here.

### 4.4 Letter confidence, and the coin flip

A letter is a threshold applied to a continuous score, so a score of 50 produces a
letter with no information in it. Rather than invent a tie-break rule, confidence
is taken from the band `big_five` already assigned the source domain — no new
cutoffs are introduced by this module:

| Source band | Confidence |
|---|---|
| `clearly_lower` / `clearly_higher` | `clear` |
| `somewhat_lower` / `somewhat_higher` | `leaning` |
| `middle` (40–60) | `coin_flip` |

A `coin_flip` letter is **rendered in lowercase inside the code itself** — `ENfJ` —
with the reason appended, so the assertion never exists as a string for a caller to
print. An exact 50.0 falls on the low letter; that direction carries no meaning and
nothing should be built on it.

When **all four** axes are coin flips, `work_style` returns `None` and `describe()`
says the code describes nothing in particular. A code assembled from four
middle-band scores is a real arithmetic result and a meaningless description, and
it is the sentence, not the letters, that readers remember.

### 4.5 Emotional Stability has no letter

The framework has no counterpart for it, per both studies above. So one of the five
things the interview measured — often the one most relevant to how someone wants to
be approached in a hard week — is simply absent from the letters.
`DOMAIN_WITHOUT_A_LETTER` names it, `DOMAIN_WITHOUT_A_LETTER_NOTE` says it in
person-facing language, `describe()` always appends it, and `as_dict()` always
ships it. A reader handed four letters and not told a fifth score exists will
assume the four are everything.

### 4.6 The rendering guard

Three layers, in decreasing order of how much they can be bypassed:

1. **`DerivedTypeCode` is a `str` subclass whose value is the qualified form.**
   `str(code)`, `f"{code}"`, `code + ""` and `json.dumps(code)` all yield
   `"INTJ (derived from Big Five)"`. There is no code path that produces bare
   letters as a side effect of normal formatting, which is the actual failure mode:
   a template interpolates the code, the qualifier lives elsewhere in the layout,
   and a reader walks away with "Cubie says I'm an INTJ".
2. **The bare letters are reachable only through
   `DerivedTypeCode.letters_for_matching`**, deliberately verbose so that one grep
   finds every place a bare code could reach a reader. The comparison engine needs
   to group people, so the accessor has to exist; it does not have to be
   convenient. In JSON it is `letters_for_matching`, shipped beside
   `never_render_letters_alone: true`, with `display` as the field a frontend
   should render.
3. **`find_unqualified_codes(text)`** scans prose — including model-written prose —
   for a four-letter code not followed by "derived" in the same sentence, within
   `QUALIFIER_WINDOW = 32` characters. An LLM asked to describe a profile will
   write "as an INTJ, they…" no matter what the prompt says; the guide generator
   can refuse or repair that before it ships. The pattern is assembled from
   `AXIS_SPECS`, so it cannot drift from the letters the module actually produces.

`Type16Result` has no attribute that returns four plain letters for display.

### 4.7 When no code is produced

`derive()` always returns a `Type16Result` — never `None` — because "we are not
showing you a type, and here is why" is a sentence a guide needs and a caller handed
`None` would have to invent.

- **An unscored source domain** leaves that axis in `unresolved_axes` and the result
  unreportable. Defaulting the missing score to the midpoint would manufacture a
  coin-flip letter out of nothing.
- **Blocking response-quality flags** suppress the layer entirely, with the reasons
  quoted from `QUALITY_FLAG_NOTES`. The derived layer is the first thing to withhold
  when the underlying answers look rushed, because it is the layer that loses least
  by being withheld. Advisory flags (acquiescence, nay-saying) do **not** suppress
  it — see §3.8.

---

## 5. Limitations

### 5.1 A derived type is weaker than the scores it came from — and must never be the headline

This is the single most important sentence in this document. The letters throw away
the continuous score, discard a real secondary association (J-P with Openness),
have no letter for Emotional Stability at all, and rest on a mapping whose strength
the largest available study puts far below the classic figures. Everything a
four-letter code can tell a colleague, the five domain scores tell them better.

Concretely, for anything that renders a profile: the letters may appear as a
secondary line with their qualifier, and the guide's substance must come from the
domains, the DISC-style read, and the person's own words. A guide whose first line
is a four-letter code has inverted the evidence.

### 5.2 No norm sample

Every band and score in this layer is a position on our own scale. There is no
company norm sample and no licence to anyone else's, so nothing here says how a person
compares to a population. Two people's bands are comparable to each other because
they answered the same items; that is the whole of it.

### 5.3 It is a self-report, taken once

The interview measures what someone said about themselves on one day, in a work
context, knowing colleagues will read the result. Self-presentation is not a defect
to be corrected — it is what the instrument measures. The response-quality checks
catch inattention, not impression management, and nothing here can tell the
difference between the two.

### 5.4 Aspect scores are the weakest numbers in the Big Five layer

They come from a content mapping of a domain-level instrument onto a structure that
has its own 100-item scale we are not using. `MIN_ITEMS_PER_FACET` and
`MAX_FACET_SHARE` keep the worst cases out, and Agreeableness loses both aspects as
a result, but a reported aspect is still a hint about which half of a domain is
driving it and not an independent measurement.

### 5.5 The Emotional Stability keying imbalance is inherent

2 plus / 8 minus is IPIP's keying, and we keep it, because pulling items out of a
validated marker set discards the validation we adopted it for. §3.8 removes the
trait confound from the acquiescence *index*; it does not remove the imbalance from
the *domain*. A genuinely acquiescent respondent will still drift low on Emotional
Stability, and no arithmetic in this module can distinguish that from a low score.

### 5.6 Citations we could not close

- `ipip.ori.org` was not fetched in this pass (§2). The official pages are cited as
  the source of record; the verification evidence is an independent reproduction.
- `myersbriggs.org`'s permissions page was not fetched. The trademark facts in §4.2
  come from The Myers-Briggs Company's own trademark notice, which was verified.
- Goldberg (1992) is a bibliographic citation here, not a read source. Nothing in
  either module depends on its contents beyond the instrument's identity, which the
  openpsychometrics test page independently attributes to it.
- Furnham (1996) was read only through Furnham (2022)'s literature review, which is
  by the same author and states the 1996 findings directly. The claim it supports —
  that Neuroticism is not reliably matched to any of the four indices — is also the
  1989 paper's own conclusion, so it does not rest on the 1996 paper alone.

### 5.7 The rendering guard is strong, not total

`DerivedTypeCode` cannot be rendered bare by accident, but `letters_for_matching`
exists and a determined caller can print it. `find_unqualified_codes` is a
regex over prose: it will not catch "she's an I-N-T-J", a code split across a line
break, or a paraphrase. It catches the realistic failure — a template or a language
model writing the bare code inline — and nothing more.

### 5.8 Both modules now have a test file

`backend/tests/test_smart_teams_big_five_and_type16.py`, 15 tests. This section
previously said no such file existed and asked to be deleted the day one did.

What it pins, and why each is there rather than being a restatement of the code:

* **The provenance key, item by item.** `CANONICAL_IPIP50_KEY` encodes the
  published scoring key (§2) as data, and the test asserts every item's domain
  assignment, IPIP number and keying sign against it. The audit in §2 was a
  script someone had to remember to run; this is the same check, enforced.
* **The Neuroticism inversion**, asserted on its own. Items 9 and 19 are the two
  reverse-scored Neuroticism items and therefore our only two *positive*
  Emotional Stability items. Getting that backwards would report every anxious
  respondent as unusually steady — wrong in a way that looks fine in a demo.
* **Reverse-keying arithmetic**, on worked examples through the public scorer: a
  positively-keyed 5 and a reverse-keyed 5 are opposite claims and must cancel;
  agreeing with one and disagreeing with the other must compound.
* **The coin-flip refusal**, parametrized over one, two, three and four
  midpoint domains. This is the regression test for the defect described in
  §5.4: the original guard withheld the work-style portrait only when *all four*
  axes were coin flips, so a single meaningless letter still produced a confident
  paragraph, and a domain at 50.0 versus 50.1 selected substantively different
  portraits of the same person.

Both guards were mutation-checked rather than assumed: flipping the keying on
one item fails the provenance test, and reverting the portrait guard to the
all-four condition fails four tests including the exact midpoint case.

The import-time `assert`s still cover the structural invariants (50 items, ten
per domain, unique ids, 1–50 numbering, four axes, one domain per axis, eight
distinct letters, sixteen work-style entries), and they run earlier than any
test, so they stay.

---

## 6. Rules for future editors

1. **Never label an item `ipip` without checking it against a source in the same
   sitting.** An unverifiable item is `authored`. A smaller verified bank is a
   smaller problem than a mislabelled one.
2. **Do not "fix" the Emotional Stability keying** by substituting authored items.
   That trades the validity this layer exists to provide for a cosmetic balance.
3. **Do not add a nickname for any of the sixteen combinations**, ours or anyone
   else's. §4.2 explains why, and the reason is product, not only legal.
4. **Do not add a way to render bare letters.** If you need to group people, use
   `letters_for_matching` and keep it away from anything a person reads.
5. **Do not let a coin-flip letter become an assertion.** The lowercase rendering
   and the withheld `work_style` are load-bearing, not decoration.
6. **Response-quality thresholds change in one place.** Edit
   `SHARED_RESPONSE_QUALITY_THRESHOLDS`'s inputs in `big_five.py`; do not copy
   numbers into another module.
7. **Do not write a claim into a docstring or into this document that is not true
   in the repository.** Two claims were removed from `big_five.py` during this pass
   for exactly that reason (§2), and both had looked completely reasonable.
