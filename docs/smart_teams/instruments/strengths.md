# Smart Teams instrument design — Work Strengths

**Module:** `rubik_core/smart_teams/instruments/strengths.py` · **Ticket:** ST-1 · **Taxonomy version:** `strengths-2026-08-2`
**Status:** authored 2026-08-19. 28 themes, 4 clusters, 8 elicitation prompts, one LLM extraction rubric, one
validator, and an import-time check over the taxonomy itself (structural invariants plus the advisory boundary).

> **`strengths-2026-08-2` (2026-08-19)** — bumped for prose and naming changes, not for a structural one. The ids,
> the count, the clusters and the prompts are unchanged from `-08-1`. What changed: two `ask_for` lines that made
> an evaluative claim about the person — one about promotion-readiness, one a comparative superlative — were
> rewritten as coordination asks, and that boundary is now enforced at import instead of trusted to a reviewer
> (§6); and two theme *names* were retired for colliding with other layers of the same guide (§2). Per §7.8, guides
> generated against `-08-1` keep their stored version and are not silently re-rendered.

The module is canonical for the theme prose (definitions, behavioural anchors, under-pressure, overuse, and the
"ask me for" line). This document is canonical for *why the layer is shaped this way* — the legal argument, the
elicitation and extraction design, and the limits of what a strengths list can be used for.

---

## 1. What a "work strength" is in this instrument

A **recurring, observable contribution**: something a colleague could watch this person do, more than once, in
ordinary work, and be glad they were the one on it.

Three boundaries fall out of that definition, and all three are load-bearing:

| A work strength is **not** | Where that actually lives | Why the distinction matters |
|---|---|---|
| A skill | `st_person_skills` (ST-8) | "Can read a trust receipt correctly" is capability. "Refuses to stop at the symptom" is contribution. The Team Builder needs both and must never substitute one for the other. |
| A virtue or character trait | Nowhere in this product | We describe work behaviour. Character classifications answer a different question, for a different purpose (and are separately proprietary — §2). |
| A rating | Nowhere in this product | Nothing in the module emits a number. A theme is present or absent, and an absent theme is not a deficit. |

Every theme therefore carries **four** pieces of prose, not one: what it looks like when it is working, what
happens to it under pressure, what it costs when overused, and what a colleague should hand this person. The
overuse field is not a hedge for legal comfort — the evidence on positive traits is full of inverted-U effects
where the good thing turns costly past some inflection point (Grant & Schwartz, 2011), and practitioner work on
"lopsided" leaders makes the same observation about overplayed strengths. A strengths list that only flatters is
not usable by the colleague it was written for.

## 2. Why we authored our own taxonomy — the legal argument

**The short version: the instrument everybody means by "strengths at work" cannot be used, and neither can the
obvious alternative.**

**Gallup CliftonStrengths.** Gallup states that CliftonStrengths and **each of the 34 theme names** are its
trademarks, and that it holds copyright on the theme descriptions. Its product terms go further than
trademark-use restrictions: you may not create or offer applications, digital tools, or platforms that use
Gallup's intellectual property — *including the 34 Themes of Talent* — without Gallup's prior written permission.
Cubie's Smart Teams guides are precisely "a digital tool built on the framework", so there is no fair-use theory
here and no amount of paraphrase fixes it. Licensed use is also per-seat and priced per assessment (roughly
$25 for Top 5, $60 for the full 34 at list), which would mean paying per employee for content we then could not
freely re-render inside our own generated documents.

**VIA Character Strengths.** The 24-strength classification is copyrighted by the VIA Institute on Character, and
reprinting the classification requires their approval. It is also a poor fit on the merits: it measures character,
not work contribution.

**What that leaves, and the rules we applied.** Authoring our own. The 28 themes in the module were written for
this codebase under five rules a future editor has to keep applying:

