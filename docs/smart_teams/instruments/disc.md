# DISC-style layer — instrument design, scoring, and limitations

- **Status:** authored 2026-08-19 (ST-1). Item bank frozen; thresholds provisional until we have real response data.
- **Code:** `rubik_core/smart_teams/instruments/disc.py`
- **Scope:** the DISC-style layer of the Cubie Work Style Interview — 48 in-house items, deterministic scoring, and a 17-style blend taxonomy we own. The Big Five backbone, motivators, Enneagram and strengths layers are separate modules with their own docs.
- **Plan:** `docs/smart_teams/plan.md` (decisions 1, 2 and the legal guardrails).

---

## 1. What this layer is, and what it must never be

It measures four dimensions of **self-reported working behaviour** — how someone prefers to decide, communicate, absorb change, and handle standards. It does not measure ability, intelligence, potential, or fitness for a role, and it cannot.

The permitted uses are exactly two: helping colleagues work with each other, and staffing project teams. It is **never** an input to performance review, promotion, or compensation. That is not defensive boilerplate. Using a personality instrument as an employment-decision input is a live Title VII / ADA exposure, and using an *unvalidated in-house* one that way would be the worst available version of it — we would be defending a measure with no criterion-validity evidence behind it. Team-fit guidance carries none of that risk, which is why the boundary is worth holding precisely.

`ADVISORY_NOTICE` in the module is the single source of that disclaimer, and `DiscResult.as_dict()` attaches it to every result, so a surface has to actively drop it rather than merely forget to write it. The string lives in code precisely so it cannot drift between surfaces.

Two more contracts inherited from the plan:

- **Self-given data only.** Every input is an answer the person typed or a report they chose to upload. `rubik_core/people/` (UC-47) is the mirror image — it researches *external* meeting attendees from public sources and refuses to profile the company's own staff. Nothing in Smart Teams may ever scrape or infer about an employee.
- **The person publishes, not the system.** Scores stay private to the person and the admins who support Smart Teams. The guide is a draft until its subject approves it, and they can unpublish at any time.

---

## 2. Legal provenance — why we may build this at all

### The argument

1. **The model is public domain.** William Moulton Marston published the four-factor framework in *Emotions of Normal People* (Kegan Paul, Trench, Trubner & Co., London / Harcourt, Brace, New York, 1928). The work is out of copyright and freely readable at the Internet Archive.
2. **Marston never built an instrument and never patented anything.** He described a theory of emotional expression, not an assessment. There is no instrument of his to infringe.
3. **That is precisely why a competitive vendor market exists.** Walter Clarke built the first measurement from it (Activity Vector Analysis, 1956; Clarke and Merenda published validation work in the *Journal of Clinical Psychology*, 1965). John Geier's instrument became Performax → Inscape Publishing → Wiley's product line in the 1970s. TTI, Insights and roughly fifty others followed. Fifty vendors could not have built competing products on a protected model.
4. **Therefore the model is common property and each vendor's instrument is not.** We may implement the model. We may not reproduce anyone's items, report prose, distinctive style names, or marks.

### What that permits and forbids, concretely

| | |
|---|---|
| **Permitted** | Using the four dimensions and the two axes beneath them; writing our own items on them; writing our own style names and style descriptions; citing Marston and the vendor history as history. |
| **Permitted** | Parsing an official report a person uploads themselves (plan decision 1). That is their own result, used with their consent, for their own profile. It is not redistribution of the vendor's instrument. |
| **Forbidden** | Any vendor's item wording, adapted or paraphrased. Any vendor's report prose. Any vendor's distinctive style names. |
| **Forbidden** | Wiley's lowercase-i trademark styling of the acronym. We write **DISC** in full capitals everywhere — code, prose, UI, and this document. The module's one use of the lowercase-i spelling is the sentence naming the mark being avoided; there should never be a second. |
| **Forbidden** | Gallup CliftonStrengths items or the 34 theme names. MBTI items, or any "Myers-Briggs"/"MBTI" product claim (the 16-type layer is labelled "16-type style (derived)" and derives from Big Five scores). TTI report text. |

### Hygiene actually performed

Two of these are authoring statements about how the content was produced; the third is a comparison that was run, and it is dated because it can go stale.

