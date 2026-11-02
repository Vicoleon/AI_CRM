# Smart Teams (ST) — Work Style Profiles, Comparison & Team Builder

**Status:** Plan approved-in-principle 2026-08-19 · **Target: single delivery by 2026-09-30**
**Origin:** two executives exchanged "How to work with me" docs plus a pairwise comparison and asked for the same for everyone, loaded into Cubie, with a comparison tool for project groupings. A follow-on ask: "get me a team with these skills and this personality mix" via chat.

The two source PDFs (one person's guide + a pair guide for two colleagues) are the output spec. The shipped guides use Cubie's brand colours.

---

## Decisions (2026-08-19)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Assessment sourcing | **Hybrid, in-house default.** Cubie Work Style Interview for everyone (only the exec team has official DISC). Official results can be uploaded and merged; system must not assume they exist. |
| 2 | Frameworks | **Everything + 16-type:** DISC-style, strengths, motivators, Enneagram, 16-type letter layer — with public-domain Big Five (IPIP) as the scoring backbone. |
| 3 | Privacy | **Open guides, private raw.** Person reviews/edits their guide, then it's company-visible. Interview transcript + numeric scores visible only to the person + the admins who support Smart Teams (`smart_teams_support`). |
| 4 | Access | **Open to the workspace.** Everyone signed in with the company's domain can take the interview, manage/view guides and compare; Team Builder also needs the copilot. Admins narrow either in Admin → Feature Access. |
| 5 | Team Builder inputs | Declared skills + AI-inferred skills + availability (PTO/OOO). **No** workload/utilization in v1. |
| 6 | Interview UX | Hybrid guided flow: quick-fire scale banks + conversational AI sections. ~25 min target, resumable. **Measured at 29.4 min** after the 2026-08-20 strengths cut (8 prompts → 5, −3.75 min); was 33.1. Still 4.4 min over — see the note below. |
| 7 | Tracking | ST-n tasks with time estimates. |
| 8 | Timeline | One delivery by end of September (internal milestones still staged). |

## Legal & ethics guardrails (non-negotiable)

- **Public-domain foundations only.** Marston's DISC model is public domain (~50 vendors build on it); IPIP Big Five items are explicitly public domain including commercial use; Spranger's six value types (1928) underpin "motivators"; the Enneagram model itself is not owned. We author our **own** items, style names, and strengths taxonomy.
- **Never copy proprietary instruments**: no Gallup CliftonStrengths items/theme names, no Wiley "Everything DiSC" items or the "DiSC" trademark styling, no MBTI items ("16-type style (derived)" wording, never "MBTI/Myers-Briggs" as a product claim), no TTI report text.
- **Parsing official reports someone uploads is fine** — those are the person's own results, used with their consent for their own profile.
- **Advisory only.** Profiles inform collaboration and team formation. They never feed performance reviews, promotion, or compensation — stated in-product and in the launch comms. Personality instruments used for employment decisions are a legal exposure; team-fit guidance is not.
- **Consent + control.** Guide publishes only after the person approves it; retake anytime (new version, re-approval); person can unpublish (admin sees completion state only).
- **Boundary with UC-47** (`rubik_core/people/`): that module researches *external* meeting attendees from public sources and deliberately refuses to profile coworkers. Smart Teams is the opposite contract: *internal*, *self-given* data only. No web scraping of employees, ever. Separate module: `rubik_core/smart_teams/`.

## Instrument design (ST-1 output, summarized)

| Layer | Source | Scoring |
|-------|--------|---------|
| DISC-style (D/I/S/C + blend style name) | ~48 fresh-authored forced-choice/Likert items on the Marston model | Deterministic |
| Big Five backbone (5 domains, key facets) | IPIP public-domain subset (~60 items) | Deterministic |
| 16-type letters | Derived from Big Five via published correlations (E/I←Extraversion, N/S←Openness, T/F←Agreeableness, J/P←Conscientiousness) | Derived, labeled "(derived)" |
| Strengths (own taxonomy, ~24–32 work-strength themes) | Conversational segment: stories of energizing work, peer feedback | LLM rubric → ranked themes |
| Motivators (6 Spranger axes: economic, theoretical, social, individualistic, traditional, aesthetic) | Mini-bank + conversational probe | Mixed |
| Enneagram (type + wing, confidence-scored) | Short item set + conversational confirmation; self-identification can override | Mixed, confidence flagged |

Every conversational segment runs on the ADK agent with centralized Gemini model ids; every LLM call is `@attribute_to("smart_teams")`.

## Architecture

**New module** `rubik_core/smart_teams/` + `backend/routers/smart_teams.py` + frontend "Smart Teams" section (sidebar = `Layout.tsx`, the live one).

### Data model (alembic revision numbered off origin/main)
- `st_profiles` — user_email, version, per-framework JSON (disc, big5, type16, strengths, motivators, enneagram), per-framework provenance (`interview` | `official_upload` | `merged`), status.
- `st_interviews` — session state, section cursor, scale answers, ADK session ref, started/completed timestamps. Resumable.
- `st_uploads` — uploaded official report (file ref), detected framework, parsed JSON, review status. Official data overrides interview data per-framework, provenance-flagged.
- `st_skills_taxonomy` / `st_person_skills` — skill, level, source (`declared` | `inferred` | `manager`), evidence ref, `confirmed` flag. **Inferred skills are suggestions until the person confirms them.**
- `st_documents` — kind (`personal_guide` | `pair_guide` | `team_guide`), subject emails, profile versions used, generated-doc ref, approved_at, rag_ingested. Pair/team docs cached by (subjects, profile versions).

### Access
- `require_smart_teams_access`: any authenticated user on the company's own domain, gated by the `smart_teams` registry feature. Covers: take interview, own profile/raw, approve/unpublish own guide, view published guides.
- Compare carries the same gate (half of every comparison is about the reader). Team Builder adds `require_full_app_access` (the copilot), because it is a staffing tool used on other people. Frontend `AuthContext.tsx` mirrors; keep the CI check that enforces backend/frontend agreement.

### Pipelines (all heavy work on cubie-worker Cloud Run Job — no fire-and-forget on the service)
1. **Interview** (guided page) → scoring job → draft personal guide → person edits/approves → publish → **RAG ingest of the guide only** (companywide corpus, tagged `smart-teams-guide`; raw scores never ingested) → chat can answer "how should I approach X about Y".
2. **Upload** → Gemini parse → person confirms mapping → merge into profile (new version) → guide regenerates → re-approval.
3. **Comparison** — chat tool + UI: pick 2 (pair guide, the source pair-guide format: synergy table, challenge areas, "X provides / Y provides", blunt % assessment) or N (team dynamics report: style coverage, amplification risks, missing styles, suggested working agreements).
4. **Skill inference sweep** — periodic job over internal signals only (meetings attended, tickets resolved, docs authored) → evidence-linked suggestions → person confirms/denies in their profile.
5. **Team Builder** (needs the copilot) — parse the ask (skills, style mix, size, project window) → filter by availability (`rubik_core/availability/pto.py`) → score skill coverage (declared + confirmed-inferred) + style complementarity heuristics (quadrant coverage, known amplification risks) → roster + alternates + AI rationale → one click generates the team guide.

### Docs
`backend/services/generated_docs.py` infra + a Cubie brand template reproducing the two PDF formats. Word + PDF export, Save-to-Assets (tagged `cubie-generated`, not RAG-ingested — the separate guide-ingest path above is the only RAG write).

### Ops & rollout
- Completion dashboard (admin): who's taken it, who's published; exportable for leadership.
- Weekly Slack DM nudge to incomplete users via the existing outbound DM path.
- Help page updated in **both homes** (`Help.tsx` + `rubik_core/help/content.py`).
- AI Cost: new `smart_teams` category — decorate the **workers**, add `_FEATURE_LABELS` entry, same PR as each feature.
- Internal dry run with one team before the all-company announcement; announcement drafted for leadership in business-outcome language.
- After code review and release approval, deploy with
  `scripts/platform/deploy.sh --env <private-env>`. Its migration Job handles
  registered workspaces before the Service and Jobs. Nothing auto-deploys.

## Out of scope (v1) — noted for later
- Workload/utilization as a Team Builder factor (deferred).
- Keep the engine free of company-specific assumptions where cheap.
- Manager-curated skill endorsements beyond confirm/deny.

## Tickets (ST prefix)

| ID | Title | Est | Depends on |
|----|-------|-----|-----------|
| ST-1 | Research & instrument design: curated-source review (DISC/Big Five/Enneagram/motivators/strengths application), author item banks + scoring rubrics + style/theme taxonomies, legal-hygiene checklist | 12h | — |
| ST-2 | Data model + migrations + `require_smart_teams_access` gate (backend + AuthContext + CI check) | 10h | — |
| ST-3 | Interview experience: guided page, scale banks + conversational segments, resume, progress, completion events | 24h | ST-1, ST-2 |
| ST-4 | Scoring engine + profile builder: deterministic scoring, LLM rubric extraction, 16-type derivation, versioning, retake flow | 16h | ST-1, ST-2 |
| ST-5 | Official-results upload + parse + merge (the exec team's DISC first) | 8h | ST-4 |
| ST-6 | "How to work with {NAME}" generator: brand template, draft→edit→approve→publish, Word/PDF export, RAG ingest | 14h | ST-4 |
| ST-7 | Comparison engine: pair guide + N-person team dynamics report, chat tools + UI, caching | 14h | ST-6 |
| ST-8 | Skills: taxonomy seed + curation, interview declaration section, AI-inference sweep with confirm/deny | 14h | ST-3 |
| ST-9 | Team Builder: chat tool + page, availability filter, skill-coverage + style-complementarity matcher, roster + rationale + team guide | 16h | ST-7, ST-8 |
| ST-10 | Rollout: completion dashboard, Slack nudges, Help both homes, AI Cost category, dry run, launch comms draft, deploy | 10h | all |
| ST-11 | *Stretch:* Meeting prep tie-in — style tips for internal attendees in briefs (flag-gated) | 6h | ST-6 |

**Total: ~144h core + 6h stretch.** Sequencing: ST-1/2 → ST-3/4/5 → ST-6 → ST-7/8 → ST-9 → ST-10 (→ ST-11).

## Sources (licensing research, 2026-08-19)
- DISC model public domain; vendor instruments proprietary: discprofiles4u.com/disc-trainer-faqs, en.wikipedia.org/wiki/DISC_assessment, discprofile.com/blog/which-disc-is-disc
- CliftonStrengths pricing (~$24.99 Top 5 / ~$59.99 full 34): uschamber.com/co/grow/thrive/cliftonstrengths-assessment, en.wikipedia.org/wiki/CliftonStrengths
- IPIP public domain incl. commercial use: en.wikipedia.org/wiki/International_Personality_Item_Pool, psdlab.uoregon.edu/measuring-the-big-five-personality-domains
- MBTI trademark + open alternatives (OEJTS, Big Five mapping): myersbriggs.org/legal, en.wikipedia.org/wiki/Myers–Briggs_Type_Indicator

---

## Interview length — measured, not asserted

`interview.duration_report()` computes the estimate from the item banks rather
than from a number typed beside them, `meets_target` is `False`, and
`backend/tests/test_smart_teams_interview_contract.py` pins the figures. Trimming
a bank therefore moves a test rather than moving this promise.

| | Required path | With the optional motivators probe |
|---|---|---|
| First cut | 33.1 min | 38.1 min |
| After the strengths cut (2026-08-20) | **29.4 min** | 34.4 min |
| Target | 25.0 min | — |

The strengths elicitation script went from eight prompts to five (a product call).
What that cost is recorded in `docs/smart_teams/instruments/strengths.md` §5 —
briefly: Stewardship themes lost their only dedicated prompt (folded into a probe
on `str_07_pressure`), and the substantive-answer bar is now 4 of 5 rather than
4 of 8, so thin-transcript flagging will fire more often.

**The remaining 263 seconds are accepted.** Making the Enneagram opt-in was
declined (2026-08-21): the layer stays in the interview for everyone. So ~29.4 minutes is
the shipped length, and the ~25 in Decision 6 is superseded by it rather than
still being chased.

`duration_report().meets_target` therefore stays `False` on purpose — it is
reporting the honest number against the original target, not flagging an open
task. Do not re-propose making the Enneagram optional, and do not "fix" the
target by trimming a bank: the resume machinery already lets somebody take the
interview in two sittings, which is the answer if 29 minutes proves too long in
the dry run.