1. **No name is taken from, or is a near-miss rewording of,** a Gallup theme, a VIA strength, or a Belbin team
   role. Be precise about what that is: an authoring rule and an editorial judgement, applied by the author and
   re-applied by a reviewer whenever a name changes. It is deliberately *not* a mechanical check, because a test
   that compared our names against those three lists would have to contain those three lists — which is the
   intellectual property this whole section exists to keep out of the repository.

   **Cross-layer collision, by contrast, is mechanical — and checking it properly caught two names.** All five
   instrument layers render into one guide for one person, so on 2026-08-19 the 28 names here were read against
   the style names in `disc.py`, the type labels in `enneagram.py`, the driver names in `motivators.py`, and the
   domain labels in `big_five.py`. Two names did not survive it:

   | Retired | Now | Why |
   |---|---|---|
   | Steady Hand | **Even Keel** | The old string was at the same time a `disc.py` style name and an `enneagram.py` type label. One guide could print "Steady Hand" three times meaning three different things — a style, a type, and a theme. |
   | Case Maker | **Decision Mover** | `disc.py` ships a style called "Case Builder". Nothing collided exactly, but "make a case" and "build a case" are the same phrase, and a reader had no way to tell which section was talking. |

   Ids did not move — `steady_hand` and `case_maker` keep their original spelling, because an id rename is a
   migration (§4).

   **This is a test now, not a sentence in a document.** The previous version of this rule said the names "were
   checked … against the DISC-style names in `disc.py` so the two layers do not collide inside one guide". No such
   check had been run, and the claim was false when it was written: "Steady Hand" was a `disc.py` style, an
   `enneagram.py` type label, and a theme here, all at the same time. Two tests replace the assurance —
   `backend/tests/test_smart_teams_strengths.py` pins that neither retired string can return as a theme name in
   this layer, and the cross-layer disjointness test added alongside this change (`backend/tests/test_smart_teams_instrument_names.py`) asserts exact-name disjointness
   across all five instrument layers, which is the only version of this rule that survives the next editor.

   **Near-misses that were weighed and kept**, listed so the next editor knows they were not missed:

   - **Standard Keeper** (here) vs **Standard Setter** (`disc.py` style). The closest surviving pair. Kept because
     the two words do different work — a setter defines the bar, a keeper holds it — and because a third rename in
     one pass costs more legibility than it buys. Revisit if guide readers actually confuse them.
   - **Even Keel** (here) vs **Evenness under load** (`big_five.py` domain label). Genuinely the same underlying
     construct, described in two registers: one is a trait axis with a band, the other is a contribution with
     behavioural anchors. Different enough in shape that no reader will mistake which section they are in.
   - The shared word-stock across layers — *-Builder* (Trust, Bridge, System here; Case Builder in `disc.py`),
     *-Keeper* (Standard, Precedent here; Process Keeper in `disc.py`), and *Straight-* (Straight Talker here;
     Straight Line in `disc.py`; The Straight-Shooter in `enneagram.py`). This is unavoidable in plain English
     about work, and it is only a problem when a whole name repeats, which is what the disjointness test pins.
2. **The structure is ours.** 28 themes in four clusters of seven — not 34 themes, and not anyone else's domain
   grouping. Any four-way cut of work behaviour lands in broadly similar conceptual territory; that is the
   domain, not copying. What must be ours, and is, are the labels, the definitions, the membership, and the count.
3. **Every line of prose was written here.** No definition, anchor, or report sentence is adapted or paraphrased
   from a vendor's materials.
4. **Someone else's results are theirs, but their vocabulary stays out of our output.** If an interviewee quotes
   their CliftonStrengths themes, that is their own data and they may keep it in their profile (the plan permits
   parsing official reports a person uploads). The extraction rubric explicitly forbids carrying that vocabulary
   into what we generate.
5. **Behaviour, not personality theory.** The themes describe what someone does at work. We make no claim that
   they are factors, that they are orthogonal in a statistical sense, or that they are exhaustive.

**Method used, as opposed to content borrowed.** The taxonomy-design literature is where the *process* came from:
mutually exclusive, collectively exhaustive characteristics per dimension, and explicit ending conditions — a
taxonomy should be concise, robust, comprehensive, extendible and explanatory (Nickerson, Varshney & Muntermann,
2013). Orthogonality/non-overlap and behavioural anchoring are the two criteria we actually leaned on, which is
why each theme has three or four watchable behaviours rather than a paragraph of adjectives, and why nearly every
theme was rewritten at least once to pull it away from its neighbour. Documented near-misses that were
deliberately sharpened:

- **Pattern Reader** (finds recurrence across cases) vs **Distiller** (compresses for an audience).
- **Downstream Thinker** (what a decision makes true later, good or bad) vs **Risk Sentinel** (named exposure,
  ranked, with a mitigation).
- **Last-Mile Driver** (drives one deliverable through the final tenth) vs **Loop Closer** (returns to many open
  threads).
- **Straight Talker** (initiates the hard truth) vs **Room Reader** (reads what is unsaid and names it).
- **System Builder** (makes it repeatable) vs **Precedent Keeper** (remembers why the rule exists).

## 3. The clusters

The clusters answer **"what does a piece of work need from the people doing it"** rather than "what kind of person
is this". Three reasons for the work-phase framing over a trait framing:

1. The layers either side of this one (`disc.py`, `big_five.py`) already group by trait. A second trait grouping
   in the same guide reads as the same claim made twice.
2. A work-phase grouping is actionable in a team report: "five Momentum, no Stewardship" is something a manager
   can act on; "high mean Openness" is not.
3. It keeps our structure clear of the vendor domain groupings we may not reproduce.

