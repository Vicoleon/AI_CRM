# Cubie go-live handoff

Phase 2 provides local code, tests and dry-run tooling. No Google Cloud project,
client account, hostname or store listing has been created by this work. Keep
the rollout local until the operator has reviewed a dry run and each human step
below has an owner. This document is a checklist, not evidence of deployment.

## 1. Choose the platform boundary

Record the Google Cloud project ID, region, public hostname, Cloud SQL instance,
storage bucket, Artifact Registry repository and runtime service account in a
private environment file. Keep client credentials and platform signing secrets
in Secret Manager, not in that file or Git. Estimate the Cloud SQL tier before
provisioning: the connection budget is
`(service max instances + Job max instances) × (control pool + workspace count ×
(sync pool + ADK pool) + peak concurrent source-mutation advisory connections)
+ reserved connections`. Advisory connections use their own pool boundary but
still occupy physical PostgreSQL slots during a managed corpus write. Set the four
`CUBIE_PROVISION_*` capacity values and leave headroom for administrative work.

## 2. Bootstrap resources, then initialize control

Run `scripts/platform/bootstrap_gcp.sh --env <private-env> --phase resources --dry-run`
and review the project, region, explicit SQL edition/tier/storage/availability,
bucket, runtime IAM and empty platform secret containers. Record the proposed
cost before approving the same command without `--dry-run`. Resource bootstrap
does not create a database user, initialize the control schema or populate
database credentials. Set `CUBIE_SQL_EDITION=ENTERPRISE` explicitly; PostgreSQL
16 otherwise defaults to Enterprise Plus in supported regions.

After the SQL instance exists, create a dedicated provisioning SQL user with
the required database/role administration privileges. Generate its password
privately and pass it in an SQL Admin API request body, never a command
argument or log. Preserve any existing user; do not reset its password as part
of a bootstrap retry. Configure a loopback Cloud SQL Auth Proxy for the exact
instance and verify its identity. Google CLI sign-in and Python Application
Default Credentials are separate: verify the RubikSoft operator's ADC before
the proxy or workspace provisioner uses it. Prefer a Cubie-specific credential
file rather than replacing another application's credentials.

For PostgreSQL 16, initialization establishes owner membership only when it
creates a new control role. Reusing an existing role requires the operator's
SET and inherited owner privileges to be present already; preflight refuses
missing membership before changing SQL or secrets.

SQL storage auto-increase is disabled so the reviewed size is the initial
cost boundary. Monitor disk usage and approve a later storage increase before
the instance fills; a bootstrap retry never resizes an existing instance.

Prepare paired operator proxy and runtime socket URLs for both control and
provisioning identities, with credentials kept private. Check the declared
capacity against live PostgreSQL `SHOW max_connections`; include aggregate
Job executions, advisory connections and reserved headroom. The service limit
is applied to Cloud Run; the Job budget does not prevent overlapping executions.

Then run `scripts/platform/bootstrap_gcp.sh --env <private-env> --phase control --fresh-install --dry-run`.
This phase requires the existing SQL target and working admin connection and
checks privileges, capacity, ownership and an empty control target before any
write. Review it before running the same command without `--dry-run` under
the approved bootstrap target and scope. Use
`--fresh-install` only for a genuinely fresh control database. The compatibility
`--phase all` path requires SQL/proxy prerequisites already prepared; use the
two phases for a new instance. A normal deploy must never create or repair a
missing/corrupt control schema.

## 3. Prepare identity and client setup

1. Set up the External Google OAuth consent screen for Cubie, complete the
   required brand verification, create the web client, and configure the exact
   hostname and redirect origins. Put the client ID in the private deploy env;
   it is not baked into a public source file.
2. Prepare one Cubie Slack app per client from the supplied manifest. Install
   and connect it after its workspace exists; each client owns its own bot and
   signing secret.

The domain-wide delegation grants are capability specific. The runtime uses
these exact scopes (see `rubik_core/tools/` and `rubik_core/auth/workspace.py`):

