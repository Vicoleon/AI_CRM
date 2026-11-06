# Motivators & Enneagram — sources, provenance, and design

**Ticket:** ST-1 (instrument design) · **Modules:** `rubik_core/smart_teams/instruments/motivators.py`,
`rubik_core/smart_teams/instruments/enneagram.py` · **Researched:** 2026-08-19

Two layers of the Cubie Work Style Interview are covered here: the six value
orientations ("motivators") and the nine Enneagram types. They are documented
together because they share a legal shape — a public, unowned *model* with
proprietary *instruments* built on top of it — and because they sit at opposite
ends of the evidence range in this feature. Motivators is thin but uncontested.
The Enneagram is thin *and* actively criticised, and is shipped anyway as a
deliberate, recorded product decision. See [Limitations](#5-limitations).

The DISC-style layer's own provenance argument lives in `disc.md`; the Big Five
backbone's in the IPIP notes. `ADVISORY_NOTICE` in `disc.py` is the single
source of truth for the in-product disclaimer and both modules here import it
rather than restating it.

---

## 1. Sources

### Motivators — the value framework

| Source | URL |
|---|---|
| Eduard Spranger — biography and the six value types from *Lebensformen* (1914) / *Types of Men* (trans. Pigors, 1928) | https://www.encyclopedia.com/humanities/encyclopedias-almanacs-transcripts-and-maps/spranger-franz-ernst-eduard-1882-1963 |
| Eduard Spranger (overview, dates, works) | https://en.wikipedia.org/wiki/Eduard_Spranger |
| Spranger's value-oriented types — summary of the six orientations | https://psychology.town/personality-theories/sprangers-value-oriented-personality-types-overview/ |
| Allport-Vernon-Lindzey *Study of Values*, fourth edition (Kopelman, Rovenpor & Guan, 2003) — the proprietary instrument line | https://www.sciencedirect.com/science/article/abs/pii/S0001879102000477 |
| TTI Success Insights Motivators / Workplace Motivators — the commercial product and its axis naming (Utilitarian, Theoretical, Aesthetic, Social, Individualistic, Regulatory) | https://www.ttisuccessinsights.com.au/profiling-tools/motivators-profile |
| TTI "6 Attitudes" — confirms the Traditional/Regulatory and Utilitarian/Economic label pairs | https://blog.hptbydts.com/in-a-nutshell-6-attitudes |

### Motivators — psychometric evidence

| Source | URL |
|---|---|
| Hanel, Litzellachner & Maio (2018), *Frontiers in Psychology* — "An Empirical Comparison of Human Value Models". Study of Values reached max α = 0.26 and was dismissed from all further analyses | https://pmc.ncbi.nlm.nih.gov/articles/PMC6167453/ |

### Enneagram — framework history

| Source | URL |
|---|---|
| Enneagram of Personality — history, Gurdjieff's symbol, Ichazo, Naranjo, and the criticism section | https://en.wikipedia.org/wiki/Enneagram_of_Personality |
| Integrative9 — history of the Enneagram's origins | https://www.integrative9.com/enneagram/history/ |
| David Daniels — history of the Enneagram as we know it today | https://drdaviddaniels.com/history-of-the-enneagram-we-know-it-today/ |

### Enneagram — proprietary instruments and content

| Source | URL |
|---|---|
| The Enneagram Institute, Terms of Use — RHETI, type descriptions and site content declared proprietary; THE ENNEAGRAM INSTITUTE® and ENNEATHOUGHT® registered marks | https://www.enneagraminstitute.com/terms-of-use/ |
| About the RHETI® — the commercial Riso-Hudson instrument | https://www.enneagraminstitute.com/rheti/ |

### Enneagram — psychometric criticism

| Source | URL |
|---|---|
| Hook, Hall, Davis, Van Tongeren & Conner (2021), *Journal of Clinical Psychology* — "The Enneagram: A systematic review of the literature and directions for future research". 104 independent samples | https://pubmed.ncbi.nlm.nih.gov/33332604/ · https://onlinelibrary.wiley.com/doi/abs/10.1002/jclp.23097 |
| Hook et al. (2021), full text PDF | https://static1.squarespace.com/static/5b9488d731d4df9d904e6031/t/61e04134e793a074ccf37276/1642086709265/the+enneagram+a+systematic+review+of+the.pdf |
| Norcross, Koocher & Garofalo (2006), *Professional Psychology: Research and Practice* — "Discredited psychological treatments and tests: A Delphi poll". 101 doctoral-level panellists; Enneagram for personality assessment scored 4.14 (SD 0.59) in round 2 on a 1–5 discredited scale | https://www.semanticscholar.org/paper/Discredited-psychological-treatments-and-tests:-A-Norcross-Koocher/cce72f4138d202cacf009a4a98313ce47ba86a7b |

---

## 2. Motivators — provenance and the legal argument

**The model is public domain.** Eduard Spranger set out six value orientations
in *Lebensformen* (Halle, 1914; revised 1921), translated into English by Pigors
as *Types of Men* (1928). Spranger died in 1963 and the work is long out of
copyright. He described a value framework; he never built an assessment from it.

**Everything built on it since is not ours.** Vernon and Allport turned the
framework into the *Study of Values* in 1931; it ran through Allport-Vernon-Lindzey
revisions to a fourth edition in 2003, and TTI Success Insights built the
commercial Motivators / Workplace Motivators report on the same six axes. Those
are proprietary instruments — proprietary items, scales, norm tables, and report
prose. Parsing a report an employee uploads about themselves is fine (their
results, their consent, their profile). Reproducing vendor items or prose is not.

**So we take the framework and author everything else.** Concretely, in
`motivators.py`:

- The six labels in `MOTIVATOR_NAMES` are ours: Practical Return, Understanding,
  Human Impact, Standing and Autonomy, Guiding Framework, Form and Craft.
- `MOTIVATOR_SPECS` — definitions, high reads, low reads, and the
  in-a-priority-fight lines — is written in-house.
- All 30 items in `LIKERT_ITEMS` are written in-house.
- `SPRANGER_LABELS` retains Spranger's original axis names (Economic,
  Theoretical, Social, Political, Religious, Aesthetic) **for the methodology
  footnote and citation only**. They are not user-facing: "political" reads as
  office politics and "religious" reads as faith, neither of which is the axis.
  Every later instrument relabelled them for the same reason.

