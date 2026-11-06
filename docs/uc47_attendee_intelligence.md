# UC-47 — Attendee intelligence: who is in the room, and how to talk to them

## The gap

Meeting prep knew an attendee as an email address and a display name. The
Roles & Dynamics panel proved it: half the cards read `(ROLE NOT SPECIFIED)` and
"their specific role is not detailed in the provided records", because the only
records the model had were our own transcripts, our own email, and our own Drive.
For the systems side that is tolerable — the people are colleagues. For anyone
walking into a meeting with an outside company it is the most important thing
missing: what does this person actually do, what do they care about, and how do
you open a conversation with them so it lands as informed rather than as a cold
pitch.

This adds a research step that answers that for every **external** attendee, and
surfaces it two ways: a prompt block the brief reasons over, and an
**Attendee Intelligence** panel the reader can click through.

## What it produces

One `AttendeeIntel` per external attendee (`rubik_core/people/schemas.py`):

| Part | What it is | Grounding |
| --- | --- | --- |
| `profile` | Position, employer, tenure, LinkedIn headline, trajectory, credentials | Observation. Carries the profile URL. |
| `activity` | Recent posts, comments, articles, talks — summarized **in their framing** | Observation. **Every item carries a URL or is dropped.** |
| `persona` | Communication style, decision style, public priorities, recurring themes, openers, what will land badly | **Inference.** Carries a `confidence` band and a `basis` line. |
| `sources` | Every URL behind the bundle | — |

The split between observation and inference is the whole safety design. Profile
and activity lines may be stated as fact because they are cited; the persona read
is fenced into its own object with a stated confidence, so the UI can label it and
the prompt can tell the model not to build strategy on a thin one.

## Why we do not scrape linkedin.com

The ask was "scrape the attendee in LinkedIn". We do not, and the reasons are
practical rather than squeamish:

1. **It breaks LinkedIn's terms**, and a business tool has no business doing that.
2. **It gets the egress IP blocked.** Cubie runs on one Cloud Run
   service. A block is not degraded service for one user — it is the feature dead
   for everyone, with no warning and no recourse.
3. **It breaks on markup changes**, which are frequent and unannounced, and the
   failure mode is silent: selectors stop matching and profiles come back empty
   rather than erroring.
4. **Auth-walled content needs an account**, whose credentials would sit in our
   config and whose ban would take a real person's LinkedIn account with it.

What we do instead gets the same content through paths that are stable:

* **Grounded public-web search (default, `sources/web_search.py`).** Gemini with
  Google Search grounding, reading the public LinkedIn profiles and public posts
  Google has already indexed — plus the rest of a professional's public footprint
  that LinkedIn scraping would miss entirely: conference agendas, association and
  speaker bios, podcast pages, trade-press quotes, bylined articles. Needs no new
  vendor, no new credential, and returns citations.
* **A licensed LinkedIn-data API (optional, `sources/linkedin_api.py`).** Set
  `LINKEDIN_DATA_API_KEY` for higher fidelity — dated position history and the
  actual text of recent posts. Proxycurl-compatible wire shape;
  `LINKEDIN_DATA_API_BASE` re-points it at any provider matching it. Inert
  without a key, which is the normal state.

Sources are tried in fidelity order and fall through, so an out-of-credit or
down provider degrades to a thinner card rather than an empty one.
`test_attendee_intel.py::test_nothing_in_the_people_package_scrapes_linkedin_directly`
asserts the posture in code — if a future change needs a real linkedin.com
request, that is a decision made by editing that test.

## Who gets researched

Narrow on purpose (`service.build_queries`, and it is pure so it is testable
without an LLM):

* **External attendees only.** Colleagues are already known to the reader, their
  LinkedIn adds nothing to a 1:1 brief, and profiling coworkers is not something
  this app should do. "Internal" means any of the workspace's verified domains
  in the control database, so a caller with no reader still cannot profile a
  colleague. An all-internal meeting makes **no research call
  at all** and its prompt is byte-identical to before UC-47.
* **Real humans only.** `info@`, `billing@`, `no-reply@`, calendar rooms and
  resources are dropped. An invented persona for a distribution list is worse
  than a missing card.
* **Capped at `MAX_ATTENDEES` (6), and the cap is reported.** Skipped people land
  in `partial_errors`, because a silently truncated list reads as "these people
  have no public presence".
* **Every reader, not just Sales.** An operations lead meeting a vendor's architect
  needs the same read as a rep does.

## Scope of the research itself