| Cluster | What it contributes | The cost of a team with none of it |
|---|---|---|
| **Sense-Making** | Works out what is going on before the group commits | Confident answers to the wrong question |
| **Momentum** | Starts, decides, and lands | Excellent analysis of work that never ships |
| **Connection** | Moves the work through people, inside and outside | Right in private, ignored in public |
| **Stewardship** | Makes the result survive next quarter | Rebuilds the same thing every year, surprised each time |

Clusters are a reading aid and a coverage prompt. They are **not scored**, nobody "is a Momentum person", and
cluster coverage is explicitly not an input to Team Builder selection (§6).

## 4. The 28 themes

Definitions and the overuse line are reproduced here for review convenience. Behavioural anchors, the
under-pressure paragraph, and the "ask me for" line live in the module and are the version of record.

### Sense-Making

| id | Name | What they contribute | Overdone, it costs |
|---|---|---|---|
| `pattern_reader` | Pattern Reader | Sees the recurring shape across cases everybody else is handling one at a time | Declares a pattern from three data points; genuine one-offs get read as system failures |
| `root_cause_digger` | Root-Cause Digger | Refuses to stop at the symptom until the answer explains the whole failure | A five-minute correction becomes a two-day investigation; nothing can just be fixed |
| `question_sharpener` | Question Sharpener | Turns a vague ask into a question that can be answered | Every request gets an interrogation, so people stop bringing half-formed ideas |
| `evidence_anchor` | Evidence Anchor | Goes to the source before forming a view, and can show where the number came from | Treats a defensible estimate as inadmissible; colleagues route around them for anything quick |
| `distiller` | Distiller | Compresses a pile of material into the two or three things the audience needs | Compresses away the caveat that mattered |
| `option_multiplier` | Option Multiplier | Produces several workable routes where the room assumed there was one | Reopens settled decisions; the team never converges |
| `downstream_thinker` | Downstream Thinker | Plays out what a decision makes true two steps later | Reversible decisions get debated as permanent ones; likely and imaginable blur |

### Momentum

| id | Name | What they contribute | Overdone, it costs |
|---|---|---|---|
| `cold_starter` | Cold Starter | Begins before it is defined and produces the first version to react to | A trail of promising v1s nobody finished |
| `call_maker` | Call Maker | Makes the decision on incomplete information and owns it | Decides what was not theirs to decide; the team stops surfacing doubts |
| `unblocker` | Unblocker | Finds the route, the exception, or the person who can say yes | Routes around process so habitually that controls stop meaning anything |
| `scope_cutter` | Scope Cutter | Removes work — says which parts need not exist | Trims muscle with fat; "do we really need this" becomes a position |
| `last_mile_driver` | Last-Mile Driver | Drags the nearly-done thing through the tedious final tenth | Finishes what should have been abandoned, and defends the sunk cost |
| `surge_performer` | Surge Performer | Gets better when it is urgent | Runs on urgency; quiet work drifts until it qualifies as an emergency |
| `pace_setter` | Pace Setter | Sets a cadence and holds it so a group knows what happens when | Cadence becomes ceremony; the work starts serving the schedule |

### Connection

| id | Name | What they contribute | Overdone, it costs |
|---|---|---|---|
| `trust_builder` | Trust Builder | The person colleagues tell the real version to, early | Holds information that needed to move |
| `bridge_builder` | Bridge Builder | Connects work across teams that would duplicate or collide | Everything becomes cross-functional; local decisions acquire stakeholders |
| `straight_talker` | Straight Talker | Says the uncomfortable true thing early, to the person's face | Candour as reflex; people brace instead of opening up |
| `case_maker` | Decision Mover | Builds the argument that moves a decision, for the room that matters | Persuasion outruns evidence; "we convinced them" gets confused with "we were right" |
| `talent_grower` | Talent Grower | Makes other people better on purpose, and delegates as teaching | Every task becomes a development opportunity; their own work slips |
| `room_reader` | Room Reader | Reads what is not being said and names it usefully | Interprets more than is there; the team manages an inferred atmosphere |
| `client_ear` | Client Ear | Holds the outside party's view inside internal debates, accurately | Every trade-off settled by whoever is furthest outside the building |

### Stewardship

| id | Name | What they contribute | Overdone, it costs |
|---|---|---|---|
| `standard_keeper` | Standard Keeper | Holds the quality line when nobody would have noticed | Polishes past value; others are held to a bar they were never shown |
| `risk_sentinel` | Risk Sentinel | Names the exposure specifically while it is still cheap | Risk answers everything; a filing deadline and a remote hypothetical sound alike |
| `system_builder` | System Builder | Turns what we did once into the way we do it | Systematises the twice-a-year; "the process says" outranks judgement |
| `detail_custodian` | Detail Custodian | Catches the wrong figure and the stale date before an outsider does | Thirty notes with no signal about which two mattered |
| `loop_closer` | Loop Closer | Nothing falls through; every open thread gets returned to | Tracking becomes the work; nothing is ever formally killed |
| `steady_hand` | Even Keel | Same quality on a bad week; calm in other people's storms | Steadiness shades into flatness, and hides when they are at capacity |
| `precedent_keeper` | Precedent Keeper | Remembers why the rule exists and what we decided last time | "We tried that in 2022" closes conversations that deserved a fresh look |

