# Customer Relationship Graph — Phase 0 + Phase 1 Spec

- **Status:** draft for review
- **Date:** 2026-08-18
- **Scope:** Phase 0 (attendance-grounded prior meetings in Meeting Prep) and Phase 1 (persisted `graph_entities` / `graph_edges` schema + writers). Phase 2 (LLM-extracted commitment edges) is **out of scope** here; §7 is a one-paragraph preview.
- Line numbers below are as of this branch's HEAD and will drift.

## 1. Problem

Cubie assembles customer/relationship context per feature, with no shared identity layer:

1. **X-Ray and Meeting Prep share zero linking code.** X-Ray anchors on a HubSpot `company_id` and joins everything through HubSpot v4 associations (`rubik_core/xray/fetchers.py:298`). Meeting Prep links by email-domain string matching, and only in Deep Prep (`backend/routers/home.py:2255` → `rubik_core/retrieval/hubspot_api.py:395`).
2. **Both reach transcripts by name-guessing, not identity.** X-Ray queries the meetings corpus with `f"meetings with {company_name}"` (`rubik_core/xray/agents/meetings_agent.py:31`); Meeting Prep queries with the event title + external domains (`backend/routers/home.py`, `fetch_past_meetings`). A transcript that never says the company name verbatim silently drops out of both.
3. **The attendance record is never used for prep.** `meeting_participants` (person ↔ transcript; the transcript ACL source of truth — `rubik_core/database/models.py:192`) answers "when did I last meet these people" *exactly*, and Meeting Prep never touches it.
4. **The graph is already computed — then thrown away.** The priorities pipeline has canonical node types with per-kind id conventions (`EntityKind`, `rubik_core/priorities/models.py:204`), stable `entity_key` join keys (`"{kind}:{id}"`), a six-tier entity-resolution engine (`backend/signals/correlator.py`), and an in-memory adjacency index rebuilt on every run (`rubik_core/priorities/orchestrator.py:891`). Nothing persists it; no other feature imports it.

## 2. Design principles

- **Deterministic before generative.** Phase 0 and Phase 1 add **zero LLM calls**. Every node/edge comes from data Cubie already fetches; we only stop discarding it.
- **No new infrastructure.** Cloud SQL tables + indexed queries. No graph database, no new service, no new scheduler (backfill rides cubie-worker).
- **ACL-preserving by construction.** The graph stores *metadata only* (ids, titles, dates, provenance) — never content. Content is always re-fetched through the existing per-source access paths. Full rules in §6.
- **Write where the data already flows.** Graph writers are fail-soft hooks inside existing fetch/ingest code paths — never new API calls, never able to break their host flow.
- **Kill rule up front.** §3 defines the question set and the gate that Phase 2 (LLM extraction) must pass. The acceptable outcome of that evaluation includes "stop here."

## 3. Acceptance questions + kill rule

The graph exists to make these answerable. They are the evaluation anchor for every phase (per the "narrow scope: start from costly, verifiable questions" discipline).

| # | Question | Today's failure mode |
|---|----------|---------------------|
| Q1 | When did I last meet these attendees, and what did we discuss? | Only answered if title-words semantic search happens to hit; "last met" dates often absent or wrong |
| Q2 | Have we met with this company before this deal existed? | Name-string RAG query; misses transcripts that don't say the company name |
| Q3 | Who at our company knows people at {company}? | Unanswerable (requires person→meeting→person traversal) |
| Q4 | Which meetings has {company} had with us this quarter? | X-Ray meetings agent name-guess; no id-join to transcripts |
| Q5 | Which contacts on this deal have we actually met vs never spoken to? | Unanswerable (HubSpot contacts and meeting attendance never joined) |
| Q6 | This attendee is new to me — has anyone at our company met them before? | Unanswerable |
| Q7 | What's open with this company right now (deals + last meeting + who owns it)? | Deep Prep gets deals only; "last meeting" is a RAG guess |
| Q8 | Which customers have gone quiet — no meetings in N weeks? | Unanswerable without per-company transcript identity |
| Q9 | Show {company}'s interaction timeline including *our* meetings, by id not name-match | X-Ray timeline has HubSpot activities + news only |
| Q10 | Prep for an internal 1:1: when did we last meet and what did I commit to? | Same title-guess path; commitments depend on the right transcript being retrieved |
| Q11 | Which deals have meetings scheduled with us next week? | Unanswerable (calendar events and deals never joined) |
| Q12 | Who owns the relationship with {company} (deal owner + who actually attends)? | Owner known in X-Ray only; attendance side unanswerable |

