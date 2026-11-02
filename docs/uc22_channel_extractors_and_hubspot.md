UC-22 — Channel extractors with entity tags + HubSpot signal
=============================================================

Status: SCOPING
Depends on: UC-20 (multi-agent pipeline), UC-21 (admin UI)
Blocks:    UC-24 (correlation framework)

Problem
-------

UC-20 ships per-channel agents that each apply their own "what to flag"
heuristic in a hardcoded prompt — Gmail flags "direct asks", Calendar
flags "external meetings", etc. This locks the pipeline to one department
(today: Sales) and means every new behavior is a per-channel prompt
edit. It also means signals carry only loose text, not structured
references — so the downstream decision-maker can't join "this email"
with "this HubSpot deal" with "this past meeting" by any consistent key.

The Gmail agent today only reads ``is:unread`` (fallback ``in:inbox``).
That misses the most common follow-up case: *I* sent an email and the
other party never replied. With no outbound visibility we cannot detect
no-reply situations, the single biggest "action without response" surface
the user described.

There is also no HubSpot signal feeding the UC-20 pipeline. Main's
``backend/signals/hubspot.py`` already pulls open / stuck / stale deals
from BigQuery for the ADK pipeline; UC-20 should consume the same data
without re-implementing the queries.

Scope
-----

Refactor every channel agent into a **pure extractor** that emits
**entity-tagged signals** with no judgment. Add HubSpot as a first-class
channel agent.

Concretely:

1. Replace ``ChannelSignal`` with a richer shape that carries an
   entity reference (``entity_kind``, ``entity_id``, ``entity_label``)
   and a free ``attrs`` dict per channel. Existing fields
   (``id``, ``title``, ``snippet``, ``link``, ``timestamp``) stay.
2. Define a closed set of ``entity_kind`` values so the decision-maker
   can do entity joins:
   ``person`` (email), ``company`` (domain), ``deal`` (hubspot deal id),
   ``event`` (calendar event id), ``thread`` (gmail thread id),
   ``document`` (drive file id), ``transcript`` (meet doc id).
3. Rewrite channel agent prompts so they extract structured observations
   only — no "is this important" judgment, no urgency, no highlights.
4. Rewrite the Gmail extractor to read **inbox + sent (last ~14 days)**,
   group messages into threads, and emit one signal per thread with
   reply-lag attributes.
5. Add ``HubSpotChannelAgent`` that reuses
   ``backend/signals/hubspot.py`` so UC-20 picks up the same BigQuery
   probes main already runs.
6. Replace ``ChannelDigest.highlights[]`` with ``ChannelDigest.summary``
   (a single descriptive sentence). The decision-maker no longer
   receives a "things this channel thinks matter" list — it works off
   the raw signals.

Out of scope
------------

- Any new judgment logic. That belongs in the decision-maker (UC-24).
- ClickUp / internal ticketing channel agent. Deferred per user
  guidance until the ticketing system lands.
- Slack channel agent. Main has ``backend/signals/slack.py`` — we'll
  wrap it in a later UC once UC-22's pattern is proven.

Data model changes
------------------

``ChannelSignal`` (extend in ``rubik_core/priorities/models.py``)::

    @dataclass
    class ChannelSignal:
        id: str
        channel: ChannelKind
        title: str
        snippet: str = ""
        link: str | None = None
        timestamp: str | None = None

        # NEW: entity reference for cross-channel joins
        entity_kind: EntityKind | None = None
        entity_id: str | None = None
        entity_label: str | None = None

        # NEW: replaces the loose `metadata` dict — typed per-channel
        # observations the decision-maker can read.
        attrs: dict[str, Any] = field(default_factory=dict)

``EntityKind`` enum (new)::

    class EntityKind(str, Enum):
        PERSON = "person"           # entity_id = lowercase email
        COMPANY = "company"         # entity_id = lowercase domain
        DEAL = "deal"               # entity_id = hubspot deal id
        EVENT = "event"             # entity_id = google calendar event id
        THREAD = "thread"           # entity_id = gmail thread id
        DOCUMENT = "document"       # entity_id = drive file id
        TRANSCRIPT = "transcript"   # entity_id = meet recordings doc id

``ChannelKind`` adds ``HUBSPOT`` (the enum value already exists; just
needs an agent class registered in ``CHANNEL_AGENT_REGISTRY``).

Channel agent contracts after the rewrite
-----------------------------------------

Every channel agent's ``build_prompt`` is rewritten to output a fixed
JSON shape with no editorial fields::

    {
      "summary": "1-sentence factual description of what this channel saw",
      "signals": [
        {
          "id": "...",
          "title": "...",
          "snippet": "...",
          "link": "...",
          "timestamp": "...",
          "entity_kind": "...",
          "entity_id": "...",
          "entity_label": "...",
          "attrs": { … channel-specific structured observations … }
        }
      ]
    }