Ids are permanent: they appear in stored profiles, generated guides, and downstream keys, so a rename is a
migration. `name` can change freely; `theme_id` cannot. Two names have moved and their ids deliberately have not,
which is why `case_maker` renders as "Decision Mover" and `steady_hand` as "Even Keel" — see §2 for the collisions
that forced both.

## 5. Elicitation design — stories, not a Likert bank

The other layers score items. This one does not, for two reasons.

**A rating scale measures self-image; a story contains behaviour.** Asked directly whether they are
detail-oriented, people answer aspirationally, and self-report carries the usual freight (§7). Asked for a
specific incident, they describe what they actually did. That is the critical-incident logic Flanagan introduced
in 1954 — collect the incident, then read the behaviour out of it — and the behavioural interview built on top of
it. It is also the only elicitation method that produces the thing our extraction rubric requires: a **verbatim
quote of a behaviour**.

**Peer echo is the cheapest bias correction available.** Prompt 4 ("what do people come to you for?") is still
self-report, but it is self-report about *observable demand* rather than about the self, and in drafting it was
the prompt most likely to contradict the flattering answer to everything else.

| # | Prompt id | What it asks for | Why it is in the script |
|---|---|---|---|
| 1 | `str_01_energising` | A recent piece of work they would happily repeat — the doing, not the outcome | Easiest possible opener; energy is the closest available proxy to a strength in use |
| 2 | `str_03_effective` | One story of being unusually effective, walked through in order | The critical incident. Highest-value answer in the segment, and the usual source of the evidence quote |
| 3 | `str_04_peer_echo` | What people actually come to them for | Peer echo — observable demand rather than self-image. Weighted heavily |
| 4 | `str_07_pressure` | What they did more of under real pressure, and what dropped | Feeds the under-pressure half of the guide from the person, not from us |
| 5 | `str_08_overuse` | Where a strength of theirs gets them into trouble | Makes the shadow side self-given rather than inferred. Asked last: it needs the trust the first four built |

The prompt ids are deliberately non-contiguous. The script was cut from eight
prompts to five on 2026-08-20 to bring the interview's length down, and the
surviving prompts keep their original ids so a transcript recorded before the cut
still resolves — `prompt_id` is the key the extractor validates evidence quotes
against, and renumbering would orphan every stored transcript.

### What the cut removed, and what it cost

Three prompts were retired: `str_02_draining` (the negative space — a task they
are competent at and quietly dread), `str_05_unassigned` (what they pick up when
nobody assigned it), and `str_06_invisible` (good work nobody noticed).

The five kept were chosen on what each uniquely feeds rather than on order:
`str_03_effective` is the critical incident and the usual source of the evidence
quote, `str_04_peer_echo` is the only prompt that asks about observable demand
rather than self-image and is therefore the one most likely to contradict a
flattering answer elsewhere, `str_07_pressure` and `str_08_overuse` each feed a
named field of the guide (`under_pressure` and `overused`) from the person's own
words rather than from our inference, and `str_01_energising` is the opener — a
conversational segment that starts with "tell me about a time you were unusually
effective" is a cold open, and `str_08_overuse` in particular needs some trust
behind it.

Two costs, recorded rather than smoothed over:

1. **Stewardship lost its only dedicated source.** `str_06_invisible` existed
   because Stewardship themes are systematically under-reported — nobody thanks
   the person who caught the wrong date. Its job was folded into a third probe on
   `str_07_pressure` ("What did you handle that week that nobody else noticed?"),
   which costs seconds instead of the minute and a quarter a prompt costs. A
   probe is weaker than a prompt: the agent may not reach it. If Stewardship
   themes come back under-represented across the first cohort, this is the first
   thing to look at.
2. **The thinness bar got tighter in practice.** The substantive-answer bar is 4
   prompts, unchanged, but it is now 4 of 5 rather than 4 of 8. A person who
   gives a one-line answer to two prompts is now below the bar where before they
   had six more chances to clear it, so expect `transcript_is_thin` — and the
   confidence ceiling that comes with it — to fire more often.

Each prompt ships two to four **probes** whose only job is to move a general answer to a specific incident
("walk me through what you actually did, in order"). Probes are follow-ups for the agent to use, not a list to
read out. Ordering is deliberate: easiest question first, overuse last.