| Capability | Google OAuth scope |
| --- | --- |
| Gmail read and drafts | `https://www.googleapis.com/auth/gmail.readonly`, `https://www.googleapis.com/auth/gmail.compose` |
| Gmail send, if enabled | `https://www.googleapis.com/auth/gmail.send` |
| Calendar | `https://www.googleapis.com/auth/calendar` |
| Drive files | `https://www.googleapis.com/auth/drive` |
| Drive labels, if enabled | `https://www.googleapis.com/auth/drive.metadata.readonly`, `https://www.googleapis.com/auth/drive.labels.readonly` |
| Directory users and groups | `https://www.googleapis.com/auth/admin.directory.user.readonly`, `https://www.googleapis.com/auth/admin.directory.group.readonly` |
| Meet space reads | `https://www.googleapis.com/auth/meetings.space.readonly` |
| Meet space creation and settings, if enabled | `https://www.googleapis.com/auth/meetings.space.created`, `https://www.googleapis.com/auth/meetings.space.settings` |

## 4. Deploy, provision, and verify

Run `scripts/platform/deploy.sh --env <private-env> --dry-run` from a clean,
known Git revision. Review the migration Job, service and Job manifests,
schedules and full headers. Before a real deploy, the script must read every
active workspace freeze window and stop during the window or the 20-minute lead.
Only Jose may perform a manual break-glass action. The first real deploy can
run with zero workspaces: it builds once, migrates the control database, then
starts the service and Jobs. Confirm `/health` reports that revision before
creating workspace schedules.

Next, create the first RubikSoft workspace with
`scripts/platform/workspace.sh --env <private-env> workspace create` and the
explicit slug, `rubik-soft.com` domain, first admin, delegation subject and
time zone. Run `--dry-run` first. The CLI returns the per-workspace service
identity and delegated client ID. Have the Workspace admin grant that client
ID the scopes listed above for the capabilities being enabled, then test the
delegation subject. Each later client needs its own grant. The client now
installs its Slack app; an admin saves that workspace's client ID, client
secret, signing secret and installation in connector settings. People connect
their own Slack user token separately. If HubSpot or Fathom is enabled, save
its workspace connector settings and write-only secret in Admin. Test
callbacks and webhook routing before scheduled ingest is enabled.

Verify the workspace's schema head, sign-in, tenant-specific content, secrets,
scheduler runs and Job results. An Acme workspace is for local/tests only; do
not provision it in production.

## 5. Client-owned AI providers

An admin can save a write-only credential under **Admin → Workspace → AI
provider credentials**. Available connections are Claude (Anthropic), DeepSeek,
OpenAI, Gemini API, Meta Muse and a client-owned Vertex AI project. The Vertex
path takes that workspace's provisioned service identity plus the client's
project and region, not a JSON keyfile from the browser. The client grants
that identity Vertex access in its project. The server keeps keys in workspace-bound secrets and
sends model requests to fixed provider endpoints. The admin tests a connection
before assigning a compatible model to an enabled workload. Workloads include
Chat and support chat, Sales email drafting and analysis, AI Priorities, L10
recaps, Insights, CRM Sync, call wrap-ups, proposals, X-Ray analysis and web
research, Meeting Prep, Deep Prep, Gmail and Slack summaries, EOS Rock drafts,
Loose Ends, Smart Teams, DAM, and overnight work. The catalog shows only
models with a tested adapter for that workload's text, JSON, tool, streaming,
grounding or media requirements. A selected client provider failure is shown
and does not fall back to Cubie's managed Vertex account.

Embedding models require a matching vector index. In **Admin → Workspace**,
select each knowledge collection, then **Rebuild index**. In GCP this starts a
workspace-scoped Cloud Run Job that creates and validates a versioned candidate;
the Admin card shows source progress and a retryable failure state. The local
index rebuild runs directly. Search uses the current index throughout.
**Activate rebuilt index** switches only that collection after verification. OpenAI and Gemini
embeddings use that workspace's credential. In GCP, candidate and active
vectors live in the workspace Cloud SQL pgvector tables; the rebuild checks
every bound GCS source and the document update outbox before cutover. Verify
the 0008 vector, 0009 ordered-outbox and 0010 managed-source-proof migrations,
the pgvector extension, concurrent HNSW index creation (including an interrupted
build and retry), continued document writes during the build, source backfill, dual-write,
tenant isolation and private-file ACL against live Cloud SQL before client
activation. A failed client key or index must stop search for that collection
without using managed Vertex. Cloud rebuilds report completed and total sources,
but a safe corpus-size ceiling has not been measured. Before activation, run a
representative build at the largest intended source count using the configured
Cloud Run Job timeout and workspace database role. Record source count, total
chunks, duration, provider rate limits and peak connections; set an operational
size limit below that measured capacity. The build record treats a queued or
running Job without progress for 30 minutes as stale and permits a retry.
Repeated rebuilds currently leave retired version-specific HNSW indexes and
applied outbox history for operator maintenance. Monitor their database size
and set a reviewed cleanup/retention procedure before frequent rebuilds.
DAM imports also retain a staging GCS copy after a successful corpus import;
include that copy in storage sizing until cleanup is automated.