Per-channel ``attrs`` keys (the LLM extracts these; the
deterministic Python fetch hydrates what's possible without an LLM)::

  GMAIL THREAD
    direction              "i_sent_last" | "they_sent_last" | "mixed"
    thread_idle_days       integer
    external_domains       string[]
    has_attachments        boolean
    mentioned_dates        ISO string[]  (extracted by the LLM)
    mentioned_amounts      ["$25,000", …] (extracted by the LLM)
    sender_emails          string[]
    recipient_emails       string[]

  CALENDAR EVENT
    start_iso              string
    end_iso                string
    external_attendees     string[]
    has_description        boolean
    has_attached_docs      boolean
    conference_url         string | null
    recurring_event_id     string | null

  MEET TRANSCRIPT
    transcript_excerpts    string[]   (relevant lines — extracted by LLM)
    commitments_made_by    {speaker_email: ["I'll send the deck", …]}
    open_questions         string[]
    decisions_recorded     string[]

  DRIVE DOCUMENT
    mime_type              string
    last_modified_iso      string
    owners                 string[]
    last_viewed_by_user    string | null

  HUBSPOT DEAL  (extracted via backend/signals/hubspot.py probes)
    stage                  string
    stage_age_days         integer
    deal_amount            float | null
    close_date_iso         string | null
    last_engagement_iso    string | null
    owner_email            string

Gmail extractor — concrete fetch plan
-------------------------------------

Replace the current ``GmailChannelAgent.fetch_signals``:

1. Query Gmail twice:
     - ``in:inbox newer_than:7d``
     - ``in:sent newer_than:14d``
2. Merge into one thread map keyed by Gmail ``threadId``.
3. For each thread, compute:
     - the messages in chronological order
     - which side sent the last message (user domain vs not)
     - ``thread_idle_days = now − last_message_internalDate / 86400``
     - the set of external domains in the participant list
4. Emit one ``ChannelSignal`` per thread with
   ``entity_kind=THREAD``, ``entity_id=threadId``.
5. The LLM call's only job is to read the last 1–2 messages of each
   thread and populate ``mentioned_dates`` / ``mentioned_amounts`` and
   classify ``has_decision_verb`` ("we agreed", "approved", "let's
   move forward"). No urgency, no flag — observation only.

HubSpot extractor — wiring plan
-------------------------------

A new ``rubik_core/priorities/agents/hubspot_agent.py``::

    class HubSpotChannelAgent(BaseChannelAgent):
        channel = ChannelKind.HUBSPOT

        def fetch_signals(self) -> list:
            from backend.signals.hubspot import (
                fetch_open_deals,
                fetch_stage_stuck_deals,
                fetch_stale_open_deals,
            )
            return [
                *fetch_open_deals(self.user_email),
                *fetch_stage_stuck_deals(self.user_email),
                *fetch_stale_open_deals(self.user_email),
            ]

        def parse_signals(self, rows) -> list[ChannelSignal]:
            # dedupe by deal_id (a deal can appear in multiple probes)
            # emit entity_kind=DEAL, entity_id=deal_id, with attrs
            …

        def build_prompt(self, signals) -> str:
            # Like every other extractor: factual summary + no judgment.
            …

Dedupe is important — a deal can appear in both "stale" and "stage
stuck" probes. We keep the union of attributes from both rows.

Test plan
---------

- ``test_channel_signal_carries_entity_tags`` (unit)
- ``test_gmail_extractor_threads_inbox_and_sent`` (mocked GmailClient)
- ``test_gmail_thread_lag_attributes_calculated`` (deterministic clock)
- ``test_hubspot_agent_dedupes_across_probes`` (mocked signal source)
- ``test_channel_prompts_contain_no_judgment_words``
  — regression test: greps "important", "urgent", "flag", "ignore"
  from the prompt strings to enforce the no-judgment rule.

Migration / rollout
-------------------

1. Land the schema changes (``ChannelSignal``, ``EntityKind``)
   first — they're backwards-compatible (new fields are optional).
2. Refactor channel agents one at a time, behind the existing test
   suite. Old prompts can be temporarily preserved as a fallback if
   the LLM returns empty ``attrs``.
3. Add the HubSpot agent + register in ``CHANNEL_AGENT_REGISTRY``.
4. Default ``PrioritiesRequest.channels`` becomes
   ``[GMAIL, CALENDAR, MEET, HUBSPOT]``; ``DRIVE`` stays available but
   off by default (still a stub).

Open questions
--------------

- Should the Drive extractor be made real in this UC, or remain a stub?
  Recommend: stub until "recent Drive activity" feed actually exists.
- Do we want ``attrs`` to be typed (pydantic per-channel models) or
  open dicts? Recommend: open dicts for v1; tighten if the LLM
  hallucinates keys.

Estimate
--------

Medium. The refactor is mechanical but the Gmail rewrite + HubSpot
wiring are real new code. ~4-6 hours of focused work.