**Collision check.** Our six labels were checked against the vendor
vocabularies for these axes — TTI's Utilitarian / Theoretical / Aesthetic /
Social / Individualistic / Regulatory naming and its separate 12 Driving Forces
names, and the Study of Values' use of Spranger's originals. No label collides.

**Which terms are whose — the exact version.** Three vocabularies are in play
and it matters that the code and this doc say the same thing about them, because
"Spranger's six axes" is an easy claim to make and only partly true of what is
in the module:

| Vocabulary | Where it lives | Whose it is |
|---|---|---|
| Economic, Theoretical, Social, Political, Religious, Aesthetic | `SPRANGER_LABELS` | Spranger's own, in the Pigors translation. Citation and methodology footnote only — never user-facing. |
| `economic`, `theoretical`, `social`, `aesthetic` | four of the six `Motivator` enum keys | Spranger's own terms, reused verbatim as identifiers. |
| `individualistic`, `traditional` | the other two `Motivator` enum keys | **Ours, not Spranger's.** They stand in for his *political* and *religious*, which is also what commercial instruments settled on — the sources in §1 have TTI Success Insights publishing those same two axes as Individualistic and Regulatory / Traditional. |
| Practical Return, Understanding, Human Impact, Standing and Autonomy, Guiding Framework, Form and Craft | `MOTIVATOR_NAMES` | Ours. The display layer, freely rewordable. |

So the enum keys are **not** uniformly Spranger's, and neither the module nor
this doc describes them that way. Two of the six are common single English words
naming an axis; they carry none of any vendor's items, scales, norm tables, or
report prose, and nothing of that kind is reproduced or paraphrased in
`motivators.py`. The keys are nonetheless stable — they are the JSON keys under
`st_profiles`, so changing one is a migration rather than an edit.

---

## 3. Enneagram — provenance and the legal argument