- **Every one of the 48 items was written for this file**, from everyday working situations — reviews, client reports, findings, all-hands, mid-week re-prioritisation. Nothing was adapted from a vendor bank, and the workplace specificity is itself part of the defence: these items read like real work, not like a generic commercial instrument.
- **No vendor report prose** informed the style descriptions. They were written from the dimension definitions plus how these behaviours actually show up at work.
- **The 17 style names were authored here, and read against two lists on 2026-08-19.**
  - *Vendor vocabularies*, as far as we could enumerate them: Wiley's letter-pair styles and the older Inscape "classic pattern" names (Achiever, Agent, Appraiser, Counselor, Creative, Developer, Inspirational, Investigator, Objective Thinker, Perfectionist, Persuader, Practitioner, Promoter, Result-Oriented, Specialist, and the shift patterns), TTI's names (Conductor, Persuader, Promoter, Relater, Supporter, Coordinator, Analyzer, Implementor), and Insights' colour names. No name on that list is one of ours.
  - *The sibling instrument layers*, because all five render into one guide for one person: `enneagram.py` type labels, `strengths.py` theme names, `motivators.py` driver names, `big_five.py` domain labels. **This read found three collisions.** Three of our style names were also naming an Enneagram type — Front Runner against The Front-Runner, Standard Setter against The Standard-Setter, Steady Hand against The Steady Hand — and Steady Hand was additionally a `strengths.py` theme name, character for character. One guide could have printed that string three times meaning a style, a type and a theme. All three were resolved by renaming *there*, not here: this layer's names shipped first, so the newer layers moved. Nothing in `disc.py` changed. Re-running the comparison after those renames, none of our 17 names is now an exact match for any name in any sibling layer.
  - Deliberate near-misses survive and are not a defect: Standard Setter still sits near `strengths.py`'s Standard Keeper, and *-Builder*, *-Keeper* and *Straight-* recur across layers because that is what plain English about work is made of. `docs/smart_teams/instruments/strengths.md` §2 catalogues the ones that were weighed and kept, so the next editor knows they were considered rather than missed.

An earlier version of this section claimed a collision check that had not been run, and asserted "no collisions" on the strength of it — which is how one string reached three layers at once. Neither list is checked by code in this module, and the vendor half cannot be: a test would have to carry the vendor lists, which is the intellectual property this whole section exists to keep out of the repository. The cross-layer half *is* mechanical, and is now pinned by `backend/tests/test_smart_teams_instrument_names.py`, which asserts the DISC, Enneagram, strengths and motivator name sets are pairwise disjoint (folding case, a leading "The", and hyphens) and refuses any name matching Gallup's 34 CliftonStrengths themes, the VIA 24, or the 16personalities nicknames. Renaming a style therefore fails a test rather than depending on the next editor's memory. The vendor half of the *item-wording* read still cannot be automated, for the reason above.

---

## 3. Sources consulted

Grounding for the dimension definitions, the provenance argument, and the validity discussion in §9. Source quality is labelled honestly, because it varies a lot and the weak ones should not be leaned on.

| Source | URL | Used for | Quality |
|---|---|---|---|
| Marston, *Emotions of Normal People* (1928), full text | https://archive.org/stream/emotionsofnormal032195mbp/emotionsofnormal032195mbp_djvu.txt | Primary source. His four responses (dominance, compliance, submission, inducement) and the two variables beneath them: whether the motor stimulus is *antagonistic* or *allied* to the self, and whether it is superior or inferior in strength. Confirms the publisher/date. | Primary, public domain |
| Internet Archive catalogue record | https://archive.org/details/emotionsofnormal0000mars | Bibliographic confirmation | Primary |
| Jones, C. S. & Hartley, N. T. (2013). Comparing correlations between four-quadrant and five-factor personality assessments. *American Journal of Business Education*, 6(4), 459–470. | https://eric.ed.gov/?id=EJ1054970 | The peer-reviewed anchor for "four-quadrant and five-factor instruments are complementary rather than disconnected"; compares a four-quadrant instrument against the NEO-FFI. | Peer-reviewed (small sample) |
| Dekker, B. & van Thiel, E. (2021). *Concurrent validation study: DISC vs. Big Five* | https://www.123test.com/content/disc-vs-big-five-123test.pdf | The concrete DISC × Big Five correlation matrices in §4, incl. facet level (9,000+ respondents who took both instruments) | **Vendor technical report, not peer-reviewed.** Large sample and transparent method, but self-published by an instrument vendor. Treated as indicative, not authoritative. |
| Wikipedia, *DISC assessment* | https://en.wikipedia.org/wiki/DISC_assessment | Vendor lineage and dates (Clarke 1956, Clarke & Merenda 1965, Geier/Performax/Inscape/Wiley), the never-patented status, and the validity criticism | Tertiary, used only for facts checkable elsewhere |
| Wikipedia, *International Personality Item Pool* | https://en.wikipedia.org/wiki/International_Personality_Item_Pool | The public-domain backbone the sibling Big Five layer uses | Tertiary |
| discprofile.com, *History of DISC* / *Science behind DISC* | https://www.discprofile.com/what-is-disc/history-of-disc | Practitioner account of the lineage, read specifically to know what vendor vocabulary to *avoid* | Vendor marketing |

