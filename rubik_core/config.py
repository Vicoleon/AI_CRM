# rubik_core/config.py
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from enum import Enum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMode(str, Enum):
    LOCAL = "local"
    GCP = "gcp"
    DEV_MOCK_AUTH = "dev_mock_auth"


#: The mock-auth developer identity when ``CUBIE_DEV_USER_EMAIL`` is unset.
DEFAULT_DEV_USER_EMAIL = "dev@acme.example"


class OrchestratorBackend(str, Enum):
    ADK = "adk"


class Settings(BaseSettings):
    APP_MODE: AppMode = AppMode.LOCAL
    ORCHESTRATOR_BACKEND: OrchestratorBackend = OrchestratorBackend.ADK
    ENVIRONMENT: str = "dev"
    LOG_LEVEL: str = "INFO"
    # Local dev origins only. A deployment adds its own public origin.
    ALLOWED_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
    ]
    ENABLE_MOCK_AUTH: bool = False
    # Who mock auth signs in as when no mock user is named (local development
    # only: mock auth never runs in gcp mode). Promoted to admin only when
    # APP_MODE is dev_mock_auth. A fictional address, never a real person's.
    CUBIE_DEV_USER_EMAIL: str = DEFAULT_DEV_USER_EMAIL

    AUTO_MIGRATE: bool = True  # Default to True, but start.sh will handle mode check

    # Platform-wide cost hold. Existing deployments keep their behavior until
    # the operator explicitly disables automatic/background processing.
    CUBIE_AUTOMATIONS_ENABLED: bool = True

    # EOS — alignment context auto-injected into every supervisor
    # turn. Set true to bypass for A/B comparison against the un-aligned
    # baseline.
    CUBIE_DISABLE_ALIGNMENT: bool = False

    # EOS — RAG ingestion of EOS data into the Vertex corpus.
    # Kill-switch for when running migrations, doing schema repair, or
    # exercising local code paths that would otherwise write real files
    # to dev GCS. Defaults to False (ingestion enabled).
    CUBIE_DISABLE_EOS_INGEST: bool = False

    # Debounce/coalesce for event-driven EOS re-ingestion (the edit hooks →
    # cubie-worker job; see rubik_core/ingestion/eos/debounce.py). The worker
    # waits until the newest edit is at least DEBOUNCE_SECONDS old before
    # extracting (so an L10 edit burst coalesces into one pass), bounded by
    # MAX_WAIT so constant editing can't starve ingestion. A dispatch claim
    # older than CLAIM_STALE is treated as dead (crashed worker / lost
    # launch) and is reclaimable by the next edit.
    EOS_REINGEST_DEBOUNCE_SECONDS: int = 120
    EOS_REINGEST_DEBOUNCE_MAX_WAIT_SECONDS: int = 600
    EOS_REINGEST_CLAIM_STALE_SECONDS: int = 1800

    USE_PRIVATE_IP: bool = False

    # WGLL feedback loop — when True (default), recent user ratings on Cubie's
    # proposals/messages are auto-injected into the priorities decision-maker
    # and chat coordinator prompts as "what good looks like" / "what to avoid"
    # exemplars. Safe no-op until ratings exist; flip to False to fully disable
    # the prompt injection without removing the feature.
    CUBIE_WGLL_FEEDBACK_ENABLED: bool = True

    # Drive-labels AI-Priorities signal — when True (default), recently-updated
    # Google Drive files carrying an importance label (Important / Cubie /
    # Project, per the admin-managed `drive_label_priorities` map) are emitted as
    # high-severity signals into AI Priorities. Safe no-op until the Workspace
    # admin grants the `drive.labels.readonly` DWD scope and labels exist; flip
    # to False to disable the source entirely.
    CUBIE_DRIVE_LABELS_SIGNAL_ENABLED: bool = True

    # When a Drive-label signal fires, also read a bounded excerpt of the file's
    # CONTENT so the judge grounds on what's *inside* the doc, not just its
    # name + label. Content is read via the user-delegated `…/auth/drive` scope
    # (already granted — powers meeting-prep / transcripts), so this needs NO
    # new DWD grant beyond the label read itself. Only the most recently
    # modified CUBIE_DRIVE_LABEL_CONTENT_MAX_FILES labeled files are read, each
    # capped at CUBIE_DRIVE_LABEL_CONTENT_MAX_CHARS, keeping token cost bounded
    # to the small labeled set. Kill switch: flip to False.
    CUBIE_DRIVE_LABEL_CONTENT_ENABLED: bool = True
    CUBIE_DRIVE_LABEL_CONTENT_MAX_CHARS: int = 4000
    CUBIE_DRIVE_LABEL_CONTENT_MAX_FILES: int = 7

    # ── Deep Prep: async, council-backed meeting prep delivered as an emailed
    # PDF (distinct from the on-screen brief). All knobs default to a safe/off
    # posture so the feature ships dark and is enabled deliberately.
    #   - ENABLED: master switch for generating deep preps at all.
    #   - EMAIL_AUTOSEND: global send kill switch. False → the sender no-ops
    #     ("disabled") so we can wire+test delivery end-to-end without a real
    #     send escaping.
    # Sender name/mailbox and recipient policy are saved per workspace in
    # Admin → Workspace operations. Dry-run previews recipients before rollout.
    CUBIE_DEEP_PREP_ENABLED: bool = False
    CUBIE_DEEP_PREP_EMAIL_AUTOSEND: bool = False

    # ── Meeting Prep, Phase 0 of the customer relationship graph
    # (docs/customer_relationship_graph.md): a deterministic "prior meetings
    # with these attendees" block read from meeting_participants — the
    # transcript attendance ACL — instead of hoping semantic retrieval surfaces
    # the history. Kill switch only: off restores the pre-feature prompt
    # byte-for-byte.
    CUBIE_PREP_ATTENDANCE_LOOKUP: bool = True

    # ── Meeting Prep v2: the newer signals a brief is built from
    # (rubik_core/meeting_prep/signals.py). One kill switch per source. Each
    # source is fail-soft and adds a prompt block only when it found something,
    # so OFF restores the pre-feature prompt for that source byte-for-byte.
    #
    # HubSpot account picture for EXTERNAL meetings: ambiguity-safe domain →
    # company match, open deals in the company's own pipelines (stage, amount, close
    # date, owner), won history, and the deal + company last-contact dates side
    # by side. A few read-only HubSpot calls per brief, in parallel with the
    # attendee research, so it adds no latency. Needs no gate beyond this: every
    # copilot user already reads HubSpot through Chat.
    CUBIE_PREP_CRM_ENABLED: bool = True
    # The latest Customer X-Ray already on file for each matched company (a
    # database read, never a new X-Ray run). Also gated per reader on the
    # "customer_xray" feature, whose audience is narrower than Meeting Prep's.
    CUBIE_PREP_XRAY_ENABLED: bool = True
    # The reader's own open to-dos owed to the people on the invite (promises
    # ledger). Also requires the reader to have My To-Dos or promise
    # follow-through (Admin → Feature Access, plus those features' own switches).
    CUBIE_PREP_PROMISES_ENABLED: bool = True
    # "What was agreed last time": the summary / action-items part of the two
    # most recent meetings the reader shared with these attendees (same
    # meeting_participants ACL as the attendance lookup; one storage read each).
    CUBIE_PREP_LAST_MEETING_NOTES_ENABLED: bool = True
    # Time off for INTERNAL attendees over the next 14 days, from the cached
    # ooo_periods table (no calendar calls).
    CUBIE_PREP_AVAILABILITY_ENABLED: bool = True
    # Today's AI Priorities cards that mention this meeting, an attendee or the
    # client's domain (a read of the reader's own cached card).
    CUBIE_PREP_FLAGGED_ENABLED: bool = True

    # ── Meeting Prep builds in the background (rubik_core/meeting_prep/
    # background.py). Opening a meeting with no brief yet, and Regenerate, no
    # longer hold the page for the 60-120s a brief takes: the request answers
    # 202 at once, the brief is built elsewhere, and the reader gets an in-app
    # notification linking back to it when it is ready (or could not be built).
    #
    # WHERE the build runs (the same choice X-Ray Deep Research makes in
    # rubik_core/xray/config.py::deep_runner_mode):
    #   "job"       — an execution of the existing "cubie-deep-prep" Cloud Run Job
    #                 (CPU always allocated, so the build finishes even though
    #                 the Service throttles CPU once the 202 is sent);
    #   "inprocess" — a task on the app's own event loop (correct locally; on
    #                 Cloud Run it can be starved, hence the Job there). The
    #                 PROD project has no cubie-deep-prep Job, so deploy_prod.sh
    #                 pins it to "inprocess";
    #   "auto"      — "job" when APP_MODE is gcp, "inprocess" everywhere else.
    # A Job launch that fails falls back to in-process, loudly logged. Each Job
    # execution is handed the Service's own TODOS_ENABLED,
    # PROMISE_ACCOUNTABILITY_ENABLED, CUBIE_PREP_ATTENDANCE_LOOKUP and every
    # CUBIE_PREP_*_ENABLED value, so turning a source off HERE, on the
    # Service, turns it off in background briefs too.
    CUBIE_PREP_RUNNER: str = "auto"
    # A build still queued/running after this many minutes is reported failed
    # ("Cubie took too long to build this briefing — try again.") and no longer
    # blocks a fresh one. A normal build takes 1-2 minutes; this is far above
    # that so a slow-but-live build is never pre-empted. The check runs whenever
    # the reader looks at that meeting AND on the notifications feed every
    # signed-in tab polls, so the "couldn't be built" notification reaches a
    # reader who left. A Job execution is capped at this long too. A build that
    # does finish later still becomes the next version and notifies — unless
    # the reader has since got a newer version or started another build of
    # that meeting, or it is a Cloud Run retry; then it stays failed.
    CUBIE_PREP_BACKGROUND_TTL_MINUTES: int = 20
    # A build still QUEUED after this many minutes never started (a worker
    # marks it running within seconds — e.g. a Job execution that died on
    # start during a deploy window) and is reported failed sooner: "Cubie
    # couldn't start building this briefing — try again."
    CUBIE_PREP_QUEUED_TTL_MINUTES: int = 5
    # How many background builds one reader may have in flight at once, across
    # meetings. Starting one more answers 429 ("You have several briefings
    # building already — wait for one to finish."); opening a meeting whose
    # build is already running joins it and is never refused.
    CUBIE_PREP_MAX_INFLIGHT_PER_USER: int = 5

    # ── Relationship graph (Phase 1 of docs/customer_relationship_graph.md):
    # persists nodes/edges from data the host flow already fetched — transcript
    # attendance, HubSpot associations. Writes are fail-soft, run in their own
    # session, and add no external calls; the cost is a handful of small INSERTs
    # per transcript. Kill switch for exactly one situation: if the :00/:15
    # transcript-sync burst is ever traced to these writes, flip it off and the
    # ingest path is byte-for-byte what it was before. Nothing reads the tables
    # yet, so off is safe — it only stops the graph filling in.
    CUBIE_GRAPH_WRITES_ENABLED: bool = True

    # Reading the graph back out (spec §5.6 consumers). Separate from the write
    # flag on purpose: writes can stay on while reads are switched off, which is
    # what you want if a consumer misbehaves — the graph keeps collecting and no
    # feature's prompt changes. Off ⇒ every consumer falls back to exactly the
    # behaviour it had before the graph existed.
    CUBIE_GRAPH_READS_ENABLED: bool = True

    # ── UC-47 Attendee intelligence: researches each EXTERNAL meeting attendee's
    # public professional presence (position, posts, talks) and reads their
    # communication/decision style from it, for the Roles & Dynamics panel and
    # the prep prompts. Colleagues are never profiled.
    #
    # Default ON because the default source needs no new vendor or credential:
    # Gemini + Google Search grounding over public profiles/posts Google has
    # already indexed. Set False to remove the panel and the prompt block.
    ATTENDEE_INTEL_ENABLED: bool = True
    # Optional higher-fidelity path: a LICENSED LinkedIn-data API (the wire shape
    # targeted is Proxycurl's; _BASE re-points it at any provider matching it).
    # Its key is a workspace-bound secret. Without one, grounded search is used.
    # Nothing in this repo
    # scrapes linkedin.com: it breaks their terms, breaks on every markup change,
    # and gets our single egress IP blocked for the whole company at once. See
    # docs/uc47_attendee_intelligence.md.
    LINKEDIN_DATA_API_BASE: str = "https://nubela.co/proxycurl"
    LINKEDIN_DATA_API_TIMEOUT: float = 20.0

    # Gates the L10 AI features — the AI-generated recap of a finished
    # session today, and the in-room copilot later. Default on for the pilot;
    # set false to hard-disable all in-meeting AI generation + endpoints.
    CUBIE_L10_AI_ENABLED: bool = True

    # ── Post-meeting L10 summary email (backend/services/l10_summary_email.py):
    # a recap of the meeting's RECORDING plus that session's own EOS entries,
    # mailed to the people the transcript shows were actually in the room, and
    # only to those of them the L10's own ACL already lets read it (so the Exec
    # L10's recap reaches Exec members only).
    #   - ENABLED: master switch. Off ⇒ the sweep returns immediately, spends
    #     nothing, and the scheduler endpoint is a no-op.
    #   - EMAIL_AUTOSEND: send kill switch, checked BEFORE the summary is
    #     generated so a dark rollout costs no tokens. Off ⇒ every eligible
    #     session reports "disabled" and stays unstamped, so flipping this on
    #     later mails everything still inside the lookback window.
    #   - LOOKBACK_HOURS: how far back a finished session may be and still be
    #     mailed. Must comfortably exceed the worst transcript delay (Gemini
    #     publish + the 15-minute transcript sync) plus any scheduler outage.
    # Sender mailbox and the go-live floor live in Workspace operations. The
    # sweep's dry-run previews the exact authorized audience before any send.
    CUBIE_L10_SUMMARY_EMAIL_ENABLED: bool = False
    CUBIE_L10_SUMMARY_EMAIL_AUTOSEND: bool = False
    CUBIE_L10_SUMMARY_LOOKBACK_HOURS: int = 72
    #   - DIGEST_ONLY: whether a meeting that was never recorded still gets a
    #     recap, written from what the team entered in its L10 (closing notes,
    #     ratings, check-ins, issues solved, to-dos). Teams that don't switch
    #     Gemini/Fathom on heard nothing at all before this. It waits out the
    #     full recording window first, so it can never pre-empt a late
    #     transcript, and it sends nothing for a meeting with no entries.
    #     Turn OFF to go back to recorded-only recaps.
    CUBIE_L10_DIGEST_ONLY_ENABLED: bool = True

    # Phase 6 Rollout & Feature Flags
    DEPLOYMENT_ROLE: str = "all"  # Options: "all", "api", "worker"
    AGENT_MANAGEMENT_MODE: str = (
        "self_managed"  # Options: "self_managed", "platform_managed"
    )
    ENABLE_MULTI_AGENT_RUNTIME: bool = True
    ENABLE_MCP_INTEGRATIONS: bool = True
    ENABLE_MANAGED_SESSIONS: bool = False
    # A2A specialists (remote CRM/KB microservices) were retired: KB
    # (UC-1 RagManager.query does the multi-corpus fan-out in-process) and
    # CRM (HubSpotRetriever serves CRM in-process via BigQuery — see
    # AgentTools.search_hubspot_data). No A2A specialist remains.

    # GCP Config
    GCP_PROJECT_ID: str | None = None
    GCP_REGION: str = "us-central1"
    GCS_BUCKET_NAME: str | None = None
    # Prefix in the GCS bucket for uploaded assets. Every object path is built
    # under it, so it is never empty; a deployment may set its own.
    GCS_RAG_PREFIX: str = "workspace"

    # Vertex models — single source of truth. Every Gemini call site reads
    # one of these three tiers; bump the version here when Google ships a
    # new family (e.g. 2.5 → 3 → 3.1) and the whole app moves at once.
    # Gemini 3 preview models are served on the global endpoint only —
    # regional endpoints (us-central1) return a 404 not-found. The
    # VERTEX_LLM_LOCATION setting below routes every genai.Client through
    # "global" by default.
    #
    # IMPORTANT — prefer GA aliases over *-preview ids for any user-facing
    # tier. Preview model ids (e.g. "gemini-3-flash-preview") have NO
    # dedicated per-project quota bucket on the global endpoint; they ride
    # Google's Dynamic Shared Quota and 429 (RESOURCE_EXHAUSTED) under even
    # light load — and that quota CANNOT be raised by request. GA aliases
    # ("gemini-flash-latest", "gemini-2.5-pro", …) draw on the large GA
    # quota already provisioned for this project. FLASH/PRO were moved off
    # "gemini-3-flash-preview"/"gemini-3.1-pro-preview" on 2026-06-25 after
    # the preview aliases repeatedly 429'd chat + the briefing pre-UAT.
    #
    # Floating "-latest" aliases auto-follow Google's newest model for a
    # tier (lower maintenance — no manual bump when a family is deprecated),
    # at the cost of Google being able to silently change what they point
    # at. As of 2026-06-25 only Flash/Flash-Lite have a servable "-latest"
    # alias on Vertex; "gemini-pro-latest" and "gemini-2.5-pro-latest" both
    # 404, so PRO is pinned to the GA "gemini-2.5-pro". TODO: flip PRO to
    # "gemini-pro-latest" once Google ships it on Vertex (re-probe; today it
    # 404s) — that restores auto-latest behavior for the Pro tier too.
    #
    # 2026-08-12 — Gemini 3.x upgrade, off Google's 2.5-Flash upgrade notice.
    # LITE moved 3.1 → 3.5 Flash-Lite (scores above 2.5 Flash at materially
    # higher throughput). Google's recommended settings are (a) these model ids,
    # (b) thinking level "minimal" (LITE_LLM_THINKING_LEVEL below — note chat
    # deliberately runs higher, see the RCA note there), and (c) thought-signature
    # circulation on multi-turn calls (ADK's session store already round-trips
    # those — see orchestration/session_service.py). Both are GA ids, not
    # *-preview, so they draw on GA quota.
    #
    # FLASH moved OFF the floating "gemini-flash-latest" alias to a pinned
    # gemini-3.6-flash. Probed 2026-08-12, that alias was still serving Gemini
    # 2.5 Flash (it rejects thinking_level with a 400) — so it was exactly the
    # "2.5 Flash workload" Google's notice was about. Pinned rather than left
    # floating because the family is what selects the thinking knob, and the
    # alias STRING is all gemini_tuning can match on: the day Google repoints
    # the alias to 3.x, thinking_budget stops applying — silently, since 3.x
    # accepts numeric budgets and ignores them rather than erroring. That drift
    # would not trip a single health check. A pinned id can't drift.
    #
    # 2026-09-08 — FLASH 3.6 → 3.8 Flash, off Google's 3.8 release notice.
    # Probed live on Vertex global before pinning (the announcement names a
    # product, not a servable id, and this repo has been burned by both 404s and
    # starved-quota 429s): "gemini-3.8-flash" answers 200 with
    # trafficType=ON_DEMAND, i.e. the GA bucket, not the preview one. The
    # negative control matters as much as the probe — unknown ids do return a
    # JSON 404 NOT_FOUND here ("gemini-9.9-flash", "gemini-4.0-flash"), so a 200
    # means something.
    #
    # Only the FLASH tier moves: "gemini-3.8-flash-lite" and "gemini-3.8-pro"
    # both 404, matching a notice that named Flash alone. LITE therefore stays on
    # 3.5 Flash-Lite (which still accepts "minimal" — 3.7+ does not) and PRO
    # stays on 2.5 Pro, which the priorities summarizer/decision_maker/critic all
    # depend on. Do not "finish the upgrade" by bumping those to 3.8 ids; they do
    # not exist.
    #
    # 2026-09-17 — PRO re-probed in full, and the answer is still "there isn't
    # one". Every Pro-tier 3.x id 404s on Vertex global for this project:
    # 3.8/3.7/3.6/3.5/3.1/3-pro, plus both "-latest" aliases. The negative
    # controls ("gemini-9.9-pro", "gemini-4.0-pro") 404 with the identical
    # message, which is what makes the result meaningful rather than a blanket
    # error — and "gemini-2.5-pro" answered 200/ON_DEMAND in the same sweep, so
    # the probe itself was sound.
    #
    # Read that carefully before "fixing" this line: PRO is NOT a deferred
    # upgrade someone forgot. It is an availability limit, and bumping it to a
    # 3.x id would 404 the priorities summarizer/decision_maker/critic, X-Ray's
    # analyst, proposals and the Deep Prep council — every heavy job at once.
    # The real open question is the opposite direction (should those jobs move
    # DOWN to the FLASH tier, which is two generations newer and ~4x cheaper?),
    # and that is a quality question a liveness probe cannot answer. It needs a
    # replay benchmark on real inputs, not another round of id guessing.
    #
    # 2026-10-14 — Gemini 4 Argon serves, and PRO still stays on 2.5 Pro. Off
    # Google's Argon launch email. "gemini-4-argon" answers 200/ON_DEMAND on
    # Vertex global through plain generate_content (what ADK uses; the
    # Interactions API the email describes is not required), while
    # "gemini-4-argon-preview" and the "gemini-9.9-argon" control 404 with the
    # same message, and 2.5 Pro answered 200 in the same run. With --workload
    # --grounding --trials 3 (scripts/probe_models.py --ids) it served a real
    # turn on the tuned call shape and ran a live Google Search. Priced on the
    # rate card at the INTRODUCTORY $2/$10 per 1M (cost/pricing.py).
    #
    # It needed a code fix before it could serve anything here: gemini_tuning
    # picked the 3.x knob off a "gemini-3" substring, so Argon fell through to
    # the 2.5 thinking_budget, which Argon rejects with a hard 400 on every
    # call (0 included). Argon takes low/medium/high and 400s on "minimal",
    # like 3.7+. The family is now read off the parsed version.
    #
    # Why PRO is not flipped in the same change: a liveness probe is not a
    # quality verdict, and this setting is NOT one job's model. A flip moves
    # every PRO consumer at once — the priorities summarizer/decision_maker/
    # critic (via the "gemini-pro" alias), the Deep Prep council, Smart Teams'
    # guide, the Meeting Prep rewrite, the Sales roles that default to PRO
    # (proposals, call-capture extraction, the X-Ray analyst and fast score),
    # the home-page and email routers, the EOS rock draft
    # and carry-over re-draft, L10, email action items and the L10 summary
    # email. Chat is NOT on that list: it runs agent_runtime.coordinator on the
    # FLASH tier, and adk_agent/agent.py's PRO "default_model" only feeds the
    # ADK CLI. Argon is also not in CHAT_MODEL_CATALOG on purpose (chat needs
    # speed; Argon is built for long deep work). The decision is per job, and
    # it runs both ways: up to Argon, or down to FLASH (3.8 Flash), the open
    # question above.
    #
    # Instead of a tier flip, Argon is an admin CHOICE per job, for four jobs:
    # each AI Priorities step (Admin → AI Priorities) and the Deep Prep brief
    # (Admin → Sales Models). It is offered only as
    # "gemini-4-argon@high", because Argon at the level it would otherwise
    # inherit (CHAT_LLM_THINKING_LEVEL, "medium") thought 0 tokens on every one
    # of those jobs, while 2.5 Pro thinks thousands. Defaults stay on PRO.
    VERTEX_LLM_MODEL_ARGON: str = "gemini-4-argon"
    VERTEX_LLM_MODEL_LITE: str = "gemini-3.5-flash-lite"
    VERTEX_LLM_MODEL_FLASH: str = "gemini-3.8-flash"
    VERTEX_LLM_MODEL_PRO: str = "gemini-2.5-pro"
    VERTEX_LLM_LOCATION: str = "global"
    VERTEX_EMBEDDING_MODEL: str = "text-embedding-004"

    # Chat LLM-call resilience. The chat orchestrator wraps the WHOLE turn in a
    # 90s wall-clock (backend/routers/chat.py REQUEST_TIMEOUT_SECONDS). Without a
    # per-call cap, one slow/hung Vertex generate_content silently eats that
    # entire budget — pilot req bb86676d spent 79s inside the first FLASH turn
    # before emitting a single tool call, then got killed. CHAT_LLM_CALL_TIMEOUT_MS
    # bounds each model HTTP call so a stall fails fast (well under the wall-clock,
    # even if retried once); CHAT_LLM_RETRY_ATTEMPTS auto-retries transient
    # 429/503/5xx (the documented Vertex resource-exhausted blips). Total attempts,
    # not retries-on-top — so 3 = up to 3 tries.
    CHAT_LLM_CALL_TIMEOUT_MS: int = 35_000
    CHAT_LLM_RETRY_ATTEMPTS: int = 3
    # Statuses the client retries on the SAME model. 504 is left out on purpose:
    # the per-call timeout above travels to Vertex as the server deadline, so a
    # 504 DEADLINE_EXCEEDED means the call already used its full 35s, and a
    # same-model retry just spends another 35s (three 504s in a row cost
    # ~110s, then an error). Those go to the fallback instead.
    CHAT_LLM_RETRY_STATUS_CODES: list[int] = [408, 429, 500, 502, 503]
    # Second model a chat turn switches to, once, when the chat model fails
    # upstream before streaming anything (rubik_core/adk_agent/fallback_llm.py).
    # Same 3.x family and price as VERTEX_LLM_MODEL_FLASH; probed on 2026-10-01
    # at 1-3s per call on 4 of 4 tries (3.7-flash took 15-55s on 3 of 4).
    # "" turns the fallback off.
    CHAT_LLM_FALLBACK_MODEL: str = "gemini-3.6-flash"

    # Curb Gemini "thinking" latency on the chat coordinator + sub-agents. The
    # default dynamic thinking budget spends 1-5s of reasoning tokens before the
    # first answer/tool-call token — the same stall class as the 79s incident
    # above. 0 disables thinking on Flash / Flash-Lite tiers; -1 keeps the
    # model's dynamic default. Ignored on Gemini 2.5 Pro (thinking can't be
    # disabled) and mapped to thinking_level="low" on Gemini 3.x — see
    # rubik_core/adk_agent/model_tuning.py.
    #
    # Raised from 0 to 512 after the "Migration Plan" hallucination RCA: with
    # thinking fully off, the Flash model skipped the "should I even search, and
    # for what?" step and confabulated a query. A small budget buys that
    # grounding check for a bounded first-token cost. Still env-tunable.
    CHAT_LLM_THINKING_BUDGET: int = 512

    # Gemini 3.x counterpart of CHAT_LLM_THINKING_BUDGET. The 3.x families took
    # the numeric budget away and replaced it with a coarse level, so the two
    # settings never apply to the same call — rubik_core/gemini_tuning.py picks
    # one by model family. Valid: "minimal" | "low" | "medium" | "high", or ""
    # to leave the model's own default alone (the -1 escape hatch's twin).
    #
    # There are TWO levels because the tiers do different work, and measured on
    # gemini-3.6-flash (2026-08-12) the levels are not close:
    #     level=minimal → 0 thinking tokens   (thinking fully OFF)
    #     level=low     → ~11 thinking tokens (on, bounded)
    #     budget=512    → ~13, i.e. IGNORED — 3.x silently drops numeric budgets
    #
    # CHAT_LLM_THINKING_LEVEL covers the chat coordinator + sub-agents (the FLASH
    # tier). It must buy real thinking tokens: zero thinking is precisely the
    # state the "Migration Plan" hallucination RCA produced — the model skipped
    # the "should I even search, and for what?" step and confabulated a query.
    # That RCA is why the 2.5 budget went 0 → 512.
    #
    # "medium" as of 2026-09-08, NOT "low" — the level scale was re-based under
    # us when FLASH moved to 3.8. Measured on one reasoning prompt, same day,
    # same endpoint:
    #     gemini-3.6-flash  low    → 573 thinking tokens
    #     gemini-3.8-flash  low    →   0 thinking tokens
    #     gemini-3.8-flash  medium → 573 thinking tokens
    #     gemini-3.8-flash  high   → 576 thinking tokens
    # So 3.8's "low" is roughly what "minimal" used to be, and "medium" is the
    # new "low". Carrying "low" across the model bump would have put chat back in
    # the RCA state — silently, with every health check still green, because a
    # supported level that thinks less is not an error. If FLASH is ever rolled
    # back to a 3.6-or-older id, roll this back to "low" in the same change.
    #
    # "minimal" is not merely discouraged here, it is unsendable on 3.7+: it 400s
    # the whole turn. gemini_tuning floors it (see _MINIMAL_DROPPED_AT) so a
    # stale env value or an old Admin → Chat pick degrades latency instead of
    # breaking chat outright.
    CHAT_LLM_THINKING_LEVEL: str = "medium"

    # …and the Flash-Lite tier gets "minimal" — Google's recommended setting for
    # 3.5 Flash-Lite, and what makes it as fast as 2.5 Flash while scoring
    # higher. Safe here where it is NOT safe on chat, because every LITE call
    # site is one-shot extraction or summarization (Slack catch-up, DAM
    # descriptions, product discovery) with the source text supplied in the
    # prompt. There is nothing to ground and no tool to choose, so the failure
    # the RCA describes has no way to occur. Still valid on the LITE tier only
    # because LITE is pinned to 3.5 Flash-Lite; Gemini dropped "minimal" at 3.7,
    # so this setting becomes unsendable the day LITE moves to a 3.7+ id.
    LITE_LLM_THINKING_LEVEL: str = "minimal"

    # Sampling temperature for the chat coordinator. Lowered to 0.2 after the
    # "Migration Plan" RCA — the default (~1.0) let the model wander off the
    # user's request into unprompted, invented searches. Low but non-zero keeps
    # answers natural while sharply cutting confabulation. Env-tunable.
    CHAT_LLM_TEMPERATURE: float = 0.2

    # Runaway-loop backstop for the chat coordinator. ADK defaults to 500 LLM
    # calls per turn, so a mis-looping agent grinds until the 90s wall-clock
    # fires with a generic "heavy load" error. Cap it to the deepest legitimate
    # turn so a loop fails fast and diagnosably instead. 0 disables the cap.
    CHAT_MAX_LLM_CALLS: int = 15

    # HITL (calendar/draft) confirmation pointer TTL. When the coordinator asks
    # to confirm an action it parks `ChatSession.last_adk_invocation_id`; the
    # next short affirmation ("yes") resumes it. Bound how long a parked pointer
    # stays honored so a much-later "ok" can't silently resume a stale action
    # (the "Migration Plan" RCA — no TTL + substring match). 0 disables the TTL.
    CHAT_HITL_PENDING_TTL_SECONDS: int = 900  # 15 minutes

    # Shared google-genai client resilience (rubik_core/models.py). Covers every
    # direct generate_content call OUTSIDE the ADK loop — HubSpot intent
    # planning, web search, daily briefing, priorities batch, embeddings. Retry
    # auto-heals transient 429/503/5xx; the timeout is a hang backstop set ABOVE
    # the 90s chat budget so it never changes chat behavior, only kills a true
    # infinite hang in a batch path. Timeout is per HTTP attempt, in ms.
    GENAI_CLIENT_RETRY_ATTEMPTS: int = 3
    GENAI_CLIENT_TIMEOUT_MS: int = 120_000

    # Server-side kill switch for the streaming chat endpoint (/api/chat/stream).
    # Additive + dormant: the default (non-streaming) /api/chat/ path is
    # unchanged, and the frontend only calls /stream when a client opt-in is set,
    # so this can stay True in prod with zero user-facing effect until the
    # frontend flips streaming on. Set False to hard-disable the endpoint.
    CHAT_STREAMING_ENABLED: bool = True

    # Vertex context caching for the chat coordinator (item 5). The ~6KB static
    # system prompt + the 20 tool declarations are otherwise re-prefilled AND
    # re-billed on every LLM call. Caching that stable prefix cuts per-call
    # prefill latency and yields up to a 90% input-token discount on the cached
    # portion. Requires the static prompt to live in Agent.static_instruction
    # (dynamic per-turn context — alignment/WGLL/user/date — stays in
    # `instruction`). ADK only caches when the prefix exceeds MIN_TOKENS, so a
    # too-small prefix silently no-ops rather than erroring. Set ENABLED False to
    # revert the Runner to the plain (non-cached) agent path.
    CHAT_CONTEXT_CACHE_ENABLED: bool = True
    CHAT_CONTEXT_CACHE_TTL_SECONDS: int = 600
    CHAT_CONTEXT_CACHE_MIN_TOKENS: int = 2048
    CHAT_CONTEXT_CACHE_INTERVALS: int = 5

    # Vertex AI RAG Corpus
    VERTEX_RAG_CORPUS_ID: str | None = None  # full resource name or just the numeric ID
    VERTEX_RAG_LOCATION: str = "us-south1"  # Region where the RAG Corpus lives

    # RAG relevance gating (chat-hallucination RCA — Part B).
    # Vertex `retrieval_query`'s `vector_distance_threshold` returns only
    # contexts whose vector distance to the query is BELOW this value, so LOWER
    # = stricter (fewer, closer matches). Dropped 0.7→0.5 after the "Migration
    # Plan" incident, where a nonexistent document still returned 5 loosely
    # matching chunks that the tool shaped as `status="success"`.
    RAG_DISTANCE_THRESHOLD: float = 0.5
    # A returned hit whose vector distance exceeds this is treated as
    # weak/tangential: it is dropped from the knowledge-base report, and when
    # NO hit is stronger than this the tool returns `status="low_confidence"`
    # instead of `success`. Must be <= RAG_DISTANCE_THRESHOLD to have any effect.
    # NOTE: gating only applies when the observed `ctx.score` values are
    # internally consistent with distances (all <= the active threshold); if
    # they look like similarities or are unpopulated the gate fails OPEN (never
    # suppresses) — see search_knowledge_base. Tune from the logged scores.
    RAG_WEAK_HIT_DISTANCE: float = 0.45
    # How much of a retrieved chunk the model actually gets to read. Ingestion
    # chunks at 512 tokens (~2,000 chars), so this shows a hit WHOLE; the old
    # hard-coded 600 threw away ~70% of every chunk, and in a lookup table that
    # discarded slice is where the row the user asked about tends to live.
    RAG_HIT_EXCERPT_CHARS: int = 2400
    # A document the user named by title is read whole, not sampled: "is X in
    # the client acronym list?" is answered by one row that can sit anywhere in
    # the file. The previous hard-coded 8,000-char cap cut a 407-row client
    # acronym export at ~30%, so every client past that point came back as
    # "not in the list" (one sat at char 21,997 of 27,662).
    # Per document, then a shared budget across the documents one search
    # resolves. Any clip that still happens is announced in the text the model
    # reads — see _clip_with_truncation_notice.
    KB_NAMED_DOC_MAX_CHARS: int = 120_000
    KB_NAMED_DOC_TOTAL_MAX_CHARS: int = 240_000

    # Vector DB (Legacy - kept for backward compat / local dev)
    CHROMA_PERSIST_DIR: str = "./storage"

    # Data
    LOCAL_DATA_DIR: str = "./data"
    DAM_DATA_DIR: str = "./dam_data"

    # HubSpot calls use workspace-bound read/write secret refs. This is only
    # the shared public REST API origin.
    HUBSPOT_API_BASE: str = "https://api.hubapi.com"

    # How long a deal can sit untouched before AI Priorities stops treating it as
    # an action item. Staleness used to make a deal *more* urgent without limit,
    # so a record nobody had touched in 254 days was cited as proof an invoice
    # was a "critical financial deadline" — it had closed long before; HubSpot
    # just never caught up. Past PRESUMED_DEAD the signal is capped to `low`
    # urgency and re-worded as "the record is likely out of date"; past MAX_AGE
    # it is dropped. See backend/signals/hubspot.py. Tune here if the sales
    # cycle changes (today: stale at 21d, stuck at 14d, so 90/180 is generous).
    HUBSPOT_DEAL_PRESUMED_DEAD_DAYS: int = 90
    HUBSPOT_DEAL_MAX_AGE_DAYS: int = 180

    # Close-date credibility. A sales leader does this by hand: they read a
    # deal's estimated close date, compare it to how long the client has
    # actually been silent, and ask the AE "why do you think it'll close by
    # then?". HORIZON is how near the date has to be before the question is
    # worth asking; QUIET is how long the client must have been silent before
    # the date stops being believable. 45/21 mirrors the sales cycle — deals are
    # already treated as stale at 21 days — and both are tunable from the
    # environment so Sales ops can move them without a deploy.
    HUBSPOT_CLOSE_DATE_HORIZON_DAYS: int = 45
    HUBSPOT_CLOSE_DATE_QUIET_DAYS: int = 21
    # The watch band: quiet enough for a manager to keep an eye on, not yet
    # enough to interrupt the rep. Deals between WATCH and QUIET appear ONLY in
    # the weekly roll-up, never as a card.
    HUBSPOT_CLOSE_DATE_WATCH_DAYS: int = 14

    # CRM chat feedback-loop: every CRM chat query emits one structured
    # "[crm-query]" log line recording how it was routed and whether it resolved
    # (see HubSpotAPIRetriever._log_crm_query). Gaps (clarify/not_found/multi_match/
    # gate_miss/…) log at WARNING so unhandled phrasings surface in Cloud Logging
    # instead of silently degrading. When False the raw query text is omitted from
    # the line (only routing fields are logged); default True — the same Cloud
    # Logging trust boundary already receives the CRM context blocks.
    CRM_QUERY_LOG_INCLUDE_TEXT: bool = True

    # UC-46 CRM Sync — reads an AE's call transcripts, diffs them against HubSpot,
    # and proposes CRM updates the AE approves one at a time. Nothing is ever
    # written without an explicit per-item human approval.
    CUBIE_CRM_SYNC_ENABLED: bool = False

    # UC-45: stamp each researched company in HubSpot with a link back to its
    # Customer X-Ray page, so research reaches reps where they already work.
    # Off = research still runs, HubSpot just isn't touched. This is a kill
    # switch for the writeback alone; a missing workspace write token is the
    # harder gate (every write is refused without it, whatever this says).
    XRAY_CRM_LINK_ENABLED: bool = True
    # How far back an on-demand scan looks for transcripts. The nightly scan uses
    # its own 25h window (1h of overlap, absorbed by suggestion dedupe).
    CRM_SYNC_SCAN_DAYS: int = 7
    # Suggestions the model is less sure about than this are dropped at scan time
    # rather than shown — a noisy queue is what kills trust in the whole feature.
    CRM_SYNC_CONFIDENCE_MIN: float = 0.5
    # A dismissed suggestion stays suppressed this long. Dismissal usually means
    # "not now", so a fact still being asserted on new calls a month later earns
    # a fresh ask (with new evidence) rather than silence forever.
    CRM_SYNC_DISMISS_SUPPRESS_DAYS: int = 30
    # Pending suggestions older than this are swept to `expired` at the start of
    # the next scan, so a stale queue never accumulates against live CRM state.
    CRM_SYNC_PENDING_EXPIRE_DAYS: int = 14

    # ── Sales Call Capture (CCS) + Cubie Sales Cockpit ───────────────────────
    # Two coupled features. CCS captures a sales call —
    # Meet space, transcript, LLM extraction — and proposes HubSpot writes the
    # AE approves in a wrap-up screen; the Cockpit turns what that call promised
    # into a commitment with an aging clock Cubie owns.
    #
    # EVERY FLAG BELOW DEFAULTS OFF, and each one names the grant it waits on.
    # Two of the external dependencies (Meet DWD scopes, HubSpot Lead scopes)
    # are admin actions outside this repo, so the
    # normal state of a fresh environment is *partly* provisioned — and a
    # half-provisioned environment must not half-run the feature. A router whose
    # master switch is off returns 404; a scheduled-job endpoint no-ops with a
    # log line. Turning any of these on is a config change, not a deploy.
    CCS_ENABLED: bool = False  # Sales Call Capture, master switch
    COCKPIT_ENABLED: bool = False  # Sales Cockpit, master switch
    # The Fathom fallback: a Fathom recording with an external
    # attendee becomes a captured call — session, transcript entries, wrap-up,
    # SOW clock, proposal — from the Fathom sync that already runs for RAG.
    # ON by default and deliberately NOT chained to CCS_ENABLED: the sync runs
    # on cubie-worker, which does not inherit the Service's env, and a job that
    # silently skipped because a flag was unset THERE is the success-shaped
    # failure this repo keeps paying for. The router's own 404 still governs
    # whether anyone can open what this writes.
    CCS_FATHOM_ENABLED: bool = True
    # A Fathom recording older than this is not attached at all (the account
    # listing reaches back indefinitely; a backfill must not mint sessions and
    # spend PRO extractions on last quarter). Independently, a wrap-up clock is
    # only opened for a recording under 48h old — see fathom_attach.
    CCS_FATHOM_MAX_AGE_DAYS: int = 14
    # The Meet space-write scopes (meetings.space.created / .settings) are
    # granted on the DWD client ID by the workspace admin. This flag is the
    # kill switch for every Meet space
    # WRITE — creating a space, and arming transcription on a Calendar-created
    # one. Off means those refuse before requesting a credential; reads
    # (conference records, transcripts, attendance) are unaffected either way,
    # which is what lets the calendar screen still explain a row it cannot arm.
    CCS_MEET_ENABLED: bool = False
    # Lead writes. Need `crm.objects.leads.read/write` granted on both Cubie
    # private apps; the Lead→Contact PRIMARY association is HubSpot-defined type
    # 578. A kill switch: set CCS_LEADS_ENABLED=false in the environment to stop
    # every lead write.
    CCS_LEADS_ENABLED: bool = True
    # Cubie in Google Meet — the call checklist in Meet's side panel
    # (backend/routers/meet_addon.py, frontend/src/meetAddon/). Off means
    # /api/meet-addon 404s and the panel says "not switched on yet". A pure
    # read, deliberately NOT chained to CCS_ENABLED: the checklist is useful on
    # a call nobody captured, and the add-on's own audience lives in Admin →
    # Feature Access (`sales_meet_addon`). The add-on also has to be deployed
    # and installed on the Google side (docs/meet_addon/README.md) before
    # anyone sees it in Meet at all.
    MEET_ADDON_ENABLED: bool = False
    # Cubie for Chrome (docs/chrome_extension/README.md): the browser extension
    # that puts a person's overdue promises and this morning's priorities on
    # Chrome's new tab page. Off means every /api/browser-extension route 404s,
    # so a connected extension falls back to "Cubie for Chrome is not switched
    # on". WHO may connect lives in Admin → Feature Access (`chrome_extension`).
    # A pure read of what Cubie already made, plus "done" on a promise or card:
    # no model is ever called from these routes.
    CHROME_EXTENSION_ENABLED: bool = False
    # How long one connected browser's key works before the person has to press
    # Connect again. Bounded rather than sliding, so a lost laptop's key dies on
    # a known date even if nobody remembers to disconnect it.
    CHROME_EXTENSION_TOKEN_TTL_DAYS: int = 90
    # Outbound digest delivery (Slack DM / email). Separate from
    # COCKPIT_ENABLED on purpose: the cockpit can be read on-screen for a week
    # before anything starts arriving in anyone's Slack.
    COCKPIT_DIGEST_ENABLED: bool = False

    # The Phase-2 ingest switch — and the only flag here that is off for a
    # DESIGN reason rather than a pending grant. v1 detects calls by POLLING
    # `conferenceRecords.list` on Cloud Scheduler, matching
    # `rubik_core/scripts/transcript_sync.py`, which already sweeps Meet
    # transcripts every 15 minutes; CCS extends that slot instead of adding a
    # parallel pipeline. Push was rejected because there are ZERO Pub/Sub push
    # receivers in this repo (`google-cloud-pubsub` is pinned and never
    # imported), so it would mean writing a service-account OIDC verifier from
    # scratch — new infrastructure with no precedent to copy — while every
    # scheduled endpoint in Cubie authenticates on `X-Scheduler-Key` instead.
    # The Workspace Events subscription path ships written and tested but
    # UNWIRED, and this flag is what wires it. Polling costs latency only:
    # seconds becomes ≤15 minutes, against a wrap-up that is a next-morning
    # workflow.
    CCS_EVENT_PUSH_ENABLED: bool = False

    # How far back each conference-record sweep looks. This must exceed the
    # scheduler interval by a wide margin — 24h against a 15-minute cadence is
    # deliberate, enormous overlap — because Google deletes conference records,
    # transcripts and transcript entries 30 days after the conference ends
    # (`ConferenceRecord.expireTime` is server-enforced, and there is no
    # historical import past it). A window missed during a scheduler outage is
    # therefore a PERMANENTLY lost call, not a delayed one. Re-reading the same
    # conference costs nothing but a read quota unit, since ingest is
    # idempotent on the UNIQUE (call_session_id, source, external_id): widen
    # this freely, never narrow it to save API calls.
    CCS_POLL_LOOKBACK_HOURS: int = 24

    # Artifacts a captured space is created with. Recording defaults OFF:
    # transcription is what the extractor reads, while an audio/video artifact
    # is a much larger consent and retention surface for no extra signal (the
    # wrap-up screen has no audio player, by design — there is nothing to seek
    # into). Smart notes are on because they cost nothing extra and give the AE
    # something human to read next to the proposals.
    MEET_AUTO_RECORD_DEFAULT: bool = False
    MEET_AUTO_SMART_NOTES: bool = True

    # DEFERRED PUSH PATH — none of the three settings below is read in v1.
    # They belong to the Pub/Sub ingest behind CCS_EVENT_PUSH_ENABLED above,
    # and THE TOPIC THEY NAME HAS NEVER BEEN CREATED: a blank value is the
    # correct state, not a misconfiguration, and nobody should go looking in
    # GCP for a subscription that v1 does not make. They stay here, spelled
    # out, because `rubik_core/integrations/workspace_events.py` ships written
    # and tested; deleting them would turn Phase 2 back into a rediscovery.
    #
    # TOPIC is where Google would publish Meet conference/transcript events
    # (Workspace Events subscriptions carry no resource data, so every handler
    # would still make a follow-up Meet read). AUDIENCE is the audience the
    # push's OIDC token must carry — the receiver verifies it and refuses the
    # POST otherwise, because Pub/Sub retries hard on any non-2xx and an
    # unauthenticated receiver is a retry loop anyone can start.
    MEET_PUBSUB_TOPIC: str = ""
    MEET_PUBSUB_AUDIENCE: str = ""  # expected OIDC audience on the push receiver
    # How far ahead the renewal job would renew a Workspace Events
    # subscription. Also unused in v1, which creates no subscriptions at all.
    # Generous deliberately: on expiry Google PERMANENTLY DELETES the
    # subscription — it cannot be renewed or reactivated, only recreated — so a
    # missed renewal is an incident, not a retry, and 72h absorbs a scheduler
    # outage or a long weekend. Max TTL is 7 days while includeResourceData is
    # false, so this must stay well under that. Not having to run this clock
    # correctly is one of the reasons v1 polls instead.
    MEET_SUBSCRIPTION_RENEW_WITHIN_HOURS: int = 72

    # Extraction (rubik_core/call_capture/extractor/). Empty = the PRO tier;
    # pin a full model id, never a `-latest` alias — the alias hides which
    # family answered, and a 3.x model silently ignores a thinking budget meant
    # for 2.x. PRO rather than FLASH because a missed vendor field or a
    # miscited quote costs more than the tokens do at this call volume.
    CCS_EXTRACTION_MODEL: str = ""  # "" → falls back to VERTEX_LLM_MODEL_PRO
    # Stamped on every call_extraction row next to the schema version, so an
    # item can always be attributed to the prompt that produced it. Bump on any
    # behavioural prompt change; that is the whole point of persisting it.
    # v2 added the call-script block and script_coverage. Kept equal
    # to prompts.PROMPT_VERSION by a test — an override is an operator's tool,
    # a drifted DEFAULT is just a wrong label on every row.
    CCS_PROMPT_VERSION: str = "ccs-extract-v2"
    # Items the model is less sure about than this are not shown in the wrap-up.
    # Low on purpose, and NOT the same trade-off as CRM_SYNC_CONFIDENCE_MIN: a
    # wrap-up is one screen about one call that its own AE reviews row by row,
    # so a hedged item they can untick beats one silently dropped. Raise it if
    # the screen stops fitting inside its 90-second budget.
    CCS_MIN_CONFIDENCE_SHOW: float = 0.5
    # How long captured transcript entries are kept. 0 = keep indefinitely, and
    # that default is EXPLICIT rather than assumed: retention is the company's
    # own policy decision, ideally aligned with its existing retention policy
    # for Meet artifacts rather than a second rule. Note Google itself deletes
    # Meet transcripts at 30 days, so whatever number lands here governs the
    # only remaining copy.
    CCS_TRANSCRIPT_RETENTION_DAYS: int = 0

    # ── Transcript coverage: "did we hear the whole call?" ───────────────────
    # `artifactConfig.autoTranscriptionGeneration` sets a DEFAULT, not a lock —
    # Google's own words are that hosts and co-hosts can still override it
    # during the meeting — and the entire Meet API has three write methods
    # (spaces.create, spaces.patch, spaces.endActiveConference), none of which
    # can stop or start transcription on a running conference. So an AE can
    # stop transcription mid-call and Cubie can neither prevent nor notice it at
    # the time: the truncated transcript reports ENDED, the sweep correctly
    # ingests it, and the wrap-up looks entirely normal — built from part of the
    # call. That is the one failure this feature spends all its code avoiding, a
    # partial result that looks complete, and it is worst exactly where people
    # reach for the stop button: mid-call, when the conversation turns to
    # pricing, a competitor or a complaint — which is where the SOW ask lives.
    # After the fact, though, the two clocks disagree: the transcript's last
    # utterance offset against the conference's own start/end. Coverage is that
    # comparison, computed at ingest and stored on the session.
    #
    # TWO CONDITIONS, BOTH REQUIRED, never either. A ratio alone flags a
    # four-minute call that ends forty seconds early — a rounding error rendered
    # as an accusation — and a gap alone flags the tail of every long call that
    # ends in silence. A detector that cries wolf teaches the AE to ignore the
    # banner, which costs more than never having shipped it (same reasoning as
    # COCKPIT_STAGE_LAG_MIN_CONFIDENCE below). Raise the ratio only alongside
    # evidence from real calls; the floor is what keeps short calls quiet.
    CCS_COVERAGE_MIN_RATIO: float = 0.85
    # The absolute floor, in seconds of call that carries no transcript. Three
    # minutes is well past the couple of seconds Meet takes to close a
    # transcription session out, and it is long enough that whatever happened in
    # the gap could actually have been the ask.
    CCS_COVERAGE_MIN_GAP_SECONDS: int = 180

    # The commitment clock runs in BUSINESS hours, not clock hours: the SLA
    # table is written in business days, and "3 business days" is stored as
    # 24. One calculator built on these two values does all the elapsed-time
    # arithmetic — breach_at, the 0.7 at-risk threshold, the +48h and +5d
    # escalation steps. A Friday-afternoon request that breaches on Monday
    # morning because someone reached for a raw timedelta is exactly the false
    # positive that ends the cockpit's credibility in week one.
    COCKPIT_BUSINESS_DAY_HOURS: int = 8
    # The company's business time zone is Workspace.time_zone in control data.
    # Stage-lag detector floor: below this, Cubie's evidence disagreeing with a
    # deal's HubSpot stage is not raised as an exception. Start at 0.7 and tune
    # from the observed "Not right" rate — a detector that cries wolf is worse
    # than no detector, because it teaches the AE to dismiss without reading.
    COCKPIT_STAGE_LAG_MIN_CONFIDENCE: float = 0.7
    # Hard cap on rows in one digest and in a capped My Day block, enforced
    # server-side (rubik_core/cockpit/digest.py) so no later feature can route
    # around it. Overflow renders as a count that links to a full list; it must
    # never expand inline, or the cap is defeated by a chevron.
    COCKPIT_MAX_INAPP_ITEMS: int = 3
    # BigQuery HubSpot (legacy fallback — used only when the HubSpot token is unset)
    BIGQUERY_PROJECT_ID: str | None = None
    BIGQUERY_DATASET: str = "hubspot_sales_marts"

    # BigQuery Data Agent (preview) — UC-7 feature flag.
    # When True, search_hubspot_data routes through the Conversational Analytics
    # API instead of HubSpotRetriever. Falls back automatically on any error.
    USE_BQ_DATA_AGENT: bool = False
    # Full resource name of the deployment's data agent; empty = not configured.
    BQ_DATA_AGENT_RESOURCE: str = ""
    BQ_DATA_AGENT_LOCATION: str = "us-central1"
    BQ_DATA_AGENT_TIMEOUT: float = 30.0

    # Workspace Sync & Credentials
    GOOGLE_CLIENT_ID: str | None = None
    # Optional per-user Gmail OAuth web client. These are separate from the
    # ID-token-only sign-in client and never appear in a browser bundle.
    GOOGLE_GMAIL_OAUTH_CLIENT_ID: str | None = None
    GOOGLE_GMAIL_OAUTH_CLIENT_SECRET: str | None = None
    GOOGLE_GMAIL_OAUTH_REDIRECT_URI: str | None = None
    DISABLE_WORKSPACE_SYNC: bool = False

    # App session length (hours) before a forced re-login. Default; admins can
    # override at runtime via workspace setting `session_lifetime_hours`
    # (Admin → Google Workspace tab). See rubik_core/auth/session_tokens.py.
    SESSION_LIFETIME_HOURS: float = 5.0

    # EOS — treat an L10's calendar roster as team membership for READ and
    # FILING scope (see backend/routers/eos/_authz.roster_dept_names). Someone
    # on two L10s has only ONE ``users.department``, so without this everything
    # they file outside a meeting lands on their home team and the standalone
    # Issues / To-Dos / Rocks pages disagree with the meeting they're sitting
    # in. Kill switch, not a rollout gate: it grants no mutation rights and no
    # Exec access, so the intended state is ON. Set false only to revert the
    # widened visibility without a redeploy.
    EOS_L10_ROSTER_SCOPE_ENABLED: bool = True

    # EOS — when an L10 is imported or synced from Google Calendar, replace a
    # Google Group on the invite (a distribution list such as the Exec team's)
    # with the people in it. The Calendar API returns a group as ONE attendee
    # carrying the group's address and never expands it, so anyone invited only
    # through the group was absent from ``roster_emails`` and therefore from the
    # in-meeting Check-in picker. Uses the
    # Directory API under the same DWD the Workspace role sync uses and
    # degrades to the unexpanded roster on any failure. Kill switch: False
    # restores the verbatim attendee list without a redeploy.
    EOS_L10_EXPAND_GROUP_INVITEES: bool = True

    # SQL Database
    DATABASE_URL: str = "sqlite:///./dam_data/dam.db"
    # Independent platform routing data; never inherits a workspace URL.
    CONTROL_DATABASE_URL: str = "sqlite:///./dam_data/control.db"
    WORKSPACE_DATABASE_DIR: str = "./dam_data/workspaces"
    WORKSPACE_ENGINE_CACHE_SIZE: int = 8

    # Cloud SQL
    CLOUDSQL_CONNECTION_NAME: str | None = None  # e.g., project:region:instance
    DB_USER: str | None = None
    DB_PASS: str | None = None
    DB_NAME: str | None = None

    # Scheduler Security
    SCHEDULER_API_KEY: str | None = None

    # ── Scheduled tasks (user-authored recurring Cubie runs) ──────────────
    #
    # A user schedules work from Chat ("every weekday at 8am, my important
    # emails") and a cubie-worker execution replays that prompt as them. OFF
    # means the chat tools are not registered, the tick no-ops and nothing
    # fires — the rows stay, so switching it back on resumes them.
    # WHO may schedule is separate and lives in Admin → Feature Access
    # (rubik_core/features registry key "scheduled_tasks").
    SCHEDULED_TASKS_ENABLED: bool = False

    # ── Meeting Skills (Templates, Skills and Routines in Ask Cubie) ──────
    #
    # A person asks Cubie for a meeting in any shape; Cubie writes the outline
    # (a Template), saves whole jobs as Skills run with "/name", and runs a
    # Skill on its own when a new transcript is ready (a Routine). Every point
    # cites a transcript line and is checked before it is shown
    # (rubik_core/meeting_skills/). OFF means the chat tools are not offered,
    # the "/" menu route answers 404 and no routine fires. WHO may use it lives
    # in Admin → Feature Access (registry key "meeting_skills"). Routines run on
    # cubie-worker, so the Job needs the flag too (deploy_worker_job.sh).
    MEETING_SKILLS_ENABLED: bool = False
    # Model for the one extraction call per run; blank = the FLASH tier.
    MEETING_SKILLS_MODEL: str = ""
    # Transcript text read per run, shared across its meetings. ~60k tokens:
    # a 90-minute Meet transcript is ~80-100k characters.
    MEETING_SKILLS_MAX_TRANSCRIPT_CHARS: int = 240_000
    # Meetings one run may cover (a weekly digest).
    MEETING_SKILLS_MAX_MEETINGS: int = 10
    # The model's reading time inside a LIVE chat turn. Chat has ~90s end to
    # end and the answer still has to be relayed after this, so a run that
    # would overrun says so instead of timing the whole turn out. Routines and
    # scheduled runs are not capped here (they have their own run timeout).
    MEETING_SKILLS_CHAT_EXTRACT_TIMEOUT_SECONDS: int = 55
    MEETING_SKILLS_MAX_TEMPLATES_PER_USER: int = 25
    MEETING_SKILLS_MAX_SKILLS_PER_USER: int = 25
    # Each routine is a model call per matching meeting: the cost guardrail.
    MEETING_SKILLS_MAX_ROUTINES_PER_USER: int = 10
    MEETING_SKILLS_MAX_INSTRUCTION_CHARS: int = 4_000
    # Consecutive failed fires before a routine pauses itself.
    MEETING_SKILLS_ROUTINE_MAX_FAILURES: int = 5

    # ── My To-Dos (the promises ledger) ───────────────────────────────────
    #
    # OFF means the /api/todos router answers 404, the Chat tools are not
    # registered, the Slack /todo modal offers only the EOS "Team L10" target
    # and the `promises` priorities channel emits nothing — the rows stay, so
    # switching it back on resumes them. Set it on BOTH the Service and
    # cubie-worker. WHO may reach the surface lives in Admin → Feature Access
    # (rubik_core/features registry key "todos").
    TODOS_ENABLED: bool = False

    # ── Promise accountability ("you said you would, and it hasn't happened")
    #
    # The nightly pass that hears first-person promises in meeting transcripts,
    # L10 notes, sent email and Slack, writes them to the promises ledger as
    # ``suggested`` rows, checks each one past its date for evidence it was
    # kept, and feeds the unkept ones to AI Priorities as ``promise_unkept``.
    # OFF means none of that runs and no such card is ever offered; the ledger
    # rows already written stay. Runs inside the priorities batch, so set it on
    # the cubie-priorities-batch Job, cubie-worker AND the Service (lazy regeneration
    # goes through the same orchestrator). WHO it runs for lives in Admin →
    # Feature Access (registry key "promise_accountability").
    PROMISE_ACCOUNTABILITY_ENABLED: bool = False

    # ── What's On Your Plate (the commitment-aware morning briefing) ──────
    #
    # OFF means the morning briefing behaves exactly as it does today: the
    # reply-owed and unanswered-request signals are not emitted, and an
    # overdue commitment is still down-ranked for lacking a future date. Set
    # it on the cubie-worker Job (where the briefing is written overnight) AND
    # the Service (the app lazily regenerates a missing card through the same
    # orchestrator, so gating one alone makes the two surfaces disagree).
    # WHO may reach it once it runs lives in Admin → Feature Access
    # (rubik_core/features registry key "open_commitments").
    OPEN_COMMITMENTS_ENABLED: bool = False

    # The weekly close-date roll-up (backend/jobs/close_date_rollup.py). OFF
    # means the scheduler can call the route and nothing is sent. A --dry-run
    # / dry_run=true call ignores this on purpose: rendering without sending
    # is how the switch gets reviewed before it is flipped. WHO receives it
    # lives in Admin → Feature Access (registry key "close_date_rollup").
    CLOSE_DATE_ROLLUP_ENABLED: bool = False

    # The sales daily rhythm (rubik_core/sales/rhythm_checks.py): the
    # Morning / Mid-day / End-of-day section on My Day and the matching
    # starter prompts in Ask Cubie. ON by default because everything it does
    # is a read — HubSpot searches, no writes, no model call of its own. OFF
    # means /api/sales/rhythm answers 404 and the chips disappear; the chat can
    # still answer the same questions when asked in words. WHO sees it lives in
    # Admin → Feature Access (registry keys "sales_rhythm" and
    # "sales_rhythm_team"). The My Day half also needs COCKPIT_ENABLED.
    SALES_RHYTHM_ENABLED: bool = True

    # ── Loose Ends (the AE loop-closure scan, docs/loose_ends/README.md) ──
    #
    # One central scan every weekday morning reads each rostered AE's own
    # Gmail (metadata only) and Slack, reconciles it against HubSpot, and keeps
    # the durable list of loose ends on Sales → Loose Ends, My Day, the daily
    # channel digest and the "Loose ends over 7 days" scorecard row. OFF means
    # /api/sales/loose-ends answers 404, the scheduler route answers
    # {"status": "disabled"} and no scan, digest or scorecard write happens;
    # the rows already written stay, so switching it back on resumes them.
    # Set it on BOTH the Service (the page, "Log it in HubSpot", "Draft a
    # reply") and cubie-worker (the scan runs there; the scheduler route hands
    # the Job the Service's own value) — deploy.sh and deploy_worker_job.sh
    # pin it. WHO is scanned lives in Admin → Feature Access (registry key
    # "loop_closure"; the team view is "loop_closure_team"), and adding a
    # person there starts the morning scan of their mailbox.
    LOOP_CLOSURE_ENABLED: bool = False
    # The daily Slack post's switch and channel are NOT settings here: a Cubie
    # admin turns the post on/off and picks the channel in Admin → Loose Ends
    # (rubik_core/loop_closure/digest_settings.py, workspace_settings key
    # "loop_closure_digest"). It ships OFF, pointed at #sales.
    # How far back the Gmail lanes look for new loose ends. Items already on
    # the list are re-checked however old they are, up to MAX_AGE_DAYS.
    LOOP_CLOSURE_LOOKBACK_DAYS: int = 14
    # A client email is "waiting on your reply" once this many BUSINESS days
    # (cockpit clock: Mon-Fri, 09:00-17:00 in the workspace's time zone) pass
    # with no answer.
    # Week-one tuning: if the noise rate on the Team tab is over 20% because of
    # newsletters, internal CCs or auto-replies, raise this to 3 first.
    LOOP_CLOSURE_OPEN_LOOP_BUSINESS_DAYS: int = 2
    # How long an AE has to log a client email or call in HubSpot themselves
    # before it is flagged "Not in HubSpot", in business days.
    LOOP_CLOSURE_LOG_GRACE_BUSINESS_DAYS: int = 1
    # A dated promise, or a HubSpot task, becomes a loose end this many
    # calendar days after its date; an undated promise this many days after it
    # was said. 5, not 3: a list of things only a few days overdue is noise.
    LOOP_CLOSURE_TODO_GRACE_DAYS: int = 5
    LOOP_CLOSURE_TODO_UNDATED_DAYS: int = 5
    # Only HubSpot tasks that went overdue within this many days become loose
    # ends. Older overdue tasks are a backlog, not a slip: a first dry run
    # can find two dozen for one AE, and a day-one wall of old tasks is how a
    # page gets ignored. A task already on the list keeps being re-checked.
    LOOP_CLOSURE_TASK_LOOKBACK_DAYS: int = 30
    # The age at which an item's chip turns red and it counts toward the
    # weekly "Loose ends over 7 days" scorecard number.
    LOOP_CLOSURE_ESCALATION_DAYS: int = 7
    # An item still open after this many days closes as "aged_out" — reported
    # on its own, never counted as fixed. Also caps how far back the HubSpot
    # activity index reads.
    LOOP_CLOSURE_MAX_AGE_DAYS: int = 60
    # The per-AE Sales scorecard measurable is named in Workspace operations.

    # Person impersonation is a global kill switch. Actor and target grants
    # are platform-managed data in the workspace operations policy.
    USER_IMPERSONATION_ENABLED: bool = True

    # ── Proposal Generator ────────────────────────────────────────────────
    #
    # OFF means the /api/proposals router answers 404 and the wrap-up never
    # offers "Draft proposal". Existing proposal rows stay; switching it back
    # on resumes them. Service only — nothing runs on the worker. WHO may reach
    # it lives in Admin → Feature Access (registry key "proposal_generator").
    PROPOSALS_ENABLED: bool = False
    # Empty = the PRO tier. A proposal is read by a prospect's decision-maker,
    # so a wrong sentence costs more than the tokens do at this volume. Pin a
    # full model id, never a `-latest` alias (see CCS_EXTRACTION_MODEL).
    PROPOSALS_MODEL: str = ""  # "" → falls back to VERTEX_LLM_MODEL_PRO

    # ── Voice input (dictation into Ask Cubie) ────────────────────────────
    #
    # The microphone in the chat box (rubik_core/voice). The browser records,
    # /api/voice/transcribe sends the clip to Gemini on Vertex once, and the
    # words come back to the chat box, which sends them as the person's
    # message (stopping the mic is the send). The audio is never stored or
    # logged. OFF means the router answers
    # 404 and the mic button is not drawn. On by default because nothing runs
    # unless someone presses the mic, and a minute of speech is ~1,900 audio
    # tokens. WHO sees the mic lives in Admin → Feature Access (registry key
    # "voice_input"). Service only.
    VOICE_INPUT_ENABLED: bool = True
    # Empty = the FLASH tier. Probed live on 2026-10-05: ~2-4s per short clip
    # on both 3.8 Flash and 3.5 Flash-Lite, and Flash-Lite lowercased proper
    # nouns ("hubspot", "acme corp") that Flash got right. Pin a full id.
    VOICE_TRANSCRIBE_MODEL: str = ""  # "" → falls back to VERTEX_LLM_MODEL_FLASH
    # Longest clip the server accepts. The browser stops recording at the same
    # mark, so this only bites a client that ignores it.
    VOICE_MAX_SECONDS: int = 120
    # The browser sends mono 16-bit WAV (so the server can measure the samples
    # without a decoder) at 16 kHz at most: 32 KB a second, 3.84 MB for 120s,
    # less from a slower device such as an 8 kHz headset. The cap
    # leaves header room and stays far under Vertex's 20 MB inline ceiling.
    VOICE_MAX_UPLOAD_BYTES: int = 4_200_000

    # ── Files in Ask Cubie (the paperclip in the chat box) ──
    #
    # A person attaches a document, spreadsheet, deck or picture; Cubie reads it
    # once (rubik_core/chat_files/) and files the reading in their PRIVATE
    # library. OFF means the upload route answers 404, the paperclip is not
    # drawn and the agent's search_my_files tool says it is switched off; files
    # already attached stay listed in Assets → My private files so their owner
    # can still download or delete them. WHO may attach lives in Admin →
    # Feature Access (registry key "chat_file_upload"). Service only.
    CHAT_FILE_UPLOAD_ENABLED: bool = True
    # OFF (or outside GCP, or the corpus unreachable) = files are read and used
    # in the chats they are attached to, but not filed in the private library,
    # so a later conversation cannot find them ("This chat only").
    CHAT_FILE_PRIVATE_INDEX_ENABLED: bool = True
    # Vertex display name of the private corpus. NOT a rag/config.py registry
    # slug: nothing but rubik_core/chat_files/private_index.py may reach it.
    CHAT_FILE_PRIVATE_CORPUS: str = "private_uploads_corpus"
    # Under Cloud Run's 32 MB request limit, and under Vertex's ~20 MB inline
    # ceiling for the scanned PDF the reader model is handed whole.
    CHAT_FILE_MAX_BYTES: int = 20_000_000
    CHAT_FILE_MAX_PER_MESSAGE: int = 5
    # Empty = the FLASH tier. It reads pictures and scans, so not Flash-Lite.
    CHAT_FILE_READER_MODEL: str = ""
    # What Cubie keeps of a file's text (the private library gets all of it).
    CHAT_FILE_STORED_TEXT_CHARS: int = 400_000
    # What one message puts in front of the chat model. The ADK session replays
    # it on every later turn of that conversation, so it is bounded; the rest
    # stays reachable through search_my_files.
    CHAT_FILE_TURN_CHARS: int = 40_000
    CHAT_FILE_TURN_TOTAL_CHARS: int = 80_000
    # New files one person may attach in 24 hours (attaching one again is free):
    # each is a reader call, a stored object and a ragFile.
    CHAT_FILE_DAILY_LIMIT: int = 100
    # How long filing one reading in the private library may take before the
    # file is kept as chat-only instead (attaching it again retries).
    CHAT_FILE_INDEX_TIMEOUT_SECONDS: float = 90.0

    # ── Voice commands (the Voice button in the sidebar, and its shortcut) ──
    #
    # A spoken command opens a page, hands a question to Ask Cubie, or proposes
    # an EOS change (add a Rock, mark a To-Do done …) that the person confirms
    # before the browser sends it (rubik_core/voice/commands.py). OFF means the
    # router answers 404 and the button and shortcut are not drawn. WHO has it
    # lives in Admin → Feature Access (registry key "voice_commands").
    VOICE_COMMANDS_ENABLED: bool = True
    # Empty = the FLASH tier, the same model dictation uses. Pin a full id.
    VOICE_COMMAND_MODEL: str = ""
    # A command is a sentence or two. The browser stops listening at this mark
    # and drops the recording rather than act on whatever the room said.
    VOICE_COMMAND_MAX_SECONDS: int = 30
    # How long the server waits for the model to read one command before it
    # says so in words. Below the browser's own 45 s limit, with room for the
    # lookups after it (the vendor knowledge base, HubSpot, Calendar), so the
    # person reads a reason rather than a dead spinner.
    VOICE_COMMAND_TIMEOUT_SECONDS: int = 25

    # ── Insights (BI dashboards, docs/insights/README.md) ───────────────────
    # On by default: nothing runs on a schedule and nothing is charged until
    # someone opens a dashboard (reads only) or asks for an AI summary (one
    # Flash call, cached). OFF means every /api/insights route answers 404 and
    # /api/users/me reports both Insights keys false, so the nav hides the area.
    # WHO may open it lives in Admin → Feature Access (registry keys
    # "insights" and "insights_people"), never here.
    INSIGHTS_ENABLED: bool = True
    # Insights exclusions are saved in each workspace's operations policy.

    # ── What's New (the Cubie changelog's release notes) ─────────────────────
    #
    # OFF means /api/whats-new answers 404 ("What's New is not switched on."),
    # /api/users/me folds `whats_new` off so the sidebar button, the drawer and
    # the Daily Briefing bar stay hidden, and the morning post answers
    # "disabled". The morning post's channel and the weekly tease are NOT
    # settings here: an admin sets them in Admin → What's New
    # (workspace_settings key "whats_new"). WHO sees the drawer lives in
    # Admin → Feature Access (registry key "whats_new").
    WHATS_NEW_ENABLED: bool = False

    # Per-user ceiling. Each task is a full chat run per occurrence, so this is
    # the cost guardrail as much as a tidiness one.
    SCHEDULED_TASKS_MAX_PER_USER: int = 10
    # Wall-clock budget for one run. Above chat's 90s: nobody is waiting, and a
    # scheduled prompt ("summarize yesterday's meetings") legitimately fans out
    # over more tools than a live question.
    SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS: int = 300
    # ADK call budget for one headless run (chat uses CHAT_MAX_LLM_CALLS=15).
    SCHEDULED_TASKS_MAX_LLM_CALLS: int = 20
    # Consecutive failures before a task auto-pauses and the owner is told.
    SCHEDULED_TASKS_MAX_CONSECUTIVE_FAILURES: int = 5
    # Tasks dispatched per tick. Each dispatch is a blocking Cloud Run Jobs
    # API call, so this keeps one tick inside Cloud Scheduler's attempt
    # deadline; anything left over is still due on the next tick.
    SCHEDULED_TASKS_TICK_MAX_DISPATCH: int = 30
    # Email delivery has its OWN autosend switch. Never reuse Deep Prep's:
    # enabling one email path must not silently enable another. Delivery
    # recipients must satisfy the report ACL and the workspace address fence.
    SCHEDULED_TASKS_EMAIL_AUTOSEND: bool = False

    # Ingestion Council audience is stored in the workspace operations policy.

    # AI Priorities — temporal anchoring (forward-looking "dreamer").
    # The workspace's control-row time zone anchors "today" for anyone who
    # has not saved their own, and the HomeCard date key.
    # Priorities quality gate (docs/priorities_quality_gate_design.md):
    # "off" (default) → the priorities-gate scope no-ops entirely;
    # "observe" → judge + persist + DM ops, delivery UNAFFECTED (phase 2);
    # "enforce" (phase 3, not implemented) will add repair + gated delivery.
    CUBIE_PRIORITIES_GATE_MODE: str = "off"
    # How far ahead the dreamer predicts ("what must I prepare for / act on").
    PRIORITIES_LOOKAHEAD_HOURS: int = 72
    # Offboarded-user gate — when True (default), a deactivated employee (their
    # Slack account is `deleted`, or their Google Workspace account is
    # `suspended`) is EXCLUDED from the priorities roster and Slack delivery
    # entirely, so an ex-employee stops generating empty daily bundles and
    # tripping the false "no bundle for their local today" ops alert. Distinct
    # from PTO (which still gets a "holding your priorities" note) — a
    # deactivated user is simply dropped. Master kill switch for the whole
    # detection path (Slack primary + Google Directory complement); flip to
    # False to fall back to the pre-gate behaviour (generate/deliver for
    # everyone eligible). The Google Directory `suspended` check additionally
    # self-disables if the `admin.directory.user.readonly` DWD scope isn't
    # granted, so leaving this True is safe even before that grant.
    CUBIE_OFFBOARD_DETECTION_ENABLED: bool = True
    # Overnight recommendations (pipeline #2) older than this are considered
    # stale and are not surfaced — stops a high-confidence rec from showing
    # forever until manually dismissed.
    RECOMMENDATION_MAX_AGE_HOURS: int = 36

    # Where the SPA is hosted. Used by OAuth callbacks (e.g. UC-15 Slack) to
    # send the browser back to a frontend page after the third-party redirect
    # lands on the API. In dev this is the Vite dev server; in prod set it to
    # the public web origin.
    FRONTEND_URL: str = "http://localhost:5173"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