Two thinness bars, either of which flags the transcript (`transcript_is_thin`): fewer than **200 words spoken by
the interviewee**, or fewer than **4 of 5** prompts answered substantively (an answer under 8 words is a nod, not
an answer). Both exist because they fail differently — 300 words on one story with nothing else, versus five
one-line answers. The length bar counts only the person's own turns on purpose: these prompts and their probes
are long, and a conversation where every one was answered "yeah, that's me" clears two hundred words while
containing no behaviour at all. Note that 4 of 5 is a materially tighter bar than the 4 of 8 this was written
for — see §5's note on what the script cut cost.

## 6. Extraction design — deductive coding against a closed codebook

### The transcript format ST-3 must supply

**This layer takes speaker-tagged turns, not a string.** That is a hard interface requirement, not a preference,
and it is the difference between an evidence quote and a fabrication:

```python
from rubik_core.smart_teams.instruments.strengths import Speaker, Turn

transcript = [
    Turn(
        speaker=Speaker.ASSISTANT,
        text="Tell me about something you did well that nobody noticed.",
        prompt_id="str_06_invisible",
    ),
    Turn(
        speaker=Speaker.INTERVIEWEE,
        text="I found the transposed digit in the November file …",
        prompt_id="str_06_invisible",
    ),
]
```

- `Speaker` has exactly two values, because exactly one distinction matters: words the person said
  (`INTERVIEWEE` — the only quotable source) and words they did not (`ASSISTANT`). A human interviewer, a second
  participant, or a document the person pasted all belong on the non-quotable side and are recorded as `ASSISTANT`
  turns.
- `text` carries **no speaker prefix inside it.** `format_transcript()` adds the label, so somebody who types
  "INTERVIEWEE:" into an answer cannot relabel themselves.
- `prompt_id` is optional and only affects the prompts-answered count: tag the turns and the count is of distinct
  prompts; tag none and it falls back to counting substantive answers. A *partially* tagged transcript counts only
  its tagged part, which under-counts — the safe direction, since under-counting reads as thinner and thinner caps
  the confidence.
- Cubie's turns are passed to the model rather than stripped, because the question is what makes a short answer
  legible ("mostly the reconciliation part" means nothing alone). The label tells the model which lines it may
  quote; the validator enforces the same rule afterwards. **The label is a hint, the validator is the control.**

Passing a bare string raises `TypeError` rather than being accepted and degraded. Accepting it would silently
restore the original defect: a quote check that searched the whole conversation, which cannot tell "I found the
transposed digit in the November file" from Cubie asking "did you find the transposed digit?". A guide printing
`In your words: "…"` around our own question is worse than a guide with no quote at all.

### The prompt

`extraction_prompt(transcript)` assembles: role and definition → the closed taxonomy block (id, name, definition,
anchors, per cluster) → eight numbered rules → a strict JSON output contract → a thinness warning when applicable
→ the fenced transcript. Assembled by concatenation rather than `str.format`, because the output contract contains
JSON braces.

Design decisions and where they come from:

| Decision | Rationale |
|---|---|
| **Closed taxonomy, restated as a hard rule** | Deductive coding against fixed categories is the tractable end of what LLMs do with qualitative data; open-ended theme generation is not. Inventing a theme is the failure mode that would put un-vetted language in a company-visible document. |
| **Full definitions + behavioural anchors in the prompt** | Definition-based prompting materially outperforms bare category labels in the deductive-coding literature. The anchors are the operational meaning of each theme, so they are what the model matches against. |
| **Verbatim evidence quote, from one `INTERVIEWEE` turn** | Grounding requirement and audit trail in one. It also makes the guide arguable by its subject: they can see the sentence a theme was drawn from and delete it — which only holds if the sentence is theirs, so rule 1 says "never from a line labelled CUBIE" (`SPEAKER_LABELS[Speaker.ASSISTANT]`) and the validator checks turn by turn. |
| **Behaviour over adjectives, stated with an example in the prompt** | "I'm very detail-oriented" is self-description; "I found the transposed digit in the November file" is behaviour. The rubric spells out both sides of that pair. |
| **Explicit permission to return less** | Forced classification is a known source of LLM coding error; abstention is the recommended mitigation. The prompt says in as many words that two well-evidenced themes is a correct and complete answer. |
| **Three confidence bands with stated criteria** | `high` needs two or more independent moments with specific behaviour; `moderate` one clear story; `low` a passing mention or an inference from a stated preference. |
| **No other framework's vocabulary in the output** | Legal (§2), and it also stops a person's prior assessment results from being laundered into our themes without evidence from this interview. |
| **One person only; no protected characteristics; no performance/promotion framing** | The advisory-only contract, stated as rules 6 and 7 rather than assumed of the model. Our own prose is held to the same line by a separate control, below — the rule was in the rubric while the taxonomy itself shipped a promotion-readiness sentence, so the rubric plainly was not enough. |
| **The transcript is fenced as data, not instructions** | Same convention as `rubik_core/people/prompt.py`. The content is self-given, but a document a person pastes into an answer can contain anything. |