Two things deliberately **not** consulted, to keep the provenance clean: any vendor's live assessment (we did not take one and reverse-engineer items), and any vendor's sample report.

---

## 4. The four dimensions as we define them

Modern generic labels, because Marston's originals do not survive contact with a 2026 workplace — "Submission" reads as an insult, which is why the field relabelled it. `MARSTON_LABELS` keeps the originals for the methodology footnote.

| | Ours | Marston (1928) | What the items actually ask about |
|---|---|---|---|
| **D** | Dominance | Dominance | Pace and directness. Making the call, saying the uncomfortable thing, changing route mid-plan, pushing into resistance rather than around it. |
| **I** | Influence | Inducement | Outward energy. Thinking out loud, working a problem by talking it through, persuading, choosing people contact over solitary focus. |
| **S** | Steadiness | Submission | Cadence and consistency. Predictable workload, advance notice before change, sustainable pace, the health of the working relationship. |
| **C** | Conscientiousness | Compliance | Standards and evidence. Seeing the numbers yourself, working a documented process, being right rather than fast. |

The two axes beneath them are Marston's (is the environment allied or antagonistic; am I stronger than it or is it stronger than me) and map onto what the interpersonal-circumplex literature calls agency and communion: D and I are the high-agency half, I and S the high-communion half.

**This is why the blend carries the information and a single letter carries very little.** A high-D paired with high C is a different colleague from a high-D paired with high I — same letter, opposite handling instructions. The guide is written off the blend.

### Cross-walk to the Big Five backbone

From the 123test concurrent-validation matrices (factor level, n ≈ 9,000; caveat in §3):

| DISC | O | C | E | A | N |
|---|---|---|---|---|---|
| D | .19 | .00 | **.34** | **−.39** | −.11 |
| I | .16 | −.06 | **.54** | .07 | −.30 |
| S | −.17 | .07 | −.28 | **.43** | .01 |
| C | −.20 | −.02 | **−.62** | −.01 | **.39** |

Three consequences we act on:

1. **D and I both load primarily on Extraversion**, so they are not cleanly independent. Our forced-choice bank contests D against I four times specifically to force a preference the Likert bank would let a person endorse twice.
2. **Openness has no DISC counterpart** (every |r| ≤ .20). The DISC layer therefore says nothing about intellectual curiosity or appetite for novelty — that is the Big Five layer's job, and the Team Builder should read it there, not infer it here.
3. **DISC's C correlates with Extraversion at −.62 and with Big Five Conscientiousness at −.02.** The letter collision is a genuine trap: our C is much more "reserved and evidence-led" than "organised". Anyone joining these two layers must not treat the two Cs as the same construct.

---

## 5. Item-authoring approach

### Two banks, on purpose

48 items: 24 Likert-agreement statements and 24 forced-choice pairs.

| | Likert bank | Forced-choice bank |
|---|---|---|
| Items | 24 (6 per dimension) | 24 pairs |
| Type | **Normative** — scores comparable between people | **Ipsative** — ranks the four dimensions *within* one person only |
| Buys us | Cross-person comparability, which is what the Team Builder needs | Immunity to the person who agrees with every statement put in front of them |
| Costs us | Vulnerable to acquiescence and social desirability | Cannot support between-person comparison on its own |
| Weight | 0.6 | 0.4 |

Blending them is the whole point: comparability from the normative half, with an acquiescence brake bolted on from the ipsative half. Neither bank alone would do.

### Keying

Per dimension: **6 items, 4 positively keyed and 2 reverse-keyed** — 8 reverse-keyed items overall. A reverse item is worded so that *disagreeing* indicates the dimension ("I would rather hand a hard call up to my manager than make it myself" is reverse-keyed D), and its raw response is flipped before it enters the sum.