**The framework is not owned by a single party.** Gurdjieff brought the
nine-pointed symbol west from around 1915, but taught it as a process model, not
a typology. Oscar Ichazo first mapped psychological meaning onto its nine points
at the Arica School from 1968, and Claudio Naranjo developed and taught the
types from there; many authors then popularised them independently. There is no
single rights-holder in the nine-type structure, the wings, or the three
centres.

**Specific instruments and publications are owned, emphatically.** The RHETI is
The Enneagram Institute's copyrighted, trademarked product, and EI's Terms of
Use declare its tests, type descriptions, and site content proprietary, with
THE ENNEAGRAM INSTITUTE® and ENNEATHOUGHT® as registered marks. The
single-word type names in widest circulation were authored by Riso and Hudson
and are proprietary to EI along with their descriptions.

**So, again, framework only.** In `enneagram.py`:

- The nine labels in `TYPES` are ours: The Line-Holder, The Supporter, The
  Scorekeeper, The Original, The Quiet Expert, The Risk-Scanner, The
  Possibility-Seeker, The Straight-Shooter, The Peace-Keeper.
- Core motivation, core fear, workplace signature, stress behaviour, what they
  need from colleagues, and both wing variations per type are written in-house.
- All 27 items are written in-house.
- We keep the three centres (gut / heart / head) because that is framework-level
  structure, useful in a team report, and not any vendor's content.

**Collision check — vendors.** Our nine labels were checked against the
Riso-Hudson names published on enneagraminstitute.com (see §1) and against the
type vocabularies used by the other widely-circulated systems — Integrative9's
and the common alternates (Perfectionist, Giver, Romantic, Observer, Devil's
Advocate, and so on). No label collides with any of them. The vendor lists are
deliberately *not* transcribed into this repo, here or in the module: the audit
trail is the source URL plus this recorded result, so that documenting what we
avoided never becomes a copy of it.

**Collision check — our own other layers.** This is the one that was missed
first time round, and it is the one a reader actually notices. A personal guide
prints the DISC-style name, the strength themes, and the Enneagram type on the
same page with nothing to mark which layer a name came from. Types 1, 3 and 9
shipped as The Standard-Setter, The Front-Runner and The Steady Hand against
DISC styles called Standard Setter, Front Runner and Steady Hand — three names
that read as identical — and are now **The Line-Holder**, **The Scorekeeper**
and **The Peace-Keeper**. (`strengths.py` had the fourth case of the same
problem: its `steady_hand` theme, now Even Keel.)

No module can enforce this on its own, because none of them imports the others.
The guarantee is a test:
`backend/tests/test_smart_teams_motivators_and_enneagram.py` reads `TYPES`,
`disc.WORK_STYLES` and `strengths.THEMES` and fails if any Enneagram label
matches a name in either of the other two after "The", hyphens, and case are
normalised away. Normalised deliberately — a raw string comparison would have
passed "The Standard-Setter" against "Standard Setter" while the guide still
read as if one layer had printed twice.

**What we deliberately did not build.** No inter-type movement (stress/security
arrows), no instinctual subtypes, no levels of development. Those are the parts
of the model with the least evidence behind them and the strongest association
with specific authors' published systems. Wings are included because the working
guide format calls for them, and are gated behind the strictest threshold in the
module.

---

## 4. Item and scoring design

### 4.1 Shared conventions

Both modules follow `disc.py`, which is the anchor for this package:
`Literal` vocabularies rather than enums, one frozen spec dataclass per
dimension carrying the prose the generator renders, `item_id`/`prompt` field
names, `as_dict()` on every result for the per-framework `st_profiles` JSON, an
`interview_payload()` that gives the frontend the scale and prompts and withholds
the scoring keys, and a pure `score_*()` entry point with no I/O and no LLM.

Two documented divergences, both deliberate:

1. Both modules stamp an `ITEM_BANK_VERSION` on every result. `disc.py` predates
   this and should get one. The version is what makes a stored interview
   re-scorable with a known bank.
2. `EnneagramRead` is frozen where `disc.py`'s results are mutable, because its
   whole purpose is an invariant — no named type without a confidence — that has
   to survive construction.