### The validator is the control, not the prompt

`validate_extraction(payload, transcript)` splits failures deliberately:

**Raises `ValueError`** — the contract has broken and a human should look: payload is not a mapping, `themes` is
missing or not a list, an entry is not an object, **a `theme_id` that is not in the taxonomy**, a duplicate
`theme_id`, an unparseable confidence. An unknown theme id is never repaired by mapping it to something nearby;
that is exactly the failure the closed taxonomy exists to prevent.

**Drops the individual theme with a warning** — the evidence did not hold up: no quote, a quote outside 4–60
words, or a quote that is not found verbatim inside a single one of the interviewee's own turns. One weak item
among five good ones should cost the person that item, not the whole section.

Quote verification normalises whitespace, smart punctuation, and case, and nothing else. Every word still has to
appear **in one interviewee turn, in order**, which rules out three things at once and is checked turn by turn
rather than against the joined conversation for exactly that reason:

1. **An invented quote** — the original purpose of the check.
2. **A quote lifted from an `ASSISTANT` turn.** The words really are in the transcript; they are our question. The
   first version of this validator searched the whole conversation, so a guide could print Cubie's own prompt back
   at the person under the heading "In your words" and look completely normal doing it.
3. **A quote stitched across two turns.** Every word present, in order, and still a sentence the person never
   said as a sentence.

Tested in `backend/tests/test_smart_teams_strengths.py`: the assistant-turn case and the stitched case are each driven
end to end and asserted to be dropped, the honest case is asserted to survive, and a bare-string transcript is
asserted to raise rather than fall back.

Two more guards: a thin transcript **caps every confidence at `moderate`** regardless of what the model returned
("the model was told not to" is not a control), and more than `MAX_REPORTED_THEMES` (6) returned themes is
truncated with a warning. Below `MIN_REPORTED_THEMES` (2), `guide_section()` returns `None` and the person sees a
short explanation instead of a thin section.

### The advisory boundary is enforced over our own prose, at import

Rule 7 of the rubric forbids the *model* from producing advice about hiring, promotion, pay, or fitness for a
role. Nothing was stopping *us*, and the first version of the taxonomy proved it: `talent_grower`'s `ask_for`
line read "Ask them who on the team is ready for more; they will have an answer." That is a promotion-readiness
instruction inside a company-visible guide, in the most-read field of the most-read section, contradicting the
module docstring, rule 7, and limitation 3 below all at once. If a guide carrying it surfaced in an employment
dispute, the company would be defending an unvalidated in-house personality instrument that told managers to consult it
about who deserves advancement.

So it is now a load-bearing check rather than a reviewing habit. `_ADVANCEMENT_PROSE` runs at import over every
definition, behavioural anchor, under-pressure line, overuse line, and ask in all 28 themes, and `_check_taxonomy`
raises `RuntimeError` — refusing to import the module at all — on a match. It covers readiness ("ready for more",
"ready for a role"), any form of "promote"/"promotion", "who deserves", "has potential", "top performer", and
comparative superlatives that rank the person against colleagues ("best in the building"). The pattern is
deliberately blunt in that direction: a false positive costs an editor one reword, a false negative ships.

`backend/tests/test_smart_teams_strengths.py` asserts both halves — that no theme's prose matches the pattern
today, and that the guard actually fires when the retired sentence is fed back into a theme, so it cannot rot into
decoration. It also asserts the guard leaves the coordination register alone: "get their best work" is exactly
what these fields are supposed to say.

### What this layer may drive

The personal guide, the pair guide, and the team report — all narrative. It must **not** enter the Team Builder's
matching arithmetic. The one study to look directly at whether balanced *strengths* coverage predicts team
outcomes found no relationship for character-strengths balance (while finding effects, some of them inverted-U,
for role representation) (Gander, Gaitzsch & Ruch, 2020). What does show up as a team-level effect is people
knowing and coordinating around each other's strengths — the sub-dimensions of "collective strengths use" are
awareness, credibility and coordination (Meyers, van Woerkom & Bauwens, 2023). Awareness is the product a shared
guide delivers, and it is the reason every theme carries an "ask me for" line. A selection score is not on offer.

## 7. Limitations

Stated here so nobody has to rediscover them, and summarised in-product wherever this layer is rendered.

1. **It is self-report, with all the usual freight.** Social-desirability bias, reference bias (people apply
   different implicit standards to the same word), and uneven self-insight all apply. Story prompts and the peer-
   echo question reduce this; they do not remove it. Self/observer agreement for personality is moderate at
   best, and we collect no observer data at all — by design, since the whole module is self-given-only.