Reverse items exist to break the rhythm of a respondent who has stopped reading. They are load-bearing twice over: once in scoring, once in `check_response_quality`. Converting one to positive keying to "simplify the bank" would quietly disable the inconsistency index.

**All eight are paired** with a positively keyed near-opposite on the same dimension in `ANTONYM_PAIRS` — e.g. `disc_c_02` ("I would rather be late and right than early and approximately right") against `disc_c_05` ("I am comfortable shipping at eighty percent and correcting it later"). After reverse-scoring, someone who read the questions gives both members of a pair a similar score. The mean absolute gap across the eight pairs is our inconsistency index (§8).

### Writing rules applied to every item

1. **First person, present tense, plain workplace English.** No jargon, no psychology vocabulary, nothing needing a glossary.
2. **A recognisable work situation** where possible — a messy review, a client report, a finding, an all-hands, a mid-week re-prioritisation.
3. **Behaviour, not virtue.** "When a plan stops working, I would rather change route than finish the original plan" describes what someone does. "I am a decisive leader" invites self-flattery.
4. **No item where one answer is obviously the good one.** Both sides of every forced-choice pair must be genuinely appealing to somebody, or the item measures social desirability instead of preference.
5. **One idea per item.** Double-barrelled items are unscoreable — you never learn which half was endorsed.
6. **Nothing touching a protected characteristic**, health, family status, or anything outside working preference.

### Ordering is part of the instrument

Scoring is order-independent; the quality checks are not. Do not re-sort these tuples.

- `LIKERT_ITEMS` interleaves dimensions so **no two consecutive items share a dimension**, and spreads the 8 reverse-keyed items through the run. A genuine profile essentially cannot produce a long run of identical answers against this ordering.
- `FORCED_CHOICE_PAIRS` covers all **6 dimension combinations 4 times each**, so every dimension is contested exactly **12** times and none gets an easy run. Consecutive pairs contest different combinations, and which dimension sits in slot `a` alternates within every combination — that is what turns "clicked down the left column" into a detectable position-bias signal instead of a clean-looking profile.

### The interview page never sees the key

`interview_payload()` returns item ids, prompt text and the five anchor labels — and deliberately withholds dimension and keying. A respondent who can see which answer scores which dimension is answering a different instrument. Nothing asserts this today; it holds only because the function builds its dictionaries field by field rather than dumping the dataclasses. Adding one line to a payload is all it would take to leak the key, which is why §10 carries an assertion for it.

---

## 6. Scoring math

Pure, deterministic, **no LLM anywhere**. Identical answers always produce an identical profile; the LLM's only job downstream is writing prose *from* this output, and it can never change a number in it.

### Per dimension

```text
likert_pct  = (Σ scored responses − n·1) / (n · (5 − 1)) · 100      n = answered items for this dimension
              where a reverse-keyed response r is scored as (1 + 5 − r)

choice_pct  = contests won by this dimension / contests it appeared in · 100

intensity   = round( 0.6 · likert_pct + 0.4 · choice_pct )   clamped to 0–100
```

Both components are kept on the result (`DimensionScore.likert_raw`, `.likert_percent`, `.choice_wins`, `.choice_percent`) so that a person asking "why is my C an 80" can be shown the arithmetic rather than a verdict.

### Renormalize, don't zero

If one bank contributed nothing for a dimension, **the other takes the full weight** — the same rule as the X-Ray churn scorer, for the same reason: "we did not ask" must never quietly average in as "low on that axis". A partially answered dimension is prorated against the range its *answered* items could span, not penalised for the blanks.

When *neither* bank has anything for a dimension there is no surviving weight to move it onto, and the rule holds there too: `intensity` and `band` come back as **`None`, not 0**. A zero would be the exact mistake the rule exists to prevent, one step later — it enters the ranking as "low on that axis", and the three dimensions we did measure get sorted above a number nobody supplied. `style_for_scores` sees the `None` and names no style at all (§6, "Score → style"). The per-dimension scores and the quality flags still come back, so the page can show what was answered and what is missing.

In practice this is unreachable without also tripping `MIN_COMPLETION_SHARE`: emptying one dimension means 6 Likert items and the 12 pairs that contest it, which is 18 of 48 blank, and completeness fails at 62%. The `None` is the belt to that braces — a retake flag tells the caller the profile is suspect, but only the `None` stops a fabricated 0 reaching the ranking in the meantime.