Unknown item ids **raise** in both modules documented here rather than being
skipped: a typo'd id that scored as "unanswered" would silently shift an axis,
and on `enneagram.py` it could shift which type a person is told they are. The
consequence is that re-scoring answers stored under an older `ITEM_BANK_VERSION`
needs an explicit migration. That is the intended cost, not an oversight.

This is **not** uniform across the package, and this doc only speaks for its two
modules. `big_five.py` logs unknown ids and carries on scoring; that divergence
is real and unreconciled, and the looser behaviour is not the considered one —
so do not "align" either module here down to it. Whichever way the package
settles, it should settle deliberately and in one commit.

### 4.2 Motivators items

| Property | Value |
|---|---|
| Items | 30 — five per orientation |
| Keying | 3 positive + 2 reverse per orientation (18 / 12 overall) |
| Scale | 5-point agreement, `LIKERT_ANCHORS`, labelled midpoint |
| Ids | `mot_<axis>_<nn>`, permanent; the number is an authoring label, not a position |
| Order | Six orientations in a fixed rotation, so no two consecutive items share an axis; reverse items scattered so no keying run exceeds three |

Reverse keying is load-bearing. On a pure agreement scale, an agreeable
respondent endorses all six axes and the *ranking* — the only output this layer
has — flattens into tiebreak order. Verified on the built bank, and kept
verified by `backend/tests/test_smart_teams_motivators_and_enneagram.py`:
adjacent-axis repeats 0, longest keying run 3.

Presentation order is part of the instrument. Scoring is order-independent, so
re-sorting the tuple would break nothing anybody would notice — which is exactly
why it is called out. Neither bank here has a response-quality check yet;
`disc.check_response_quality` is the model for the one they need, and every
measure in it (run length, longstring, position) only means something on the
order the respondent actually saw. The tuples must not be re-sorted.

### 4.3 Motivators scoring

Per axis: reverse items flipped (`LIKERT_MIN + LIKERT_MAX - response`), mean of
the answered items, rescaled to 0–100. The field is called `emphasis`, not
`intensity` like its DISC counterpart, because it is explicitly **within-person**:
there is no norm group, so a 68 means something only against this person's own
other five numbers. It must never be rendered as a percentile.

Ranking is the output. Ties break on the canonical `MOTIVATORS` order so the same
answers always produce the same guide.

| Constant | Value | Why |
|---|---|---|
| `MIN_ITEMS_PER_MOTIVATOR` | 3 | Below this the axis is `thin` and the whole result is `is_complete = False`. A ranking containing a guessed-at axis is worse than no ranking, because the guide states it as fact. |
| `CLOSE_RANK_MARGIN` | 8.0 | Roughly one scale point on one item of five — inside the noise of a 30-item bank. Adjacent pairs inside it are reported as level, never as one outranking the other. |
| `LEVEL_PROFILE_SPREAD` | 8.0 | If all six fit inside this, nothing leads and `headline()` says so instead of inventing a top two out of tiebreak order. |

An axis with no answers sits at the midpoint (50), not zero, so "we didn't ask"
can never read as a confident "this does not motivate them"; `is_complete` is
what distinguishes the two.

`probe_reason` returns *why* confirmation is needed — `incomplete`,
`level_profile`, `top_two_unordered`, `runner_up_contested`, or `None` — and
both `headline()` and the confirmation probe (§4.4) pick their wording from it.
This matters: an earlier revision appended "their answers put these close
together" to any result needing a probe, which printed a flat falsehood on a
profile whose top two were fifty points apart because *third* place was
contested. One hedge cannot cover four different doubts.

`bottom_is_contested` is a separate flag for a separate failure. `probe_reason`
is entirely about the top of the ranking, so a profile can lead by fifty points
and still have its last two places a rounding error apart — at which point
"Guiding Framework moves them least" is tiebreak order stated as a fact about
the person. When it is set, `headline()` names both of the trailing pair
("… X and Y move them least") and the probe swaps in its hedged
bottom-of-ranking question.

### 4.4 Motivators confirmation probe

