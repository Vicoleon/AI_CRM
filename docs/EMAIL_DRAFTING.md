# UC-7 — Email Drafting Agent

Sales email drafting that grounds each draft in real CRM, transcript, and product context before composing. Builds on the existing `sales_email_agent` (system prompt unchanged) and the UC-4 Gmail draft path.

## Surfaces

| Surface | Entry point | Notes |
|---|---|---|
| HTTP API | `POST /api/email/draft` | Server-driven; pre-binds tools to the authenticated user. |
| Chat | "draft a follow-up to [Acme]", "write a prospecting email to [contact]", etc. | Coordinator routes to `sales_email_agent` (sales users only); the agent now has retrieval tools and will pull context on its own. |

The HTTP endpoint and the chat path both end up at the same agent — the only difference is who composes the user message. The endpoint composes a structured one; chat composes whatever the user typed.

## Endpoint

### `POST /api/email/draft`

```jsonc
// Request
{
  "recipient_email": "ceo@acme.example",
  "recipient_name": "Jane Doe",            // optional
  "recipient_title": "CFO",                // optional
  "company": "Acme Corp",                  // optional
  "email_type": "follow_up",               // cold_prospect | follow_up | re_engagement | market_trigger
  "trigger_context": "a competitor raised prices", // optional; required-ish for market_trigger
  "additional_context": "they asked for pricing last quarter" // optional
}
```

```jsonc
// Response (200)
{
  "recipient": "ceo@acme.example",
  "email_type": "follow_up",
  "subject": "...",                  // parsed from agent output, may be null
  "body": "...",                     // parsed from agent output, may be null
  "gmail_draft_url": "https://mail.google.com/mail/u/0/#drafts/...", // null if the agent didn't create a draft
  "raw_response": "<full agent text>"
}
```

The agent always saves the draft via `gmail_create_draft` (per the system instruction); the URL is parsed back out of the agent's final message. If parsing fails, `subject`/`body`/`gmail_draft_url` may be `null` — the caller should fall back to `raw_response`.

**No "send" path** — Gmail OAuth scopes are `gmail.readonly + gmail.compose` only ([gmail.py:17](../rubik_core/tools/gmail.py:17)). Sending requires `gmail.send` (DWD scope) which has not been granted. See `project_uc4_gmail_modify_scope.md` in memory.

### Email types

| Type | When to use |
|---|---|
| `cold_prospect` | First contact with a lead. Open with insight, identify gap, propose 15-min call. |
| `follow_up` | Existing customer or active deal. References last interaction; never "just following up." |
| `re_engagement` | Lead that's gone cold. Lead with fresh insight, not "checking in." |
| `market_trigger` | Triggered by a regulatory or market event. Pass the event in `trigger_context`. |

## Retrieval bundle

The sales agent now has these tools available (in addition to `gmail_create_draft`):

| Tool | What it pulls |
|---|---|
| `search_hubspot_data` | Recipient's company / deal history / pipeline status. Routes through BQ Data Agent or `HubSpotRetriever` per the flag (see below). |
| `search_past_meetings` | Prior Google Meet transcripts the user attended with the recipient/company. Access-bounded by `meeting_participants`. |
| `search_knowledge_base` | UC-1 multi-corpus RAG (department + companywide) for the company's product and service context. |
| `search_products` | Product catalog corpus — pricing, fulfillment, feature details. |

The system instruction is **unchanged**. Tool selection is discovered by the LLM from each tool's docstring. Treating the prompt as immutable preserves the carefully-tuned company voice and Gap Selling structure.

## BQ Data Agent feature flag (preview)

`search_hubspot_data` can route through Google's [Conversational Analytics API](https://cloud.google.com/gemini/docs/conversational-analytics-api/overview) — a managed BigQuery Data Agent — instead of the local `HubSpotRetriever`. This is a preview API ("not for production" per Google) and is gated behind a flag.

| Setting | Default | Purpose |
|---|---|---|
| `USE_BQ_DATA_AGENT` | `False` | Master switch. `True` routes HubSpot queries through the BQ Data Agent. |
| `BQ_DATA_AGENT_RESOURCE` | `projects/<project-id>/locations/us-central1/dataAgents/<agent-id>` | Full agent resource path. |
| `BQ_DATA_AGENT_LOCATION` | `us-central1` | Region in the agent path. |
| `BQ_DATA_AGENT_TIMEOUT` | `30.0` | Per-call timeout (seconds). |

**Failure handling.** Any error inside `query_hubspot_via_agent` (auth, HTTP, empty response, malformed JSON) raises `BQDataAgentError`, which `search_hubspot_data` catches and falls through to the legacy path. The flag is a **soft** switch — flipping it on never breaks production; if the agent fails, the call silently degrades to `HubSpotRetriever`.

**Why a flag.** The Conversational Analytics API is preview. We want production traffic on the legacy path until we've measured agent quality + latency in real use. After a release window in production, the plan is to keep the flag on if it's been stable.

**IAM.** The Cloud Run runtime service account needs `roles/geminidataanalytics.dataAgentUser` on the project that holds the data agent.

**Output shape.** The API returns natural-language text (and optional chart specs). We extract NL only and feed it as the tool result. We do not attempt to parse structured rows — the sales agent uses the NL summary as context.

## Things deliberately omitted from UC-7

- **Send button / `/api/email/send`** — Gmail scope (`gmail.send`) is not granted. Drafts only.
- **`confidence_score` field** — LLM self-graded confidence is unreliable. Skipped until we have a real signal.
- **`suggested_send_time`** — Not enough behavioral data to ground a recommendation.
- **`hubspot_query_log` table** (from UC-5 spec) — UC-5 was deferred; the existing audit log captures `DRAFT_SALES_EMAIL` actions, which is sufficient until UC-5 is revisited.

## Operational notes

- Audit log: every call to `/api/email/draft` writes a `DRAFT_SALES_EMAIL` row to `audit_log` with the recipient, email type, and trigger flag.
- The endpoint runs the agent in a transient `InMemorySessionService` session (one session per request). No cross-call memory.
- Latency: dominated by the agent's tool-calling loop. Expect 5–15s for a typical draft (one or two retrievals + one generate). The BQ Data Agent path may shave time off the HubSpot retrieval; that's the bet.