For each client, record who controls the provider account and who accepts its
data-processing terms before activating a workload. Rotate by saving a new key
and retesting; revoke by disconnecting the provider, then verify a selected
route fails closed. AI Cost shows provider, model, workspace feature, billing
owner and an estimated token cost. The provider's own invoice is authoritative;
pricing tiers, discounts and taxes can differ. Provider-specific rate estimates
and their source links live in `rubik_core/cost/pricing.py`.

The selected workload's prompt and response are processed by its selected
provider. Review each provider's current data-processing and retention terms
with the client before the test request:

| Choice | Destination and account to review |
| --- | --- |
| Claude | Anthropic Messages API, client's Anthropic account |
| DeepSeek | DeepSeek Chat Completions API, client's DeepSeek account |
| OpenAI | OpenAI Chat Completions API, client's OpenAI account |
| Gemini API | Google Gemini API, client's Gemini API account |
| Meta Muse | Meta Model API, client's Meta account |
| Client Vertex | Vertex AI in the client's approved project and region |

Do not assume one provider's retention or residency terms apply to another.

Stubbed provider contracts are local verification only. A live credential
smoke test for each selected provider, including revoked-key behavior and
invoice reconciliation, remains **unverified until a client supplies a test
credential and authorizes the request**. Do not mark provider acceptance from
stubbed responses alone.

## 6. Publish browser surfaces

Before enabling **multiple Gmail accounts** in a client workspace, configure
the optional `GOOGLE_GMAIL_OAUTH_CLIENT_ID`,
`GOOGLE_GMAIL_OAUTH_CLIENT_SECRET` and `GOOGLE_GMAIL_OAUTH_REDIRECT_URI` trio in
the private platform environment. The deploy binds the secret only to the
Service. Register the exact callback URL on the Google web OAuth client. This
is separate from the sign-in ID token and Workspace domain-wide delegation.
The admin option starts off; each user connects an extra mailbox under
Settings → Email Preferences and explicitly selects it in Productivity →
Email. Connected mailboxes join that user's AI Priorities and scheduled
summaries automatically with a visible mailbox label. Any card from a run
that included personal Gmail context stays out of manager/team digests;
personal mail is not logged to the shared CRM.
Disconnecting revokes its Cubie access. Live OAuth connection and revoked-token
behavior remain unverified until the client supplies an approved test account.

Turning the admin option off pauses every extra mailbox read and scheduled AI
run; existing connections stay saved so their owners can disconnect or resume
after re-enablement. If the admin changes the AI Priorities provider or model,
personal-mail processing remains paused until each owner approves the new
route. An expired or revoked extra token requires reconnecting that mailbox;
the default work mailbox and other healthy accounts continue. If the optional
OAuth client settings are incomplete, Settings explains that extra-account
connections are unavailable before opening Google. Verify these paused and
reconnect states in a client test before enabling the option.

Google classifies both `gmail.readonly` and `gmail.compose` as restricted OAuth
scopes, and the latter grants API permission to send even though Cubie's user
flow creates drafts only. The consent copy must state the actual Google scope.
External public use requires Google's restricted-scope verification; storing
restricted Gmail data server-side can require a security assessment. Complete
that review before offering personal Gmail connections to clients.

Publish the Cubie Meet add-on and Cubie for Chrome as **Public (unlisted)**
when ready. Do not choose Private visibility; it cannot later be opened to
other clients. Check the final public origin and permission descriptions before
submission. These publications are separate from the Phase 2 code rollout.

## 7. Reversal and evidence

Keep the previous image digest, schema revisions, signed release commit,
workspace roster and dry-run output with the rollout record. Follow
`docs/deployment_rollback.md` if a release fails; a rollback is also a deploy
and obeys every workspace freeze window. Record the live `/health` result,
Google OAuth and delegation tests, Slack installation test, first Fathom
delivery and provider smoke tests separately from local test results.