### Worked example

A respondent who answered all 48 items in 13 minutes:

| | Likert raw (n=6) | likert_pct | Contests won | choice_pct | Intensity | Band |
|---|---|---|---|---|---|---|
| D | 27 | 87.5 | 11 / 12 | 91.7 | **89** | Very high |
| I | 17 | 45.8 | 4 / 12 | 33.3 | **41** | Moderate |
| S | 15 | 37.5 | 0 / 12 | 0.0 | **22** | Low |
| C | 26 | 83.3 | 9 / 12 | 75.0 | **80** | Very high |

D: `(27 − 6)/(6·4)·100 = 87.5`; `11/12·100 = 91.7`; `0.6(87.5) + 0.4(91.7) = 89.2 → 89`.

Ranking D > C > I > S. Primary margin 9 (below `SINGLE_STYLE_GAP`), so it is a blend; secondary margin 39, so the second slot is unambiguous → **D+C "Case Builder", confidence high**.

### Bands

Words so the guide never has to print a bare number: **Low** < 25, **Moderate** < 45, **High** < 70, **Very high** ≥ 70.

Note these are **absolute positions on our own scale, not percentiles.** We have no norm sample, so "High" means "endorsed most of these items", not "higher than most colleagues". §9.

### Score → style

`style_for_scores` is pure, and resolves in this order:

1. **Unscorable** — any dimension came back `None`: no style is named (`assignment.style` is `None`), no ranking, no margins, confidence low. See "Renormalize, don't zero" above.
2. **Flat profile** — the whole set spans *fewer* than `LEVEL_PROFILE_SPREAD` (12) points, i.e. 11 or less: return All-Rounder, confidence low. We say "nothing is leading" rather than promoting noise into a blend. A spread of exactly 12 is a blend.
3. **Single-dominant** — leader clears the runner-up by ≥ `SINGLE_STYLE_GAP` (15): the secondary adds nothing worth naming. Confidence high at a margin ≥ 25, else moderate.
4. **Blend** — the top two, in order. Confidence from the *secondary* margin (gap from second to third): high ≥ 12, moderate ≥ 5, else low. A 1-point gap to third means the blend was nearly a different one, and the guide should hedge.

Ties break on `DIMENSIONS` order (D, I, S, C). Arbitrary, but it must be fixed — otherwise two runs of the same interview could disagree.

`score_disc` finally **floors style confidence to "low" whenever the quality checks recommend a retake.** The style is still returned, because the person should be able to see what their answers produced, but nothing downstream may describe it confidently.

---

## 7. The blend taxonomy (ours)

17 styles: 4 single-dominant, 12 **ordered** blends (D+C and C+D are different people, so order is part of the answer), and one honest label for a flat profile.

| Key | Name | Identity in one line |
|---|---|---|
| D | **Straight Line** | Goes at the objective directly and expects the shortest path to be the right one. |
| I | **Open Floor** | Thinks out loud and moves work by getting people talking about it. |
| S | **Steady Hand** | Holds a sustainable pace and is the reason things stay finished after they ship. |
| C | **Fine Print** | Would rather be right than quick, and is usually why the work survives review. |
| D+I | **Front Runner** | Drives hard and brings people along at the same time. |
| D+S | **Quiet Force** | Pushes as hard as anyone but without noise; nobody gets run over. |
| D+C | **Case Builder** | Decides fast, but only on ground that will hold. |
| I+D | **Closer** | Persuades toward a decision, and will not leave the room without one. |
| I+S | **Team Glue** | Reads the room, keeps people connected, notices who has gone quiet. |
| I+C | **Pitch and Proof** | Sells the idea and insists the numbers behind it are right. |
| S+D | **Backstop** | Holds the line quietly, and pushes hard when something genuinely matters. |
| S+I | **Open Door** | Steady and approachable; the reason new people get up to speed. |
| S+C | **Process Keeper** | Keeps the system running and notices drift before it becomes a finding. |
| C+D | **Standard Setter** | Sets the bar and holds people to it, willing to be unpopular about it. |
| C+I | **Translator** | Gets it right, then makes it make sense to everyone else. |
| C+S | **Groundwork** | Builds the foundation carefully; the reason nothing collapses later. |
| — | **All-Rounder** | No single style dominates; adapts to what the situation needs. |