logger.info(
    f"Config initialized: APP_MODE={settings.APP_MODE}, GCP_PROJECT_ID={settings.GCP_PROJECT_ID}"
)


# Company credentials are resolved by workspace secret refs at use time. They
# are never fetched into process-wide Settings during import.


class GcpProjectNotSet(RuntimeError):
    """An operation needs the Google Cloud project and none is configured."""


def gcp_project_id() -> str:
    """The configured ``GCP_PROJECT_ID``; raises :class:`GcpProjectNotSet`.

    For operations that address a project explicitly (launching a Cloud Run
    Job, printing the command that runs one). There is no fallback project.
    """
    project = (settings.GCP_PROJECT_ID or "").strip()
    if not project:
        raise GcpProjectNotSet("GCP_PROJECT_ID not set")
    return project


# Ensure local directories exist
Path(settings.LOCAL_DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(settings.DAM_DATA_DIR).mkdir(parents=True, exist_ok=True)
if settings.APP_MODE == AppMode.LOCAL:
    Path(settings.CHROMA_PERSIST_DIR).mkdir(parents=True, exist_ok=True)

# ── ADC hygiene ──────────────────────────────────────────────────────────────
# Workspace DWD uses keyless IAM signing in the selected workspace. Never
# export company credentials as GOOGLE_APPLICATION_CREDENTIALS: that changes
# the principal for unrelated Cloud SQL, Vertex, Secret Manager and BigQuery
# calls throughout the process.

# ─── ADK / GenAI SDK Environment Mapping & Monkeypatch ─────────────────────
# The google-genai SDK (used by ADK) expects explicit parameters for Vertex AI.
# To ensure robust operation across all internal ADK calls, we globally patch
# the Client class to inject our settings if they are missing.
if settings.APP_MODE == AppMode.GCP or settings.GCP_PROJECT_ID:
    import os

    os.environ["GOOGLE_GENAI_VERTEXAI"] = "True"
    if settings.GCP_PROJECT_ID:
        os.environ["GOOGLE_GENAI_PROJECT"] = settings.GCP_PROJECT_ID
    if settings.VERTEX_LLM_LOCATION:
        os.environ["GOOGLE_GENAI_LOCATION"] = settings.VERTEX_LLM_LOCATION

    try:
        import google.genai

        original_init = google.genai.Client.__init__

        def patched_init(self, *args, **kwargs):
            # If not using an API key and vertexai is not explicitly False,
            # we force Vertex AI with our project/location settings.
            # setdefault so callers can still override location for the rare
            # regional-only model (e.g. an embedding tier that hasn't shipped
            # on global yet).
            if not kwargs.get("api_key") and kwargs.get("vertexai") is not False:
                kwargs["vertexai"] = True
                if settings.GCP_PROJECT_ID:
                    kwargs.setdefault("project", settings.GCP_PROJECT_ID)
                if settings.VERTEX_LLM_LOCATION:
                    kwargs.setdefault("location", settings.VERTEX_LLM_LOCATION)
            original_init(self, *args, **kwargs)

        google.genai.Client.__init__ = patched_init
        logger.debug(
            "Successfully applied google.genai.Client monkeypatch for Vertex AI."
        )
    except Exception as patch_err:
        # Fallback to print if logger fails during early initialization
        print(f"Could not apply google.genai.Client monkeypatch: {patch_err}")