**Kill rule.** Before any Phase 2 work: build gold answers for Q1–Q12 against real data for ~3 pilot users; run current Deep Prep vs graph-aware Deep Prep. If fewer than **4** of the transcript-dependent questions (Q1, Q2, Q4, Q7, Q10) flip from wrong/missing to right, Phase 2 is **not built**. Phase 0/1 must justify themselves on prep accuracy alone; if Phase 1 writers cause operational trouble, they are feature-flagged off and the tables sit inert (additive schema, nothing depends on it).

---

## 4. Phase 0 — attendance-grounded prior meetings in Meeting Prep

**Goal:** every briefing states *when the reader actually last met the people on the invite and in which meetings*, from attendance records rather than semantic retrieval. Directly targets Q1/Q10; measurably improves `past_meeting_context`, `client_profile`/"Relationship Context", and `open_items_you_owe` grounding.

**Non-goals:** no schema change, no new tables, no change to transcript *content* retrieval (the existing `search_past_meetings` call stays as-is), no X-Ray changes.

### 4.1 New repository method

`SQLMeetingParticipantsRepository` (`rubik_core/database/repository.py:601`) gains:

```python
def list_shared_meetings(
    self,
    reader_email: str,
    counterpart_emails: list[str],
    limit: int = 8,
) -> list[SharedMeeting]:
    """Transcripts the reader attended together with ≥1 counterpart, newest first.

    ACL-safe by construction: only docs where the READER has a
    meeting_participants row are eligible — this is the same gate
    search_past_meetings enforces.
    """
```

Semantics (single query + Python-side grouping — no `ARRAY_AGG`, so it runs on the sqlite test DB too):

- Rows from `meeting_participants mp_self` where `participant_email = reader` (lowercased), joined to `meeting_participants mp_other` on the same `doc_id` with `mp_other.participant_email IN counterparts` (lowercased, deduped), joined to `assets` with `status != 'deleted'`.
- Group by `doc_id` in Python → `SharedMeeting(doc_id, title=assets.original_filename, when=assets.created_at, overlap=[counterpart emails present])`.
- Order `created_at DESC`, cap `limit`.
- `assets.created_at` is the ingest-time meeting-date proxy (same proxy `search_past_meetings` already stamps on excerpts) — the block labels it as such.

`SharedMeeting` is a small frozen dataclass next to the repository (or a NamedTuple — match file style).

### 4.2 New fetch in `collect_meeting_prep_context`

`backend/routers/home.py:1336` already computes `counterparty_emails` (every attendee except the reader — internal colleagues included, which is exactly right for internal 1:1s). Add a sixth fail-soft coroutine to the existing `asyncio.gather`:

```python
async def fetch_prior_meetings():
    if not settings.CUBIE_PREP_ATTENDANCE_LOOKUP:
        return []
    try:
        emails = counterparty_emails
        if len(emails) > 15:  # all-hands guard: don't build a
            emails = [a["email"] for a in external_attendees]  # wall of history
        if not emails or len(emails) > 15:
            return []
        repo = SQLMeetingParticipantsRepository()
        return await anyio.to_thread.run_sync(
            repo.list_shared_meetings, user_email, emails, 8
        )
    except Exception as e:  # noqa: BLE001 — prep must survive
        context["errors"].append(f"Attendance lookup failure: {e}")
        return []
```

Result lands in `context["prior_meetings"]` (list of dataclasses) plus a pre-rendered `context["prior_meetings_block"]` string (see 4.3), mirroring how `attendee_intel` flows.

### 4.3 Prompt block + grounding rule

New formatter `_format_prior_meetings_block(shared: list[SharedMeeting]) -> str` in `home.py`, returning `""` for empty input (the UC-47 rule: **no block → no clauses → byte-identical prompt**, see `test_meeting_prep_attendee_intel.py` for the pattern). Non-empty shape:

```
## PRIOR MEETINGS WITH THESE ATTENDEES (attendance records — treat as ground truth)
You last met someone on this invite on Monday, August 04, 2026.
- Monday, August 04, 2026 — "Acme / Globex QBR" — with sam.lee@globex.example, jordan.park@acme.example
- Friday, June 27, 2026 — "Globex migration sync" — with sam.lee@globex.example
(up to 8 most recent; dates are ingest dates, normally within a day of the meeting)
```

Insertion points in `generate_meeting_prep_analysis` (`home.py:1608`):

- Sales branch: inside `## RESEARCH DATA`, immediately **before** `{context["past_meetings_report"]}`.
- Internal branch: inside `## CONTEXTUAL DATA`, same position.

Conditional grounding rule appended to **both** system instructions only when the block exists (exactly like `intel_grounding_rule`):

> `- PRIOR MEETINGS WITH THESE ATTENDEES comes from attendance records, not retrieval: its dates, titles, and who-was-there facts are authoritative — prefer them for any "last met" or "we have met N times" claim. What was *said* in those meetings must still come from transcript excerpts or other sources; the list proves the meetings happened, not their content.`

### 4.4 Persistence, Deep Prep, and flag

- **Flag:** `CUBIE_PREP_ATTENDANCE_LOOKUP: bool = True` in `rubik_core/config.py` (next to `CUBIE_DEEP_PREP_ENABLED`, line 143). Kill switch only; default on. Off ⇒ prompts byte-identical to today.
- **Run snapshot:** `_persist_meeting_prep_run` (`home.py:1936`) adds `"prior_meetings_block"` to `context_bundle` — the bundle's contract is "exactly the strings that go into the prompt."
- **Deep Prep:** `_run_deep_prep` (`home.py:2225`) calls `collect_meeting_prep_context` directly, and the council builds its `_context_block` from that same context — the block flows into Deep Prep with **no extra work** (verify at implementation that `deep_prep/council.py` renders unknown context keys or add it to its block explicitly).
- **Optional (deferred, not in the first PR):** a `restrict_doc_ids: set[str] | None` parameter on `search_past_meetings` (`rubik_core/adk_agent/tools.py`) that intersects with `accessible_doc_ids`, letting prep pull *content snippets* specifically from the shared meetings. Ship the metadata block first and measure; add this only if content-misses persist (it doubles retrieval work).

### 4.5 Tests — `backend/tests/test_meeting_prep_prior_meetings.py`

Follow the house style (narrative module docstring, direct calls to `collect_meeting_prep_context` with mocked fetchers, cf. `test_meeting_prep_attendee_intel.py`):

1. `test_shared_meetings_requires_reader_attendance` — doc where a counterpart attended but the reader didn't is excluded (the ACL property).
2. `test_shared_meetings_orders_newest_first_and_limits`
3. `test_shared_meetings_excludes_deleted_assets`
4. `test_shared_meetings_reports_which_counterparts_overlap`
5. `test_prep_context_and_bundle_include_attendance_block` — block present in prompt context and in the persisted `context_bundle`.
6. `test_no_shared_meetings_leaves_prompt_byte_identical` — and same with the flag off.
7. `test_attendance_lookup_failure_is_partial_error` — repo raises ⇒ `partial_errors`, prep succeeds.
8. `test_all_hands_guard_restricts_to_external_attendees`

### 4.6 Ship checklist

- [ ] `uvx ruff check --fix . && uvx ruff format .` (CI gates on it)
- [ ] Help page updated in **both** homes (`frontend/src/pages/Help.tsx` + `rubik_core/help/content.py`): Meeting Prep "now includes your actual meeting history with the people on the invite."
- [ ] No new AI-cost category needed (zero new LLM calls; prep already attributes to `meeting_prep`).
- [ ] After code review and release approval, run the guarded
  `scripts/platform/deploy.sh --env <private-env>` path. Nothing auto-deploys.
- **Measure:** `meeting_prep_runs.context_bundle` now records the block ⇒ spot-check a week of real briefs for "last met" correctness vs before (Q1/Q10).

**Estimate:** ~1 dev-day including tests.

---

## 5. Phase 1 — persisted relationship graph (schema + writers)