Each style carries six fields, and this is the content the "How to work with {NAME}" generator draws on:

- `identity` — one line
- `communication` — 3 concrete preferences
- `energized_by` / `drained_by` — 2 each
- `under_stress` — what this person does when it goes wrong
- `feedback` — how to give it to them so it lands

**The editorial bar: if a line would be equally true of any of the 17 styles, it does not belong.** Horoscope filler is what makes a profile feel accurate and be useless. So the S entry says "a change announced Monday for Thursday lands; one announced Thursday for Thursday does not", and the C+S feedback entry says "ask questions rather than making pronouncements, then give them a day". Those are actionable before lunch; "communicates well with others" is not.

`under_stress` is deliberately unflattering, and that is a considered choice. A guide that only lists strengths cannot help a colleague who is currently struggling with this person, which is the case where a guide is most needed. It stays inside the advisory boundary because it describes *behaviour under pressure that the person self-reported*, not a deficiency, and the person approves the text before anyone reads it.

---

## 8. Response-quality checks

The premise: **a rushed interview that produces a confident, wrong profile is worse for us than no profile at all.** The person then walks around with a company-visible guide describing someone else, and colleagues act on it. So a suspect interview gets flagged for retake rather than silently scored.

Four indices, all from the careless-responding literature, all computed **within-person** — we score one interview at a time, so there is no sample correlation matrix to mine and no person-total correlation to compute.

| Index | Constant | Catches | Severity |
|---|---|---|---|
| Longstring — longest run of identical Likert answers (a blank breaks the run) | `STRAIGHT_LINE_RUN = 8` | Straight-lining, and nothing else | retake |
| Intra-individual response variability — pstdev of Likert answers | `MIN_RESPONSE_VARIABILITY` — **not a number of ours**: it is `big_five.MIN_RESPONSE_SD`, 0.40 as this is written | The near-straight-liner longstring cannot see | warn |
| …with a response-range companion — highest minus lowest answer | `MIN_RESPONSE_RANGE = 2` (needs `MIN_ITEMS_FOR_RANGE_CHECK = 12` items answered) | A clean alternation between two *adjacent* anchors, which sits at pstdev **exactly 0.50** and slips past the line above while showing a longstring run of 1 | warn (same flag — one behaviour cannot cost two warnings) |
| Antonym-pair discrepancy — mean abs gap across the 8 pairs after reverse-scoring | `MAX_PAIR_DISCREPANCY = 1.75`; `SEVERE_PAIR_DISCREPANCY = 3` in `MAX_SEVERE_PAIRS = 3`+ pairs | Agreeing with a statement and its opposite — the signature of not reading | warn / retake |
| Forced-choice position share | `MAX_POSITION_SHARE = 0.9` (needs `MIN_CHOICES_FOR_POSITION_CHECK = 12` answered) | Clicking down one column | warn |
| Completeness | `MIN_COMPLETION_SHARE = 0.75`; `TOLERATED_BLANKS = 4` | Not enough answered to place a style | retake / warn |
| Pace, when the caller has it | `MIN_SECONDS_PER_ITEM = 2.0` | Nobody read the questions | retake |

`retake_recommended` is true on **any retake flag, or on two independent warnings** — two weak signals pointing the same way is the common shape of a genuinely careless interview, where no single index quite trips.

### Shared with the Big Five layer, and where we diverge

`big_five.py` states the rule above its own thresholds: the DISC bank scores the *same sitting*, so it must reuse those numbers rather than invent its own, or one careless interview comes out flagged on one instrument and clean on the other and whichever a reviewer sees first becomes the truth. This layer complies. Both directions of the contract are written down in both places — `disc.py` records each divergence at the constant, and this table is the same information for a reader who starts here.