The prompts fence this explicitly (`persona._ETHICS`): **professional and public
only** — role, employer, trajectory, credentials, what they publish, the
positions they take on their industry, how they write. Never health, religion,
politics, race, national origin, sexual orientation, gender identity, age,
marital or family status, criminal history, financial standing, home address,
personal accounts, or family. Nothing behind a login or paywall, and nothing
clearly personal even when public. If a person's only findable material is out of
scope, they are treated as having no findable professional presence.

## The failure that actually matters: namesakes

Common names collide, and a rep will read this out loud in a room. The research
prompt requires an **identity tie** — the employer matches the email domain or
the stated company, or the title matches the hint — before anything is
attributed, and returns `found: false` when it cannot make the tie. A
wrong-person card is worse than no card, so "no card" is the default on doubt.
`company_hint` is derived from the email domain (`globex.example` → `Globex`),
so every external attendee has a disambiguator for free.

## Contracts enforced in code, not just asked for in prompts

Prompts ask; `persona.parse_intel` enforces:

* **Cite or drop.** An activity item with no URL never reaches the bundle, and
  neither does one whose URL is only a site's home page (`https://www.example.org`).
  The URL needs a path or a query, so it points at the item itself and the reader
  can check it. Added after a person's talk and article were both "cited" to a
  professional association's home page.
* **No guessed pronouns.** Both prompts tell the model to use the person's name
  or "they", never a pronoun or gender inferred from a name. This one is
  prompt-only (`_ETHICS`), with a test that it stays in both prompts.
* **`found: false` stays empty.** No half-filling from fields the model
  volunteered anyway.
* **No activity → confidence is `low`.** Whatever the model claimed. A
  personality drawn from a job title cannot present as well-evidenced.
* **Bounded strings and lists** on every schema field — provider and LLM output
  is untrusted input, so a runaway "headline" is truncated rather than shipped to
  the prompt and the database.

The response field is also copied from the research step **verbatim** rather than
read out of the briefing model's JSON. These are cited observations; letting the
briefing model re-emit them is precisely how a citation gets detached from the
claim it supports.

## Cost and latency envelope

Research runs inside meeting prep's source-gathering `asyncio.gather`
(`collect_meeting_prep_context` in `backend/routers/home.py`), alongside Gmail,
the transcript RAG and the other sources, so it is budgeted, not hoped about
(`people/config.py`).

When UC-47 shipped that gather ran inside the page's request. Now
(`meeting_prep/background.py`) Get Ready and Regenerate answer 202 at once and build the brief elsewhere: on GCP an
execution of the `deep-prep` Cloud Run Job running
`backend.jobs.meeting_prep_job`, otherwise a task on the app's own event loop.
Only `POST /meeting-prep` still builds inside the request. The budget still
matters: it bounds how long the reader waits for the "ready" notification, and a
build still unfinished after `CUBIE_PREP_BACKGROUND_TTL_MINUTES` (20) is marked
failed.

* One Flash call per attendee (grounded one-shot for the search path; one
  synthesis call for the API path).
* `PER_ATTENDEE_BUDGET_S = 180`, `TOTAL_BUDGET_S = 200`, attendees concurrent — so
  the step's contribution to a build's wall-clock time has a ceiling regardless
  of invite size. These were 35 and 60 at launch. After the Flash tier moved to
  a newer model, every lookup timed out at those budgets: no attendee got a card.
* What fixed the time was the number of searches, not the budget alone.
  Measured on the two outside guests of one meeting, one grounded call with no
  retries took 65-183 s at the model's default thinking (22-23 searches), 44-151 s at `"low"` thinking (11-14 searches), and 28-32 s
  at `"low"` with the prompt's search cap (8-9 searches; the model treats the
  cap loosely). So the research call now sends `thinking_level="low"` and the
  prompt carries `_SEARCH_BUDGET` (`MAX_SEARCHES = 5`). Even capped, one run of
  the same two people passed 90 s, so the ceiling sits well above the typical
  30-45 s.
* The research call passes the budget as its own HTTP timeout
  (`llm_grounded(timeout_s=...)`). The client default (`GENAI_CLIENT_TIMEOUT_MS`,
  120 s) would cut a slow call off and retry it from scratch, so a call that
  needed 150 s could never land. X-Ray's grounded calls pass neither knob and are
  unchanged.
* A timeout is logged at WARNING and named in the brief's Intelligence Gaps
  ("Attendee research ran out of time for …"). It used to be an INFO line and
  the card simply did not appear, which is why the failure above went
  unnoticed.