Four questions, rendered against the person's own ranking so the conversation
starts from their answers rather than a blank page. Two of the four ask about
cost — a trade they actually made, and a stretch that went well by every
external measure and still left them flat — because people describe their values
accurately when recalling a real trade and aspirationally when asked directly.

The probe runs in **every** interview: 30 Likert answers cannot order six values
on their own. `probe_required` is the narrower flag marking results where the
item scores must not reach a published guide at all without the probe answers.

Because it runs every time, the probe is the most-read prose the module produces
— and the person reading it is the person it is about. So there is no single set
of templates. `probe_form()` picks one of four **forms** from `probe_reason`, and
each form may name only what the scores behind it support:

| Form | Used when | May name |
|---|---|---|
| `confident` | `probe_reason is None` | The top two in order, and the bottom |
| `pair_unordered` | `top_two_unordered` | The leading pair, in no order — never a first place |
| `second_place_hedged` | `runner_up_contested` | The leader; asks whether second place belongs to whom we put there |
| `open` | `level_profile` or `incomplete` | Nothing. No orientation is named at all |

`probe_for()` is the entry point; it renders whichever form the result earned.
The bottom-of-ranking question is then swapped for its hedged variant when
`bottom_is_contested`, independently of the form, because the top and the bottom
of a ranking fail separately.

**Why this exists.** The first cut had one fixed set of confident templates and
rendered it for everybody. On an all-midpoint answer set every axis scores 50.0
and the ranking is `MOTIVATORS` declaration order — so that wording told the
person "your answers put Practical Return and Understanding at the top of what
drives you at work", then asked them to account for a bottom-ranked value they
had never expressed. Every flat responder in the company got the identical
invented claim about themselves. `probe_reason` and `probe_required` were already
on the object; nothing consulted them. The lesson is in the module as a rule:
nothing may state a position in the ranking without going through
`probe_form()`, and a new `probe_reason` code with no wording of its own raises
rather than inheriting the confident templates.

All four question ids (`mot_probe_01` … `_04`) are the same in every form. An id
identifies the *purpose* — "the question about the bottom of the ranking" — not
the wording, because a stored answer has to mean the same thing whichever form
the person was asked. Both invariants are tested in
`backend/tests/test_smart_teams_motivators_and_enneagram.py`.

### 4.5 Enneagram items

| Property | Value |
|---|---|
| Items | 27 — three per type |
| Keying | All forward |
| Scale | 5-point agreement, same anchors |
| Ids | `enn_t<type>_<nn>`, permanent |
| Order | Three passes over the nine types in three different orders (`1..9`, then `5,9,4,8,3,7,2,6,1`, then `6,2,7,3,8,4,9,5,1`) |

All-forward keying is a considered choice, not laziness. A reverse item on a
typology has to mean "not a 4", which is eight different things at once and
measures none of them. The three passes use different orders so the sequence
does not telegraph a repeating 1–9 cycle a respondent could pattern-match; the
type each item points at is withheld from `interview_payload()`, because someone
who can tell which three statements are "the type 6 questions" is self-casting
rather than self-reporting.

### 4.6 Enneagram scoring

Each person's answers are **centred on their own mean** before types are
compared, then rescaled so a one-scale-point lead reads as 12.5 points. Centring
is how the module pays off the acquiescence bias that all-forward keying buys.
The price is that the output is purely relative: it can order this person's
patterns and can never say how strongly any one of them is present. That is
stated on `TypeScore` so no renderer reads a 71 as "strongly a 5".

| Constant | Value | Why |
|---|---|---|
| `WEAK_MARGIN` | 6.0 | Below this, no type is named at all. Returns provenance `unresolved` with both candidates and a "tell us" prompt. |
| `STRONG_MARGIN` | 12.5 | One full scale point on one item of three — the smallest difference a three-item subscale can express. `high` confidence also requires a complete bank. |
| `WING_MARGIN` | 8.0 | Stricter than the type bar, because wings have the least support of anything in the model. "No clear wing" is a fine thing for a guide to say. |
| `MIN_ITEMS_PER_TYPE` / `MIN_ITEMS_ANSWERED` | 2 / 18 | Below either, `unresolved` with the finish-it-later / tell-us / skip-it copy. |