2. **LLM extraction varies.** Reported agreement between LLM and human coders in deductive settings spans a wide
   range across studies (roughly κ ≈ 0.5 to near-perfect depending on task and prompting), which is wide enough
   that no single published result generalises to ours. Two consequences we accept: the same transcript can
   produce a slightly different ranking on re-run, and the person's review step is not a formality — it is the
   human-in-the-loop the literature says this method requires.
3. **A strengths list is not a competence assessment.** It says nothing about whether someone is good at their
   job, ready for a role, or performing. It is not evidence for or against any employment decision, and using it
   that way would convert an advisory tool into a legal exposure. Advisory-only is in the product copy, in the
   module docstring, and in the extraction prompt — and, since 2026-08-19, in a check that refuses to load theme
   prose which breaks it (§6). All three of the written statements were already in place on the day the taxonomy
   shipped an `ask_for` line telling managers to ask who was ready for more, which is the whole argument for
   having the fourth.
4. **The taxonomy has no psychometric validation.** It has face validity and non-overlap discipline; it has no
   reliability coefficients, no factor structure, and no criterion validity, because it is a vocabulary rather
   than a measure. Nothing here should be described to anyone — internally or to a client — as a validated
   instrument.
5. **Absence is not deficit.** A theme that did not surface may simply never have come up in five prompts — a
   weaker guarantee than when the script had eight. The
   guide never lists "themes you lack", and no view derives a gap from a missing theme.
6. **Cluster coverage is a conversation, not arithmetic.** See §6. A team report may observe that nobody in a
   group shows a Stewardship theme; it may not score or rank a roster on that basis.
7. **The evidence base for strengths work is positive but modest, and partly vendor-produced.** Controlled-trial
   meta-analysis finds small improvements in well-being and performance from strengths interventions (effects in
   the d ≈ 0.2–0.3 range), and strengths use correlates with engagement and performance across a large
   multi-sample literature. Separately, the psychometric evidence for the best-known commercial instrument comes
   largely from its own vendor's technical reports rather than independent peer review — a good reason to be
   modest about our own claims too.
8. **Re-run policy.** Bump `TAXONOMY_VERSION` on any change to ids, prose, prompts, or the rubric — a name change
   counts as a prose change. Existing guides keep their stored version and are not silently re-generated; a person
   re-approves after a regeneration. Last bump: `strengths-2026-08-2` on 2026-08-19, for the two rewritten
   `ask_for` lines and the two retired theme names (header note, §2, §6).

## 8. Sources

Retrieved 2026-08-19. Marked **[full]** where the source text itself was read, **[abstract]** where only the
abstract or publisher summary was available, and **[search summary]** where the claim rests on a search-result
synthesis and should be re-checked before it is quoted externally.

**Licensing and IP**

- Gallup, *Product Terms of Use* — CliftonStrengths and the 34 theme names as trademarks; no building
  applications or digital tools on Gallup IP including the 34 Themes of Talent without written permission:
  https://login.gallup.com/Home/ProductTerms **[search summary]**
- Gallup, *Partnering With Gallup* — licensing and permitted-use posture:
  https://www.gallup.com/cliftonstrengths/en/350171/partnering-with-gallup.aspx **[search summary]**
- Gallup, *Science of CliftonStrengths*: https://www.gallup.com/cliftonstrengths/en/253790/science-of-cliftonstrengths.aspx **[abstract]**
- CliftonStrengths overview and pricing (~$24.99 Top 5 / ~$59.99 full 34):
  https://en.wikipedia.org/wiki/CliftonStrengths · https://www.uschamber.com/co/grow/thrive/cliftonstrengths-assessment **[search summary]**
- VIA Institute on Character, *Permissions and Guidelines for Use and Interpretation* — classification is
  copyrighted; reprinting needs approval:
  https://www.viacharacter.org/faq/permissions-and-guidelines-for-use-and-interpretation **[search summary]**

**Strengths at work — evidence and mechanisms**

- Meyers, C., van Woerkom, M., & Bauwens, R. (2023). Stronger together: A multilevel study of collective
  strengths use and team performance. *Journal of Business Research*, 159, 113728. Sub-dimensions: strengths
  awareness, credibility, coordination; 136 teams / 925 employees. https://doi.org/10.1016/j.jbusres.2023.113728 ·
  https://research.tilburguniversity.edu/en/publications/stronger-together-a-multilevel-study-of-collective-strengths-use-/ **[abstract]**
- Gander, F., Gaitzsch, I., & Ruch, W. (2020). The relationships of team role- and character strengths-balance
  with individual and team-level satisfaction and performance. *Frontiers in Psychology*, 11, 566222. No
  relationship found for character-strengths balance; inverted-U effects for some duplicated roles; 42 teams,
  self-report-dominated. https://pmc.ncbi.nlm.nih.gov/articles/PMC7734085/ **[full]**