* A **shared** cache (`attendee_profiles`, TTL 14 days). A prospect's title and
  posting themes are not reader-specific, so one row serves every rep who preps a
  meeting with them — the same call X-Ray makes in treating a snapshot as a team
  asset. Steady-state cost of repeat prep on the same account is zero.
* Failed rows cache for **6 hours**, not 14 days, so a transient provider outage
  does not blank somebody's card for a fortnight.
* Cost is attributed to its own feature (`attendee_intel`) in the AI-Cost ledger,
  via a decorator on the per-person function — a contextvar set at the caller
  would not survive the hand-off to the worker thread.

## Where it surfaces

* **Roles & Dynamics** — each card picks up the researched position (only where
  the model left `role` blank), the LinkedIn headline, a LinkedIn link, and up to
  three recurring themes. The card's job is still the meeting dynamic.
* **Attendee Intelligence** — the full panel: trajectory, style read with its
  confidence badge, themes, public priorities, the cited activity list, openers,
  what would land badly, and a collapsible source list.
* **Both prompts** — the sales Battle Brief and the internal/EA brief, plus the
  Deep Prep council, all render the same block through
  `people/prompt.format_intel_block`, so the on-screen brief and the emailed PDF
  cannot form different impressions of the same person from the same evidence.
* **One-section rewrite** — `meeting_prep/rewrite.py` re-renders the block from
  the version's stored `context_bundle` (`format_intel_block_from_dicts`), so
  rewriting one section reasons over the same evidence the brief was built from
  and makes no new research call.
* **"Built from" strip** — `meeting_prep/signals.py` adds an "Attendee research"
  entry ("N people researched"), counting only people whose research did not
  error.
* **Word and PDF exports** — `people/export.py` flattens each bundle into
  style-tagged lines once; `deep_prep/docx.py` and `deep_prep/pdf.py` each render
  those lines with their own primitives, so the two files cannot drift on what
  appears or in what order.

Every instruction that points *at* the block is conditional on the block
existing. With research absent (flag off, or it failed), the sales prompt's
external-research mandate reverts to its original wording — disabling the feature
must not leave the brief worse off than before the feature existed.

## Other features that reuse it

One feature outside meeting prep calls into `rubik_core/people` rather than
reimplementing it, so the no-scraping rule, cite-or-drop and the confidence
floor apply to it too. `build_queries` also drops the company's own Workspace
domain as well as the reader's, so a caller that passes `user_email=""` still
never researches a colleague.

* **Call Capture** (`call_capture/domain_match.py`) — imports `PersonQuery` and
  `_RESOURCE_HINTS`, so the rule that keeps rooms and shared mailboxes out of
  research also keeps them out of HubSpot domain matching.

## Config

| Setting | Default | What it does |
| --- | --- | --- |
| `ATTENDEE_INTEL_ENABLED` | `True` | Master switch. Off → no panel, no prompt block, no research. |
| `LINKEDIN_DATA_API_KEY` | `""` | Licensed provider key. Empty → grounded search only. |
| `LINKEDIN_DATA_API_BASE` | Proxycurl | Re-point at a compatible provider. |
| `LINKEDIN_DATA_API_TIMEOUT` | `20.0` | Per-request timeout for that provider. |

Tunables that are not env-facing (budgets, TTL, caps, model tiers) live in
`rubik_core/people/config.py`.

## Files

```
rubik_core/people/
  schemas.py            AttendeeIntel and friends; the JSON round-trip contract
  config.py             budgets, TTL, caps, model routing, flags
  sources/base.py       PersonQuery (the who-is-researchable filter) + selection
  sources/web_search.py grounded public-web research (default)
  sources/linkedin_api.py licensed provider (optional, inert without a key)
  persona.py            prompts + the single model-JSON → bundle parse site
  store.py              the shared TTL'd cache
  service.py            research_attendees() — the entry point
  prompt.py             bundle → prompt block
  export.py             bundle → style-tagged lines for the Word and PDF exports
rubik_core/models.py    llm_grounded / safe_json now live here (see below)
alembic/versions/0001_cubie_baseline.py   the attendee_profiles table
frontend/src/components/AttendeeIntel.tsx
backend/tests/test_attendee_intel.py
backend/tests/test_attendee_intel_exports.py
backend/tests/test_meeting_prep_attendee_intel.py
```

`llm_grounded` and `safe_json` moved from `xray/agents/base.py` into
`rubik_core/models.py` (the neutral LLM layer) rather than being copied a third
time; X-Ray re-exports them from its old import site, so nothing there changed.