Outcomes, each one exercised against the built bank in
`backend/tests/test_smart_teams_motivators_and_enneagram.py`:

| Situation | Provenance | Confidence | Type named? |
|---|---|---|---|
| Wide margin, all 27 answered | `inferred` | `high` | yes |
| Margin ≥ 6.0 but < 12.5, or an incomplete bank | `inferred` | `moderate` | yes, hedged — the guide appends a "held loosely" paragraph |
| Margin < 6.0 | `unresolved` | `low` | **no** — both candidates named |
| < 18 answers, or < 2 on any type | `unresolved` | `low` | **no** — no candidates either |
| Person states their type | `self_identified` | `high` | yes |
| Person opts out | `declined` | `none` | no |
| Segment not reached | `not_taken` | `none` | no |

`guide_section()` is the single accessor every document generator must use. It
returns `None` for declined, unresolved, and not-taken, which is what makes "a
profile with no Enneagram" a supported shape everywhere downstream rather than a
special case each renderer has to remember. A declined read also **discards the
scored ranking**, so a rejected inference cannot be picked up later by some
future feature.

### 4.7 Enneagram confirmation

`confirmation_step()` presents the top two candidates in plain language, with
neither marked as Cubie's pick, and four options: **the first one is more like
me / the second one is more like me / neither, let me pick from the nine / skip
the Enneagram on my profile**. The last two are what keep the layer optional;
dropping them would make it mandatory. A footnote states plainly that skipping
costs nothing.

The first two options are positional and symmetric, and that is load-bearing
rather than cosmetic. On an `inferred` read the candidate order *is* our ranking,
so the option strings are the only thing standing between "choose between these
two descriptions" and "agree with our answer". The original pair — "That's me"
and "The other one fits better" — did the latter: "that's me" only has a referent
if the person has already been told which one we picked, and "the other one"
frames the second candidate as the alternative to our pick rather than an equal
option. The answer maps back to a type by position, so the UI must render the
candidates in the order it received them.

When too few answers were given there are no candidates to show and the function
returns `None` — but that is **not** the end of the layer. The interview still
has to offer "tell us your type" and "skip it", and `EnneagramRead.note` is the
copy for it.

---

## 5. Limitations

### 5.1 Motivators

Spranger's framework has a far longer history than it has psychometric support.
When Hanel, Litzellachner & Maio (2018) compared human value models empirically,
the Study of Values scales reached a **maximum alpha of 0.26** and were dismissed
from all further analyses for low reliability. Our bank is shorter than theirs,
and authoring it more carefully does not fix a reliability problem of that size.

Read this layer as a **structured conversation opener, not a measurement**. The
ranking's job is to give the interview something specific to ask about and the
guide something concrete to say; the confirmation probe is where the actual
signal comes from. Three further honest caveats:

- Absolute scores are meaningless across people. There is no norm group.
- The middle of the ranking is not resolvable at 30 items. Only the top two and
  the bottom one are reported for exactly this reason.
- Reported values are partly self-presentation. The probe asks about trades
  actually made because that is harder to answer aspirationally.

### 5.2 The Enneagram — the criticism in plain terms

The Enneagram's evidence base is **substantially weaker than the Big Five
layer** in this same interview. Plainly:

1. **The systematic review is equivocal.** Hook et al. (2021), reviewing 104
   independent samples, found *mixed* evidence of reliability and validity. In
   its favour: some factor-analytic work partially aligns with the theory,
   subscales relate to other constructs (notably the Big Five) in
   theory-consistent ways, and several studies found the framework useful for
   personal growth.
2. **The nine-type structure does not reliably reproduce.** Factor analyses of
   Enneagram instruments typically recover **fewer than nine factors**, and the
   factors that do emerge look more like Big Five dimensions than like the nine
   types. No published work derives the nine types by clustering — the structure
   was asserted first and measured afterwards, which is the reverse of how the
   Big Five was built.
3. **The secondary machinery is the weakest part.** There is little research
   supporting wings or inter-type movement under stress and security. We ship
   wings behind the strictest threshold in the module and ship none of the
   movement theory at all.