- Virga, D., et al. (2023). Effectiveness of strengths use interventions in organizations: A pre-registered
  meta-analysis of controlled trials. *Applied Psychology*.
  https://iaap-journals.onlinelibrary.wiley.com/doi/10.1111/apps.12451 **[search summary — paywalled; effect
  sizes quoted in §7 are from the publisher summary and should be verified before external use]**
- Zhang, et al. (2025). Strengths use in the workplace and its effects on individual work outcomes: A
  meta-analysis (111 samples, N ≈ 43,575). *Applied Psychology*.
  https://iaap-journals.onlinelibrary.wiley.com/doi/10.1111/apps.70036 **[search summary]**
- van Woerkom, M., et al. (2023). Strengths-based leadership and employee work engagement: A multi-source study.
  *Journal of Vocational Behavior*.
  https://research.tilburguniversity.edu/files/98493172/HRS_vanWoerkom_Strengths-based_leadership_and_employee_work_engagement_JoVB_2023.pdf **[abstract — direct fetch blocked]**
- Hodges, T., & Clifton, D. *Strengths-Based Development in Practice* — the vendor-side account of the
  "opportunity to do what I do best" meta-analysis: https://www.weber.edu/WSUImages/leadership/docs/sq/development-in-practice.pdf **[search summary]**
- *The Quest for Strengths* — review of the theory and research underlying strengths-based development, and the
  standing critique that most validation is vendor-produced: https://files.eric.ed.gov/fulltext/EJ685058.pdf
  **[search summary — PDF text extraction failed]**

**Overuse / the inverted U**

- Grant, A. M., & Schwartz, B. (2011). Too much of a good thing: The challenge and opportunity of the inverted U.
  *Perspectives on Psychological Science*, 6(1), 61–76. doi:10.1177/1745691610393523 ·
  https://works.swarthmore.edu/fac-psychology/107/ **[full]**
- VIA Institute, research summary on strengths overuse and underuse:
  https://www.viacharacter.org/research/findings/character-strengths-overuse-and-underuse **[search summary]**
- Kaplan, R. E., & Kaiser, R. B. *Fear Your Strengths* / "Stop Overdoing Your Strengths", *HBR* (2009) — the
  practitioner "lopsided leader" account: https://hbr.org/2009/02/stop-overdoing-your-strengths **[search summary]**

**Elicitation method**

- Flanagan, J. C. (1954). The critical incident technique — the five-stage procedure, and the behavioural-event
  interview built on it: https://en.wikipedia.org/wiki/Critical_incident_technique ·
  https://sk.sagepub.com/ency/edvol/download/organizationalpsychology/chpt/critical-incident-technique.pdf **[search summary]**
- Enhanced critical incident technique in counselling/organisational research:
  https://files.eric.ed.gov/fulltext/EJ858080.pdf **[search summary]**

**LLM extraction from qualitative data**

- Hila, et al. (2025). Assessing the reliability of large language models for deductive qualitative coding: a
  comparative intervention study. https://arxiv.org/pdf/2507.14384 ·
  https://asistdl.onlinelibrary.wiley.com/doi/10.1002/pra2.1255 — definition-based and step-by-step prompting
  beat zero-shot; agreement moderate overall; recommends category definitions, human review, and permitted
  abstention. **[full, via summariser]**
- Exploring inductive and deductive qualitative coding with AI: inter-rater reliability between LLM and human
  coders: https://pure.psu.edu/en/publications/exploring-inductive-and-deductive-qualitative-coding-with-ai-inve/ **[search summary]**
- *When LLMs fall short in deductive coding: model comparison and human–AI collaboration workflow design*:
  https://arxiv.org/pdf/2512.21041 **[search summary]**

**Self-report limits**

- Reference bias in self-report measures: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9649615/ **[search summary]**
- Social-desirability bias: https://en.wikipedia.org/wiki/Social-desirability_bias **[search summary]**
- Self- and observer reports of personality (*Annual Review of Psychology*, 2025):
  https://www.annualreviews.org/content/journals/10.1146/annurev-psych-020124-115044 **[abstract]**

**Taxonomy-design method**

- Nickerson, R. C., Varshney, U., & Muntermann, J. (2013). A method for taxonomy development and its application
  in information systems. *European Journal of Information Systems*, 22(3), 336–359. Ending conditions (concise,
  robust, comprehensive, extendible, explanatory) and mutually-exclusive/collectively-exhaustive characteristics:
  https://link.springer.com/article/10.1057/ejis.2012.26 ·
  https://opus.bibliothek.uni-augsburg.de/opus4/files/93439/93439.pdf **[search summary]**
- Towards an integrative taxonomy of social-emotional competences — non-overlap and behavioural anchoring in a
  psychological competence domain: https://www.frontiersin.org/articles/10.3389/fpsyg.2021.515313/full **[search summary]**