| Index | This layer | Why |
|---|---|---|
| Response SD | **Imported.** `MIN_RESPONSE_VARIABILITY = big_five.MIN_RESPONSE_SD` — the constant itself, not a copy of its value | Same statistic on the same 1–5 scale over a mixed-keying bank. A copy would drift, and the drift would show up as two layers disagreeing about one person |
| Longstring | **Same rule, re-derived.** `STRAIGHT_LINE_RUN = 2 × 4 dimensions = 8`, where big_five's `MAX_IDENTICAL_RUN` is 2 × 5 domains = 10 | The rule is "two full cycles of the interleave", not the number 10. Copying 10 into a bank of 24 items would be a much weaker check than the one big_five is applying to 50 |
| Keying / antonym gap | **Deliberately looser.** `MAX_PAIR_DISCREPANCY = 1.75` against big_five's `MAX_KEYING_GAP = 1.5` | Structural, not a second opinion. big_five's index is the distance between two *means* (all plus-keyed items in a domain against all minus-keyed), where independent noise cancels. Ours averages 8 absolute per-pair gaps, which cannot cancel — ordinary item-to-item noise only ever pushes it up. Same scale, different footing; aligning them would flag careful people |
| Completeness | **Whole bank, not per dimension.** `MIN_COMPLETION_SHARE` against big_five's `MIN_ANSWERED_PER_DOMAIN` | Half of this bank is forced-choice pairs and every pair contests two dimensions at once, so "items answered for this dimension" is not the same countable quantity here. The per-dimension protection is in the scoring instead: prorate what was answered, and return `None` for a dimension with nothing at all |
| Neutral overuse, acquiescence | **Absent.** No counterpart to `MAX_NEUTRAL_SHARE` or `MAX_ACQUIESCENCE` | The ipsative bank is this layer's acquiescence brake — a forced choice cannot be answered by agreeing with everything. A neutral-heavy respondent lands flat and is routed to the hedged All-Rounder rather than given a confident blend, which is the outcome the index would have bought us |

### Two calibration decisions worth preserving

Both look like slack until you work out what they catch.

- **The response-range companion exists because the variability threshold alone cannot do the job its own docstring describes.** A perfect alternation between two neighbouring anchors has a population standard deviation of exactly 0.50 — clear of the 0.40 floor — and a longstring run of 1. It is invisible to both. Requiring the answers to span more than two adjacent anchors across 24 interleaved items, 8 of them worded against their own dimension, is what actually catches it.
- **`TOLERATED_BLANKS` exists because without it a single skipped item costs a full warning**, and two warnings are a retake. One blank plus one soft index would send an honest interview back. Scoring prorates around a few blanks anyway.

### What these checks have and have not been measured against

**Nothing. No false-positive rate, no detection rate, no harness.** An earlier version of this section published per-respondent-type detection percentages and attributed them to an "ST-1 audit harness". There is no such harness in the repository, those figures were never computed, and they have been removed rather than rounded off or hedged. Fabricated numbers are worse than no numbers here: the whole point of §9 is that a reader should be able to tell exactly how much evidence sits behind this instrument, and a table of invented percentages destroys that for every real number next to it.

What can be said without measuring anything is the arithmetic, which is reproducible by hand from the constants:

- **Uniform random clicking is invisible to the antonym-pair index, by construction.** Two independent draws from a uniform 1–5 scale differ by (5² − 1) / (3 · 5) = **1.6** on average, which sits under `MAX_PAIR_DISCREPANCY = 1.75`. Reverse-scoring does not change that — flipping one draw leaves it uniform. So this is not a threshold that needs tightening; it is a threshold that 8 pairs cannot resolve. §9 limitation 7.
- **A perfect alternation between two adjacent anchors sits at pstdev exactly 0.50** (equal counts of two values one point apart), above the 0.40 floor, with a longstring run of 1 — which is why the range companion exists.
- **A dimension can only go fully unanswered by leaving 18 of 48 items blank**, which fails `MIN_COMPLETION_SHARE` at 62%. So the unscorable branch in §6 always arrives with a retake flag beside it.

Building the harness is item 3 on the revisit list, and it is worth building for the false-positive side specifically: the risk this instrument actually runs is not missing a careless respondent, it is sending an honest one back to redo a 48-item interview. Simulated respondents can bound that, and simulated respondents are all we will have until real interviews exist. Whatever it prints goes here with the date it was run and the seed it used — and §9 limitation 7 already warns against tuning thresholds against a simulation, which applies to the harness's own output as much as to anyone's intuition.

---

## 9. Known limitations

Read this section before building anything that consumes the output.