**Goal:** persist the entity/edge layer the priorities pipeline already derives, fed incrementally from flows that already run, so any feature can traverse identity instead of re-deriving it.

**Non-goals for Phase 1:** no LLM-extracted edges (Phase 2); no EOS edges (`eos_*` is already relational — SQL joins suffice); no Gmail/THREAD nodes (mailbox-scoped visibility — a shared edge derived from one user's mailbox would leak the *existence* of correspondence to others); no ticket edges yet; no derived/materialized edges (derive by traversal at read time); no graph database; no UI.

### 5.1 Ontology

- **Node types = the existing `EntityKind` enum verbatim** (`rubik_core/priorities/models.py:204`), including its documented per-kind id conventions (PERSON = lowercase email, COMPANY = lowercase domain, DEAL = HubSpot deal id, EVENT = Calendar event id, TRANSCRIPT = transcript doc_id, …). No parallel vocabulary.
- **Node key = `entity_key` = `f"{kind}:{id}"`** — the same convention as `ChannelSignal.entity_key`, so priorities signals join against the graph for free.
- **Keyspace warning (import from `backend/signals/correlator.py:236`):** transcript `event_id` and calendar `event_id` are different keyspaces. EVENT and TRANSCRIPT nodes are never merged or cross-keyed.

### 5.2 Tables

New models in `rubik_core/database/models.py`; the tables are in the baseline migration (`alembic/versions/0001_cubie_baseline.py`; dev runs `AUTO_MIGRATE`). When two branches each add a migration off the same head, the second to land re-points its `down_revision`, so revision ids need not ascend along the chain: run `alembic heads`, never a filename sort.

```python
class GraphEntity(Base):
    __tablename__ = "graph_entities"

    entity_key = Column(String, primary_key=True)  # "{kind}:{id}"
    kind = Column(String, nullable=False, index=True)  # EntityKind.value
    entity_id = Column(String, nullable=False)  # canonical per-kind id
    display_name = Column(String, nullable=True)  # metadata, never content
    attrs = Column(JSON, default=dict)  # small, non-authoritative cache
    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("kind", "entity_id"),)


class GraphEdge(Base):
    __tablename__ = "graph_edges"

    id = Column(String, primary_key=True)  # uuid4 hex (house style)
    src_key = Column(
        String,
        ForeignKey("graph_entities.entity_key", ondelete="CASCADE"),
        nullable=False,
    )
    dst_key = Column(
        String,
        ForeignKey("graph_entities.entity_key", ondelete="CASCADE"),
        nullable=False,
    )
    rel = Column(String, nullable=False)  # §5.3 vocabulary

    # Provenance — every edge answers "says who?"
    source = Column(
        String, nullable=False
    )  # 'meeting_participants' | 'hubspot_associations' | 'calendar' | 'domain_heuristic'
    method = Column(
        String, nullable=False, default="deterministic"
    )  # 'deterministic' | 'heuristic' | 'llm' (Phase 2)
    confidence = Column(Float, nullable=False, default=1.0)
    evidence_ref = Column(
        String, nullable=True
    )  # doc_id / HubSpot object id / event id
    observed_at = Column(
        DateTime, nullable=True
    )  # when the fact happened (meeting date, assoc seen)

    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    invalidated_at = Column(
        DateTime, nullable=True
    )  # soft delete; re-observation clears it
    attrs = Column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("src_key", "dst_key", "rel", "source"),
        Index("ix_graph_edges_src_rel", "src_key", "rel"),
        Index("ix_graph_edges_dst_rel", "dst_key", "rel"),
    )
```

Upsert semantics: `ON CONFLICT (src_key, dst_key, rel, source)` ⇒ bump `last_seen_at`, keep max `confidence`, clear `invalidated_at`. Edges are facts with provenance, not rows to churn.

### 5.3 Edge vocabulary v1 (deterministic only)

Direction convention: an edge reads "`src` `rel` `dst`".

| rel | src → dst | source | method / confidence | writer (hook point) |
|-----|-----------|--------|---------------------|---------------------|
| `attended` | person → transcript | `meeting_participants` | deterministic / 1.0 | inside `SQLMeetingParticipantsRepository.upsert_participants` (`repository.py:615`) — the single choke point every ingest path already goes through (`backend/routers/fathom.py:154`, `rubik_core/scripts/transcript_sync.py:369,595`, `sync_meet_transcripts.py:225`) |
| `invited_to` | person → event | `calendar` | deterministic / 1.0 | priorities calendar agent, which already emits person/company `related_entities` per external attendee (`rubik_core/priorities/agents/calendar_agent.py:183`) |
| `has_contact` | company → person | `hubspot_associations` | deterministic / 1.0 | write-through after `fetch_company_bundle` contacts fetch (`rubik_core/xray/fetchers.py:337`) and after Deep Prep `company_relationships_for_domains` (`hubspot_api.py:395`) |
| `has_deal` | company → deal | `hubspot_associations` | deterministic / 1.0 | same write-throughs (`fetchers.py:298`, `hubspot_api.py:441`) |
| `owned_by` | deal → person | `hubspot_associations` | deterministic / 1.0 | same (owner email via `owner_name_map` / `owner_directory`, `fetchers.py:291`, `hubspot_api.py:468`) |
| `member_of` | person → company | `hubspot_associations` **or** `domain_heuristic` | deterministic / 1.0 or heuristic / 0.8 | HubSpot contact→company assoc where available; else email-domain fold using the correlator's normalization (`correlator.py:114`) |

`attended` follows **set-replace semantics**: `upsert_participants` replaces a transcript's participant set, so the graph writer mirrors it — replace that transcript node's `attended` edges (invalidate ones no longer present). All writers: wrapped fail-soft (`try/except` + log), batched upserts, **zero new external API calls** — they only persist what the host flow already fetched. HubSpot-derived edges are "lazy-warmed": written whenever an X-Ray or Deep Prep run touches that company.

### 5.4 Store API — `rubik_core/graph/store.py`

Plain SQLAlchemy over the two tables; depth ≤ 2 means at most two indexed queries — no recursive CTEs, no graph DB.

```python
def upsert_entities(entities: Iterable[EntityStub], db: Session | None = None) -> None
def upsert_edges(edges: Iterable[EdgeStub], db: Session | None = None) -> None
def replace_edges(src_key: str, rel: str, source: str, dst_keys: list[str], ...) -> None   # set semantics (attended)
def ego(keys: list[str], rels: list[str] | None = None, depth: int = 1, limit: int = 200) -> EgoGraph
def filter_transcripts_for_reader(reader_email: str, keys: list[str]) -> list[str]         # delegates to meeting_participants
```

`ego()` is unfiltered infrastructure; **feature callers must apply §6 reader filters** — `filter_transcripts_for_reader` exists so none of them re-implements the gate.

### 5.5 Backfill

One cubie-worker job (background work never runs in-request — Cloud Run throttles post-response CPU):

1. `meeting_participants` × `assets` sweep → person/transcript nodes + `attended` edges. Pure SQL, thousands of rows, minutes.
2. HubSpot sweep: **skipped** — lazy-warm via X-Ray/Deep Prep runs is sufficient at a single company's scale; a sweep can be added later if coverage lags.

### 5.6 First consumers (fast follows, not Phase 1 deliverables)

- Phase 0's `list_shared_meetings` re-pointed at `ego()` (behavior-identical; proves the read path).
- Deep Prep `_fetch_crm_relationships` reads company/deals from the graph with live-API fallback and write-through.
- X-Ray meetings agent: company → `has_contact` persons → their `attended` transcripts, **filtered through the requester's accessible set** (preserves current behavior where X-Ray only reads meetings the requester attended), replacing the `f"meetings with {company_name}"` guess. Targets Q2/Q4/Q9.

### 5.7 Cost & scale

Zero new LLM calls; no new AI-cost category triggered (rule applies when Phase 2's extraction lands — that worker gets `@attribute_to` + a `_FEATURE_LABELS` entry in the same PR). Scale: ~10³ transcripts × ~5 participants + HubSpot associations ⇒ order 10⁴–10⁵ edges. Negligible for Cloud SQL; the pinned `db-custom-1-3840` instance is unaffected.

**Estimate:** 2–3 dev-days (models + migration + writers + store + backfill job + tests).

### 5.8 As built — what shipped, and where reality differed from this spec

Phase 1 landed as `rubik_core/graph/` (`keys`, `store`, `writers`, `backfill`), models `GraphEntity`/`GraphEdge` (tables in the baseline migration), job `backend/jobs/graph_backfill_job.py` (worker name `graph-backfill`), flag `CUBIE_GRAPH_WRITES_ENABLED` (default on), and 48 tests.

Four deviations from the plan above, each found while wiring the real hook points:

1. **Deep Prep cannot supply CRM edges.** `hubspot_api.company_relationships_for_domains` returns `{domain, found, company_name, won_count, open_count, open_value, last_won}` — it deliberately drops the HubSpot **company id**, and aggregates deals into counts rather than returning deal ids. There is nothing there to key `has_deal`/`has_contact` on. All rich CRM edges therefore come from the **X-Ray bundle** (`fetch_company_bundle`), which carries real ids. §5.3's claim that the Deep Prep path is a second write-through for those edges is wrong and is superseded by this note.
2. **`owned_by` is conditional.** X-Ray holds an owner **name** map (`owner_name_map`), not addresses, and a PERSON node is keyed on email. Rather than invent an address, `record_company_bundle` takes an optional `owner_emails` map and writes no owner edge without one. A caller holding `owner_directory()` output enables it.
3. **The calendar `invited_to` writer is deferred.** It sits in the priorities batch (per-user, 600 s budget) and would be the only writer on a hot path with a known incident history, so it is a follow-up rather than part of the foundational PR. `member_of` still gets written — from transcript attendance — so company nodes exist without it.
4. **`keys.py` does not import `EntityKind` at runtime.** `rubik_core.priorities.__init__` pulls in the orchestrator and every channel agent, and the calendar agent is itself a future graph writer — a module-scope import would close an import cycle the moment (3) lands. The kind strings are bound late and a test asserts each equals its `EntityKind` member.

Two API details worth knowing before writing a consumer:

- **`ego()` returns detached `NodeView`/`EdgeView` dataclasses, not ORM rows.** Returning live `GraphEdge` objects raised `DetachedInstanceError` on any access after the session closed, which would have made session lifetime every caller's problem.
- **Hops are literal.** `depth=2` from a person reaches their meetings (1) and co-attendees (2); a co-attendee's company is hop 3. `MAX_EDGES = 500` caps every traversal regardless of the `limit` asked for, and `EgoGraph.truncated` says when that bit.

`replace_edges` also gained an `anchor_side` parameter (`"src"`/`"dst"`): attendance is many-people → one-transcript, so the fixed end is the destination.

---

## 6. ACL rules (binding for every phase)

1. **Metadata only.** Nodes/edges carry ids, titles, dates, provenance — never transcript text, email bodies, doc content, or VTO material. VTO content never enters the graph in any form (per-line confidentiality).
2. **Content expansion only through existing access paths.** Transcript text via the `meeting_participants` gate; knowledge-base docs via department corpus fan-out; email via the reader's own mailbox. The graph never becomes an alternate read path for content.
3. **Reader-anchored traversal only.** Every feature read is an ego traversal anchored on the requesting user or on entities they already have access to (their meeting, their X-Ray company). No global browse/export endpoint — also the product posture: Cubie synthesizes conclusions, it doesn't expose raw data.
4. **Transcript metadata is reader-filtered.** Any surface rendering transcript-derived facts (titles, dates, attendance) passes the doc ids through the reader's `meeting_participants` set first (`filter_transcripts_for_reader`). A meeting you weren't in doesn't appear in *your* brief even as a title.
5. **Mailbox-derived facts stay out of the shared graph.** No THREAD nodes/edges in Phase 1 (see §5 non-goals).
6. **Pre-baked cross-document summaries are prohibited** (the GraphRAG "community summary" pattern) — they can't respect per-reader ACLs. Synthesis happens per-request over reader-visible context only.

## 7. Phase 2 preview (out of scope, gated by §3's kill rule)

One new edge type — `commitment` (person → commitment node → company/person, `method='llm'`, `evidence_ref=doc_id`, extracted by a cubie-worker pass over new transcripts at ingest) — giving X-Ray a commitments ledger and prep an "open items since you last met" section that cites its source transcript by construction. Requires its own AI-cost category, extraction-quality evaluation on a gold set, and the §3 gate passed first.