4. **Expert opinion is unfavourable.** In the Norcross, Koocher & Garofalo
   (2006) Delphi poll of 101 doctoral-level psychologists, the Enneagram as a
   personality assessment scored **4.14 (SD 0.59)** in round 2 on a 1–5 scale
   where 4 reads "probably discredited" and 5 "certainly discredited". Note the
   caveat that most panellists reported being unfamiliar with it (65.9% in round
   2), so this measures expert consensus, not a fresh empirical test.

What follows for us: a type is a **vocabulary for a conversation**, not a
finding. "I default to scanning for what could go wrong" is genuinely useful for
a colleague to know. "Cubie has determined you are a 6" is not something a
27-item questionnaire on a contested model has earned the right to say.

### 5.3 Product decision (recorded)

**Decision:** ship the Enneagram layer, as a confidence-scored,
person-confirmable, fully optional part of the profile.

**Why ship it at all:**

- The two working guides this feature reproduces (a personal guide and a pair
  guide) use Enneagram language, and the team that asked for them already speaks
  it. Omitting the layer reads as a downgrade of the format we were asked for.
- As shared vocabulary it earns its place even where the measurement does not.

**The constraints that make that acceptable.** Four of the six are enforced by
the module and covered by
`backend/tests/test_smart_teams_motivators_and_enneagram.py`. The last two are
obligations on code that is not written yet, and the table says so rather than
implying a check exists:

| # | Constraint | Where it lives | Enforced? |
|---|---|---|---|
| 1 | Never reported without a confidence | `EnneagramRead` is frozen with no construction path to a type without one | code |
| 2 | A narrow margin yields no type, two named candidates, and a question | `WEAK_MARGIN`, provenance `unresolved` | code + test |
| 3 | Self-identification overrides inference, and is recorded as such | `self_identified()`, `with_self_identification()` | code + test |
| 4 | Declining is a complete, supported outcome | `declined()`, and `guide_section()` returning `None` | code + test |
| 5 | Narrative only — never an input to Team Builder matching arithmetic | Nothing in the module can stop a caller; complementarity scoring is to run off DISC-style + Big Five only | rule for ST-5 |
| 6 | Advisory only — never review, promotion, or compensation | `ADVISORY_NOTICE` is on every `as_dict()`; every generator has to print it | rule for ST-6/ST-7 |

**A profile with no Enneagram is a valid, complete profile.** The personal
guide, the pair guide, and the team report all have to read correctly with the
layer absent, and that is a test obligation on ST-6 and ST-7, not a nice-to-have.

**What would change the decision.** If confirmation-step telemetry shows people
routinely rejecting our inferred type — say the "neither" and "skip" options
together running above roughly a third of completions — the honest response is
to stop inferring and let the layer be self-identification only. That is a
one-line change in the interview flow (skip `score_enneagram`, go straight to
"do you know your type?") precisely because `self_identified` and `declined` are
already first-class states.

---

## 6. Rules for future editors

- **Never loosen the thresholds to reduce "unresolved" outcomes.** They are the
  honesty of the Enneagram layer, not tuning knobs. Reversing them reverses the
  design.
- **Never add an item, label, or description sourced from a vendor** — no Gallup
  CliftonStrengths items or theme names, no Wiley "Everything DiSC" items or
  that trademarked lowercase-i styling, no MBTI items or "Myers-Briggs" product
  claim, no TTI report prose, no Enneagram Institute type names or descriptions.
  Parsing a report someone uploads about themselves stays fine.
- **Bump `ITEM_BANK_VERSION`** on any change to item text, dimension/type
  assignment, keying, or a scoring threshold — anything that makes an old set of
  answers score differently.
- **Do not re-sort the item tuples.** Scoring is order-independent, so nothing
  fails when you do — and the run-length and position-bias checks these banks
  still need are measured on the order the respondent saw (see §4.2).
- **Keep both layers self-given only.** Smart Teams data comes from the
  interview and from reports the person uploads. `rubik_core/people/` (UC-47) is
  external-attendees-only and must stay that way.
- **Keep prose in the spec dataclasses**, never assembled ad hoc at render time,
  so a person's own guide, a pair guide, and a team report all characterise the
  same axis the same way.