1. **It is self-report, and that is all it is.** It measures how someone describes their own working preferences on the day they took the interview. It does not observe behaviour, and it cannot detect someone describing who they would like to be. This is a ceiling on the whole layer, not a fixable defect.
2. **Behaviour is situational.** DISC-style preferences shift with role, tenure, team, and how much pressure someone is under. A profile taken during a brutal audit cycle is not the same profile as one taken in a quiet month. Retakes create a new version rather than overwriting, precisely so the drift is visible instead of silent.
3. **The four-quadrant model is not psychometrically clean, and never was.** The four dimensions were proposed theoretically, not derived by factor analysis; independent evaluations generally do not recover them as four independent factors; D and I both load primarily on Extraversion (§4); a 2013 German evaluation found one commercial DISC instrument met reliability standards but largely failed validity requirements. This is why the plan puts the **public-domain Big Five (IPIP) layer** underneath as the scoring backbone and treats the DISC layer as the *communication vocabulary* — it is the language many teams already speak, and executives often have official results in it, which is what makes it worth carrying at all.
4. **No norms, so no percentiles.** We have no norm sample. `intensity_band` reports absolute position on our own scale — "High" means "endorsed most of these items", not "higher than most colleagues". Nothing may present these as percentiles until a norm sample exists, and even then it will be an internal norm of a few dozen people.
5. **No reliability or validity evidence yet, at all.** No internal-consistency figures, no test-retest, no convergence against the ET's official DISC results. The last of those is the cheapest and most valuable first study, and ST-5 (official-results upload) is what makes it possible — a handful of paired cases would tell us whether this layer is measuring the same thing at all.
6. **The forced-choice half is ipsative.** It ranks the four dimensions inside one person and cannot support between-person comparison on its own. The 60/40 blend keeps overall comparability, but 40% of every intensity carries this caveat, and any future statistic computed *across* people needs to account for it rather than treating intensities as fully normative interval data.
7. **Uniform random clicking is not detected.** It has high variability, a wide range, no position bias, and an expected antonym-pair gap of 1.6 against a threshold of 1.75 — arithmetic, not a measurement: the mean absolute difference of two independent draws from a uniform 1–5 scale is (5² − 1) / (3 · 5). A consistent honest respondent should land well below that, but nobody has measured where honest respondents actually land, so the overlap between the two distributions is unquantified and 8 pairs is coarse resolution for separating them. Tightening the threshold trades an unknown amount of detection against an unknown amount of false alarm on genuinely flat honest profiles, which is not a trade anyone can currently price. The real defence is the elapsed-time check. Do not "fix" this by tuning against a simulation; fix it with real response data, or with more antonym pairs.
8. **Thresholds in §8 are judgement calls.** The literature is explicit that these indices have no universal cutoffs. Ours are set from the shape of this bank and are deliberately loose — the check exists to catch the interview nobody actually took, not to second-guess people who answered honestly and consistently.
9. **English only, and culturally situated.** The items assume US-workplace norms about directness, meetings, and disagreement. There is no translation and no cross-cultural validation.
10. **Not for selection, promotion, compensation, or discipline.** Restating it here because it is a *limitation*, not just a policy: this instrument has no criterion-validity evidence, so it could not support such a decision even if policy allowed it. Anyone who proposes wiring profile data into a review workflow should be pointed at this line and at §1.

---

## 10. Revisit list

Ordered by value, for whoever picks this up after ST-4/ST-5:

1. **Convergence against the ET's official DISC results** (needs ST-5). The one study that would tell us whether this layer measures what it claims.
2. **Internal consistency per dimension** once ~30 interviews exist. If a dimension's six items do not hang together, the bank is wrong, not the person.
3. **A response-quality harness, and then §8's empty measurement section.** Synthetic respondents across the honest range and the careless patterns, seeded and re-runnable, reporting how often each index fires. False-positive rate first: sending an honest person back to redo 48 items is the failure this instrument is actually likely to commit. Whatever it prints goes into §8 with its date and seed — and §8 stays empty until it does.
4. **Re-tune §8 thresholds against real response-time and answer distributions** once interviews exist. The current numbers are set from the shape of the bank, not from data.
5. **More antonym pairs** if random-clicking detection matters (limitation 7). Eight is the minimum that produces a usable index; sixteen would produce a good one.
6. ~~A name-disjointness regression test across all five instrument layers~~ — **done**, `backend/tests/test_smart_teams_instrument_names.py`. The vendor half of the item-wording read still cannot be automated, for the reason §2 gives.
7. **An assertion that `interview_payload()` never carries `dimension` or `keying`** (§5). It is one field away from leaking the scoring key, and nothing currently notices.
8. **Retest-drift reporting** — surface to the person, not to anyone else, how their profile moved between versions.
