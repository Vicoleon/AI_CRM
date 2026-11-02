# Cubie platform — design

Date: 2026-10-27 · Status: draft for Jose's approval · Author: Claude with Jose Leon Salgado

## 1. Intent

This codebase is an AI copilot Jose built end to end. It was built for one company and is hard-wired to it: that company's name, domain, people, cloud project, HubSpot portal and processes are everywhere.

It becomes **Cubie, by RubikSoft**: a product RubikSoft can run for many client companies from one deployment. Each company gets a **workspace** with its own isolated data, its own branding touches, and its own connected tools.

**Why now:** RubikSoft wants to offer Cubie to future clients. There are no clients yet. The first workspace is RubikSoft itself (`rubik-soft.com`), and a placeholder workspace (**Acme Corp**, `acme.example`) proves that a second company can be set up and stays isolated.

**What success looks like (this effort ends locally, per Jose)**
- Nobody can tell from the code, the UI, the docs or the history that the product belonged to anyone else.
- On Jose's Mac, one command brings up Cubie with two workspaces: RubikSoft (`rubik-soft.com`) and Acme (`acme.example`).
  - People sign in (mock sign-in locally) and land in the right workspace.
  - Each workspace sees only its own data.
  - The Connectors page, the Platform wizard and every kept feature work.
- The deploy and provisioning scripts are complete and tested in dry-run mode and unit tests, but not run against a real Google Cloud project.
- A go-live runbook lists every remaining step, in order. These are the ones only Jose or a client's admin can do: Google Cloud project, OAuth consent screen, Workspace access grant, Slack app, hostname. When the time comes, going live means following the runbook, not writing code.
- Standing up a new client is a short script or wizard run, plus the steps only the client's own admin can do.

## 2. Goals, non-goals, anti-goals

### Goals
1. **Rebrand to Cubie.** Use RubikSoft's identity and remove every trace of the previous brand (§4.1, §4.2).
2. **Delete eight modules** that only made sense for the previous company (§4.2.3).
3. **Isolate workspaces.** One app, a separate database per workspace, and a small control database (§4.3).
4. **One-command deploy and workspace provisioning scripts**, with no organisation values hard-coded (§4.4).
5. **Connectors page** (§4.5):
   - Google Workspace and Slack work on day one.
   - HubSpot, Fathom and custom MCP servers are available.
   - Microsoft Teams and Google Chat show as "coming soon", behind a shared messaging interface.
6. **New-workspace wizard** for RubikSoft staff (§4.6).
7. **Sales Coach add-on**, switched on per workspace. It is designed in its own session once goals 1–6 land (§4.7).

### Non-goals (for this effort)
- Going live. No Google Cloud project, real OAuth client, real Slack app or real domain is created or used. The scripts and the go-live runbook are the deliverable for that part.
- Publishing the Meet add-on or the Chrome extension to any store. They are rebranded and routed per workspace only. When they are published, it must be as **Public (unlisted)**, never Private: Private visibility can't be changed later and would lock clients out. The runbook says this.
- Building the Microsoft Teams or Google Chat adapters (only the interface and "coming soon" cards).
- A Spanish UI (my call; English stays the only language).
- Per-workspace custom web domains. One app host serves every workspace.
- Billing, plans or payments. Add-ons are switched on by RubikSoft staff.
- Migrating any data from the previous deployment. Every database starts empty.
- A public HubSpot OAuth app (a pasted private-app token comes first).
- White-labelling the product name. Workspaces can set their logo and colours, but the product is always "Cubie".

### Anti-goals (failure even if everything "works")
- **Any trace of the previous brand survives**: a string, a file, an asset, a stored value, an infra name, a test fixture, or the git history.
- **Data crosses workspaces.** Any code path that runs without a known workspace must raise an error, never fall back to a default database, cache, corpus or bucket prefix.
- **A workspace admin can run code on the server or reach another workspace's secrets.** The current stdio MCP loader passes every app secret to a subprocess; that path must go.
- **Kept features break silently.** Chat, briefing, meeting prep, EOS and the rest must still pass their tests.
- **Deploys depend on hand-set config** that no script declares.

## 3. Decisions Jose made

- **Name:** **Cubie**, a product of **RubikSoft**.
- **Brand:** RubikSoft's brandbook (`Consultoria/RubikSoft/BRANDING RUBIKSOFT`).
  - Colours:
    - Ink `#0E0729`
    - Violet `#7C3AED`
    - Lavenders `#A47BFF`, `#C9B1FA`, `#D5CAFC`
    - Off-white `#F2F0F7`
    - Green "Cacti" `#03553E`
  - Fonts: **Unbounded** for logo, titles and highlights; **Montserrat** for body text.
  - Logo: the RubikSoft cube icon. The vector source is `LOGOPACK/LOGOS RUBIKSOFT.ai`, which has 36 artboards and can be exported to SVG with `pdftocairo -svg`.
- **Remove every trace of the previous brand:** its name and abbreviation, its domain, the old product names (the copilot's name, the assistant's two names and the old "<abbreviation> Brain"), its staff, its clients and sister companies, its GCP project ids and numbers, its HubSpot portal, pipeline and property ids, its Cloud Run URLs, and its OAuth client.
- **Package rename:** the old package becomes **`rubik_core`**.
- **Files:** delete what isn't needed; rebrand what is needed.
- **Delete these modules:**
  - Buyer Personas
  - Conferences
  - Regulatory Watch
  - Help Desk / tickets (including the Productivity → Tasks tab)
  - Compliance Score (and its approval)
  - Vendor Questionnaire
  - System Team Knowledge Base
  - Daily Giggle
- **Keep EOS/L10** as a module that is **off by default** in each workspace.
- **Everything else stays** as core Cubie: chat, briefing, AI Priorities, meeting prep and Deep Prep, Customer X-Ray, Sales Cockpit, call capture, the Meet add-on, CRM Sync, proposals, Loose Ends, Smart Teams, to-dos and promises, scheduled tasks, meeting skills, voice, chat files, insights, page tours, help, the Chrome extension, offboarding and handoffs, and the AI cost dashboard.
- **Migrations:** the 92+ migrations collapse into **one clean baseline**.
- **The Tuesday L10 deploy freeze** becomes an **optional per-workspace setting**.
- **Database per workspace**, plus a control database. The sign-in domain picks the workspace.
- **An easy deployment script.**
- **First workspace:** RubikSoft on `rubik-soft.com`. **Placeholder:** Acme Corp on `acme.example`.
- **Connectors catalog:**
  - Day one: Google Workspace and Slack.
  - Also available: HubSpot, Fathom and custom MCP servers.
  - Planned: Microsoft Teams and Google Chat.
  - Each card shows its status, what it can access, and connect/disconnect.
  - Admins connect company-wide accounts; people connect their own where needed.
- **New-workspace setup:** an admin wizard (name, domain, logo and colours, admins, features, connectors) plus a command-line script.
- **Sales Coach:** becomes an add-on to Cubie. The code in this repo is only a read-only bridge into the separate Sales Coach database. The Sales Coach itself is rebuilt inside Cubie, unless Jose supplies its repo to port.
- **This effort ends locally.** The scripts are ready; going live comes later, by runbook.
- **The old company's industry content is replaced, not rewritten for RubikSoft:**
  - Prompts become industry-neutral and read each workspace's **company profile** (§4.2.5).
  - Proposal templates, the product/service catalog and sales call scripts become per-workspace data, empty by default.
- **What's New becomes a Cubie changelog** that RubikSoft writes once and every workspace sees. Knowledge-base capture is deleted.
- **Everyone in the company's domain gets the core app.** The department pilot gate and the "no department, no sign-in" lockout go. Admins can still narrow features by department.

## 4. Design

### 4.1 Brand

**Identity**
- The product name everywhere is **Cubie**. The vendor line is "by RubikSoft".
- The wordmark is "Cubie" set in Unbounded, next to the RubikSoft cube icon.
- The login page and the app footer carry the "Cubie by RubikSoft" lockup.

**One source of truth**
- **Frontend:** one brand module (name, vendor, support contact, logo assets) and CSS variables for colour and type. Tailwind maps a neutral `brand` token family (`brand`, `brand-hover`, `brand-tint`, `rail`, `rail-active`, `ink`, `display`/`body` fonts) onto those variables.
- **Backend:** a matching constant module. LLM prompts, emails, Slack copy and generated documents read the product name from it.
- **Replacements:**
  - Every hard-coded brand colour (about 1,700 class and hex uses of the old maroon) is replaced by these tokens.
  - The old company-named Tailwind token and the unused `primary` scale go.
  - The Inter and Manrope fonts go.

**Palette mapping** (shades are the builder's call within the brandbook)
- Primary actions and active states: violet `#7C3AED`.
- Navigation rail: ink `#0E0729`, with violet for the active item.
- Tints and soft surfaces: `#F2F0F7`, `#D5CAFC`, `#C9B1FA`.
- Secondary accent: `#A47BFF`.
- `#03553E` is used sparingly, for positive/success signals only.
- Check contrast against WCAG AA.

**Per-workspace touches**
- A workspace can set its own logo and accent colour. These apply after sign-in, through CSS variables, without a page flash.
- The workspace **company name** replaces the old company name in UI copy ("outside {company}") and in LLM prompts.
- The workspace's **company details** (legal name, address, website, short blurb, logo) feed generated documents: proposal letterhead, Deep Prep PDF/DOCX, L10 recap email, XLSX templates.
- The login page always shows platform branding, because the workspace isn't known until sign-in.

**Satellite surfaces**
- "Cubie for Chrome" (manifest, icons, keyword `cubie`, one production origin).
- The Meet add-on, named "Cubie".
- The Slack app manifest, named "Cubie".
- Favicon and page titles.
- The Chrome privacy page.

### 4.2 Removing every trace

#### 4.2.1 Scope and policy
- **What counts as a trace:**
  - Old company names: full, abbreviated, and the email domain.
  - Old product names: the copilot's name, the assistant's two names, and the old "<abbreviation> Brain".
  - The old clients and the sister company named in code and fixtures.
  - Real staff names and emails.
  - Old GCP project ids and numbers, Cloud Run hosts, the OAuth client id, the HubSpot portal, pipeline and custom property ids, and Slack channel ids.
  - The old maroon palette.
- **Where to look:** code, tests, prompts, help content, tours, docs, scripts, CI, `.claude/`, assets, file and folder names, stored identifiers, env var names (the old product-name prefix becomes `CUBIE_*` or unprefixed), infra names (service `cubie`, jobs `cubie-worker` and so on), browser storage keys, and the extension message protocol.
- **Replacement policy:**
  - A reference to the product becomes the brand constant ("Cubie").
  - A reference to the customer company becomes the workspace's company name, read from settings.
  - A company-specific value (emails, ids, rosters, pipelines, URLs, time zones) becomes per-workspace configuration, or is deleted when nothing needs it.
  - Test fixtures use `@acme.example` and `@outside.example` addresses.
  - Stored identifiers are renamed as values, not by regex. This is safe because the databases are new. Examples: ADK `app_name`, the assistant-feedback table, an `origin` value that named the old product.
- **Order:**
  1. Delete the modules and files.
  2. Freeze stored identifiers as named constants.
  3. Move the package.
  4. Externalise per-company values.
  5. Rename the env vars and infra names.
  6. Swap in brand tokens.
  7. Rewrite the copy.

  The old company's email domain contains its abbreviation, so the domain and email pass runs before any bare abbreviation pass.

#### 4.2.2 Lock it in
- A CI test scans the whole repo, text and file names, for a banned-terms pattern and fails on any hit.
- The terms are stored in a form that doesn't trip the scan itself, for example assembled from fragments or hashed.
- The scan also runs over generated frontend `dist/` output in the build check.
- **Git:** the folder is not a git repo today.
  - It is initialised locally for safety during the work.
  - When the work is accepted, the repo is re-initialised with **one clean first commit**, so no history carries a trace.
  - Nothing is pushed anywhere without Jose saying so.

#### 4.2.3 Module deletions
Each deletion removes, in one change:
- the module code and its routers, jobs and DB models
- its feature-registry spec and AuthContext key
- its AI-cost label and config settings
- its pages, routes, nav rows, help topics (both homes), tours and anchors
- its tests and docs

Inbound references from kept code are cut cleanly. The safe order is:
1. Regulatory Watch
2. Vendor Questionnaire
3. Daily Giggle
4. Compliance Score
5. Conferences
6. Buyer Personas
7. Help Desk
8. System KB

**Entanglements and how they resolve:**
- **Buyer Personas** leave Meeting Prep sections, the Meet side panel, CRM Sync (including the `update_contact_buyer_persona` change type and its HubSpot write fence) and the Deep Prep exports. Those features keep working without the persona block.
- **Compliance Score:** the X-Ray tab and the Proposals hand-off go. `briefCopy.ts` moves to the proposals components first.
- **Conferences:** the AI-Priorities conference outreach card and the voice records for conferences go. Meet "conference records" are unrelated and stay.
- **Help Desk:** removed from the AI-Priorities ticket channel and card actions, the Cockpit ticket source, Smart Teams skill evidence, chat tools and prompt, confirmation rendering and Insights. The one-way EOS bridge goes; EOS keeps working.
- **System KB:** goes with **Knowledge-base capture**, which only fed it.
  - **What's New** loses its System-KB drafting source and its per-workspace approval flow. It is rebuilt as the Cubie changelog (§4.6.1).
- **Product catalog.** The old company's priced service catalog CSV is deleted. It feeds proposals, meeting-prep rewrite, a products RAG corpus and chat. These read the per-workspace catalog instead (§4.2.5). The hard-coded products corpus path goes.
- **The external Sales Coach database bridge** is removed (my call). It reads one external database shared by the whole process, which breaks the "no default database" rule, and it pointed at the old deployment. The Sales Coach add-on brings coaching signals natively in phase 4. Until then, its toggle shows "coming soon".

#### 4.2.4 Files
- **Delete:**
  - the deploy log, AI notes (after harvesting, below) and the refactoring `.docx`
  - the EOS blueprint PDF and the product catalog CSV/MD
  - `scratch/` and all root one-off scripts and probes
  - the root `tests/` folder (move its 2 real tests into `backend/tests`)
  - the one-off repair and backfill scripts
  - the executive scorecard seed data
  - the `docs/kb` System-KB content and the docs of deleted modules
  - docs that name real people or clients
  - `.claude/kb-capture` and its hook
  - the abandoned `frontend/src/v2/` shell and its routes
  - unused Vite boilerplate
  - the old logos
- **Keep and rebrand:**
  - README (rewritten for Cubie), CLAUDE.md (rewritten; rules kept, freeze rule now per workspace), `.env.example`
  - Dockerfile: no baked OAuth client or project number; these are served at runtime or passed as build args with no defaults.
  - CI workflows, `alembic.ini`, `pyproject.toml`, `requirements.txt` comments
  - conftest, deploy and setup scripts (reshaped per §4.4), the deploy skill (rewritten), the Chrome extension, the Meet add-on docs, the Slack manifest
  - engineering docs for kept features (logging, telemetry, prompt-injection hardening, incident response with placeholders, feature design docs)
- **AI notes are harvested, not lost.** The still-valid engineering lessons (feature gating, Help mirror, EOS invariants, connection-pool math, scheduler header gotcha, Fathom path, private files, voice, Loose Ends, What's New, Meeting Skills and others) move into a scrubbed `docs/ENGINEERING_NOTES.md`, which CLAUDE.md points to.

#### 4.2.5 Industry content becomes generic and per-company
Several kept features have the old company's industry built in. Following Jose's decision, each becomes industry-neutral and reads the workspace's own data:

- **Company profile.** A new per-workspace record, edited in Admin → Workspace profile and in the setup wizard:
  - what the company sells
  - who it sells to
  - industry
  - its main products or services in a sentence each
  - its sales process in a few lines

  Every prompt that used to say "a mortgage QC/compliance firm" or name the old company's products reads this profile instead. Affected areas include X-Ray research, call capture, CRM Sync, Loose Ends, meeting-prep rewrite, proposals, Smart Teams and the chat system prompt. When the profile is empty, prompts fall back to neutral wording ("the company").
- **Product/service catalog.** Admins upload a CSV with a simple, generic shape: name, short description, category, price and unit (both optional), and a link. It is empty by default.
- **Proposal templates.** Become per-workspace data with a simple admin editor, empty by default.
  - A template is a named service write-up plus optional catalog rows for pricing.
  - Proposals keep their AI-written sections. When no template matches, the proposal is built from those sections plus catalog rows.
  - Pricing still never passes through AI prose.
  - Letterhead (one per workspace) comes from the company details.
  - The sister-company letterhead goes.
- **Sales call scripts and play keys.** Become per-workspace data, edited where call scripts are edited today, and empty by default. The old company's scripts are deleted.
- **Smart Teams skills taxonomy.** Replaced by a short, neutral, industry-agnostic starter taxonomy (general business, sales, operations, technology and people skills), seeded into each workspace. Admins can extend it.
- **Graceful empty states.** Every feature that reads these says plainly what's missing and links the admin to where to add it. It never invents content.

#### 4.2.6 Migrations
- After the deletions, one baseline migration is generated from the models.
- **Fidelity is proven by a schema diff** between a database built from the old chain (minus deleted tables) and one built from the baseline. The diff covers partial indexes, enums, CHECK constraints, and the tables created under variable names.
- Seed rows that the old migrations inserted move into a `seed_workspace()` step: the default priority playbook, Drive label priorities, SLA config placeholders and the Smart Teams taxonomy.

### 4.3 Multi-workspace

#### 4.3.1 Shape
- **Control database**, with its own Alembic folder. Tables:
  - `workspaces`: slug, name, status, DB name, time zone, company details, Google DWD config, storage prefix, corpus ids, freeze windows.
  - `workspace_domains`: unique domain, mapped to a workspace.
  - Branding: logo and colours.
  - Module and add-on enablement.
  - Company connectors: status, non-secret config, secret references, who connected it, last check.
  - Platform admins (RubikSoft staff).
  - Provisioning steps, for resumable setup.
- **Workspace databases:** one per workspace, all on the same Postgres instance, each with its own DB user, and all built from the same baseline.

#### 4.3.2 Routing every request and job to the right database
- **Sign-in:**
  - Verify a Google **ID token** locally (`aud`, `iss`, `hd`).
  - Route on the hosted domain, not the email suffix.
  - Look up the workspace in the control database, mint the app session token with a workspace claim, and sign it with a **platform** secret.
  - An unknown domain gets a clear "your company isn't set up on Cubie" page.
- **Each request:**
  - An outermost ASGI middleware reads the workspace from the session token and sets a context variable.
  - `SessionLocal()` and `get_engine()` resolve the engine for that workspace through an engine registry (lazy, small pools, least-recently-used disposal). The ~700 existing call sites keep working unchanged.
  - The server process has **no default workspace**: missing context raises.
  - Each request also checks that the user's domain still belongs to that workspace.
- **Inbound calls with no user:**
  - Slack events and commands route by Slack `team_id`.
  - Fathom webhooks go to a per-workspace URL path.
  - Chrome extension keys embed the workspace.
  - Scheduler calls carry the scheduler key and either name a workspace or fan out.
- **Scheduled work and jobs:**
  - Scheduler endpoints fan out across active workspaces that have the module on, with bounded concurrency. One workspace's failure doesn't stop the others.
  - Cloud Run job launches carry `CUBIE_WORKSPACE`. Job processes bootstrap their workspace from env.
  - Batch runners run per workspace.
- **Threads:** every executor submit, daemon flush thread and in-thread `asyncio.run` copies the context. A lint test rejects raw submits.
- **Process-wide caches and buffers** are keyed by workspace or moved per workspace: about 20 caches, the AI-cost and Insights buffers, the secret cache (now with a TTL), and the feature-access cache.
- **ADK chat sessions** get one session service per workspace.

#### 4.3.3 Per-workspace storage and credentials
- GCS prefix `workspaces/<slug>/`.
- Vertex RAG corpora are namespaced `<slug>__<name>`, and their ids are stored on the workspace. This avoids project-wide display-name collisions.
- Local Chroma has one directory per workspace.
- **Google Workspace:** one service account per workspace in RubikSoft's project, used **keyless** (the runtime account signs as it). The workspace's own admin grants domain-wide delegation to its client id. The one credentials loader resolves the current workspace's account and delegation subject.
- **Company connector secrets** live in Secret Manager as `ws-<slug>-<connector>-<field>`. The control database stores references only.
- **Personal tokens** (for example Slack user tokens) stay in the workspace database, **encrypted**. They are plaintext today.
- Locally, an encrypted local store stands in for Secret Manager.

#### 4.3.4 Roles and modules
- **Platform admin:** RubikSoft staff, recorded in the control database. Can create and manage workspaces and switch add-ons on. Workspace admins never see platform screens.
- **Workspace admin:** today's admin role, scoped to its own database.
- **Support access (my call):** a platform admin can **enter** a client workspace to set it up or support it.
  - The session is not tied to the sign-in domain.
  - A persistent "Acting in Acme as RubikSoft support" banner shows the whole time.
  - Every action is written to that workspace's audit log, attributed to the RubikSoft person.
- **Effective access** = env kill switch (platform ceiling) **AND** the workspace's module flag **AND** the existing `feature_access` roster.
- The feature registry gains a module field: core, EOS, or the Sales Coach add-on.
- **Default access:** everyone in the workspace's domain gets the core app.
  - The "pilot departments" gate and the "no department, no sign-in" lockout are removed.
  - Departments sync from Google when available and are optional.
  - Feature defaults that were limited to the old company's departments open to everyone; admin-only and manager-only features stay that way.
  - Hard-coded rosters of real people's emails in the registry are removed.
  - Admins can still narrow any feature by department in Feature Access.

#### 4.3.5 Local development
- One command brings up:
  - a control SQLite file and two workspace SQLite files (RubikSoft and Acme)
  - seeded with demo-safe data
  - mock sign-in choosing the workspace by email domain
- Tests run against an explicit "test" workspace. There is a dedicated test proving that two workspaces never see each other's rows, caches or corpora.

### 4.4 Deploy and provisioning scripts

All scripts take an environment file and flags. They hard-code no organisation values.

- **`scripts/platform/bootstrap_gcp.sh --env <env>`**, run once per GCP project:
  - enable APIs
  - create the Postgres instance, bucket, Artifact Registry repo and runtime service account
  - set IAM
  - create the platform secret placeholders
  - create the control database
- **`scripts/platform/deploy.sh --env <env>`:**
  1. Guards:
     - clean tree
     - a **freeze guard** that blocks if *any* workspace's freeze window is active (fails closed, with break-glass by hand)
     - the live database revisions are known to the tree
  2. Build the image once and pin it by digest.
  3. Run a **migrate job**: the control database, then each workspace in turn under an advisory lock. Stop on failure.
  4. Deploy the service declaratively: all env and secrets are set by the script, so nothing is hand-set.
  5. Deploy jobs from a manifest (`deploy/jobs.yaml`).
  6. Apply scheduler entries from a manifest (`deploy/schedules.yaml`), idempotently, always with all headers.
  7. Run a smoke test: `/health` shows the commit and each workspace's revision.
- **`scripts/platform/workspace.sh create|list|migrate|delete`** wraps the Python provisioning CLI.
  - **create** takes slug, name, domain(s), first admin(s), delegation subject, time zone, logo, accent colour, modules and an optional freeze window. Each step is resumable, and there is a dry run. The steps:
    1. validate
    2. check connection headroom
    3. create the database and its DB user (password into Secret Manager)
    4. migrate
    5. create the storage prefix and corpora
    6. create the workspace's Google service account
    7. register it in the control database
    8. seed the first admin and defaults
    9. print the checklist only a human can do (the exact client id and scopes for the delegation grant, the Slack install link, how to paste a HubSpot or Fathom key)
  - **delete** requires typing the slug.
- **`scripts/dev/up.sh`** sets up the local equivalent.
- The wizard (§4.6) calls the **same** provisioning service as the CLI.

### 4.5 Connectors

#### 4.5.1 Model
- A backend connector registry declares each connector:
  - key, name, category, logo (from the `simple-icons` package per Jose's rule; where a brand is missing, a generic icon, never a hand-drawn logo)
  - scope (company, personal or both), availability (available or coming soon), auth method
  - the config and secrets it needs, a health check
  - the chat tools and AI-Priorities signals it provides, and the features that require it
- A readiness check combines "the company connection is healthy" with "this person has connected", where personal.
- **Chat tools and priority signals are registered only when their connector is ready.** The model is never offered a tool the person can't use. The same gate feeds `/api/users/me`.

#### 4.5.2 Catalog

| Connector | Scope | Day-one state | How it connects |
|---|---|---|---|
| Google Workspace | Company | Available | The workspace's admin grants domain-wide delegation to Cubie's per-workspace client id with the listed scopes and names a delegation subject. Test = impersonate the subject and read their calendar. Replaces Admin → Google Workspace (sync settings and group→role mappings move into this connector's detail). |
| Slack | Company + personal | Available | **Each client creates its own Cubie Slack app from a manifest Cubie provides** (my call), then pastes its client id, secret and signing secret into the connector. Slack's 2025 rules throttle message-history reads for commercially distributed apps that aren't in the Slack Marketplace; an app a company creates for itself is exempt. A Marketplace listing can come later. The bot token is stored per workspace; inbound calls route on `api_app_id` + `team_id`. People connect their own user token for reading their messages. Replaces Settings → Slack. Disconnecting a person never removes the company bot. |
| HubSpot | Company | Available | Admin pastes a private-app read token (and optional write token). Portal id, pipelines that count, and any custom property names are connector config, not code. |
| Fathom | Company | Available | Admin pastes the API key and webhook secret. Cubie shows the workspace's webhook URL. |
| Custom MCP server | Company | Available | Remote HTTP/SSE servers only (no local commands). URL, auth header, a tool allow-list that is actually enforced, and read-only by default. |
| Microsoft Teams | Company | Coming soon | Card only. |
| Google Chat | Company | Coming soon | Card only. |

The Chrome extension connection (per-person keys) also appears in the personal section.

#### 4.5.3 Messaging layer
- One messaging interface that Slack implements now and Teams/Google Chat implement later. It covers:
  - sending a direct message, posting to a channel
  - finding a channel and resolving a person
  - interactive buttons and forms where supported
  - a directory snapshot, for offboarding detection
- Messages are written once in a portable shape, and each provider renders it (Slack Block Kit today). The provider owns the "rich, then plain-text fallback" logic that is copied in six places today.
- **Every outbound Slack caller moves to it:**
  - AI-Priorities delivery, manager digest, close-date roll-up
  - Cockpit digest and nudges, manager follow-up
  - scheduled-task delivery, ingestion council
  - What's New approvals, the Loose Ends daily digest
- **Inbound** commands and interactions come through one route per provider and resolve the workspace from the provider's tenant id.
- A per-person table stores each provider identity and conversation reference, because Teams and Google Chat can't message someone cold.
- Each workspace has a default messaging provider.

#### 4.5.4 UI
- A **Connectors** area in the main navigation, visible to everyone:
  - **Company section:** admins can act; others see read-only status, "Connected by your admin".
  - **Personal section.**
- **Each card:** logo, status (connected / needs attention / not connected / coming soon), and a one-line "what Cubie can read or do with it".
- **Each card's detail drawer:**
  - the access it grants
  - setup steps (including the exact client id and scopes for Google)
  - "test connection"
  - last checked, last error
  - connect or disconnect
- Ships with its page tour, Help topic in both homes, and feature-registry entry, per project rules.

### 4.6 New-workspace setup (UI)
- A **Platform** area, visible only to platform admins:
  - a list of workspaces with status and provisioning progress
  - a **New workspace** wizard:
    1. Company: name, slug, domains, time zone, company details.
    2. Brand: logo, accent colour, live preview.
    3. People: first admins, delegation subject.
    4. Modules: EOS, Sales Coach add-on.
    5. Review → provision (runs as a background job with step-by-step progress).
    6. Done: the human checklist, with copy buttons.
- Each workspace's own Admin gains a **Workspace profile** tab. It edits company details, company profile (§4.2.5), logo, accent colour, time zone and the optional deploy freeze window, reusing the wizard's brand step. The wizard's Company step also collects the company profile.
- **Workspace colours** (my call):
  - A workspace sets **one accent colour**. It replaces Cubie's violet for buttons and active states, and hover and tint shades are derived from it.
  - The navigation rail stays Cubie ink.
  - The wizard rejects an accent that fails WCAG AA contrast on white, showing why.
  - The workspace logo sits in the app header next to a small Cubie mark.

#### 4.6.1 Cubie changelog (What's New)
- **Authoring:** platform admins write release notes in the Platform area: title, plain-language body, the feature or page it touches, and a publish date. An optional "draft it for me" turns a few bullet points into plain-language copy.
- **Storage:** notes live in the control database. This is the one deliberate path where platform content reaches every workspace. It is read-only, platform-owned, holds no workspace data, and the isolation tests allow exactly this table.
- **What workspaces see:** the existing What's New drawer, button and spotlight, filtered to features that workspace has switched on.
- **What stays per workspace:** "seen" and "opened" counts stay in each workspace database. Each workspace can opt into a digest via its messaging connector.
- **What's removed:** the per-workspace approval flow, the approver Slack buttons and System-KB drafting.

### 4.7 Sales Coach add-on (phase 4, designed separately)
- **What it is:** an add-on module switched on per workspace by a platform admin. It reuses Cubie's call transcripts (Meet and Fathom), research and email delivery.
- **Shape to match Jose's existing Sales Coach:**
  1. Score each call against the company's sales framework.
  2. Research the prospect to judge the handling.
  3. Email each rep a morning PDF with their score and recommendations.
  4. Send weekly scorecards.
  5. Keep markers of what works.
- **The existing bridge** that turns coaching weak spots into AI Priorities becomes internal to the add-on.
- **The detailed design** gets its own brainstorm once phases 1–3 land. If Jose supplies the Sales Coach repo, it is ported instead of rebuilt.

## 5. Phases and order

Each phase ends green: `ruff`, `pytest`, and the frontend type-check, lint, tests and build.

0. **Baseline.**
   - Initialise git locally and record the current test results, so later failures can be told apart from pre-existing ones.
   - Install the frontend dependencies.
1. **Rebrand and clean-up** (goals 1–2), in the §4.2.1 order. Ends with the banned-terms scan passing and the migration baseline proven.
2. **Workspaces** (goals 3–4):
   - control database
   - routing
   - storage namespacing
   - job fan-out
   - scripts
   - local two-workspace setup
   - isolation tests
3. **Connectors and setup** (goals 5–6):
   - connector registry
   - messaging layer
   - Connectors page
   - Platform area and wizard
   - Workspace profile
4. **Sales Coach add-on** (goal 7): separate design first.

## 6. Open questions for Jose
None. These were answered on 2026-10-27:
- go live later, by runbook;
- industry content becomes generic and per-company;
- What's New becomes the Cubie changelog;
- everyone in the domain gets the core app.

**The go-live runbook** (`docs/GO_LIVE.md`) is written as part of phase 2. It covers:
- choosing the Google Cloud project, region and hostname;
- `bootstrap_gcp.sh`;
- the OAuth consent screen and client;
- RubikSoft's own Workspace delegation grant and Slack app;
- `deploy.sh` and `workspace.sh create rubiksoft`;
- the Meet and Chrome publishing rule;
- the Cloud SQL tier and connection-pool sizing, leaning low-cost.

## 7. Constraints
- **Project rules in CLAUDE.md still apply:**
  - feature gating through the registry
  - Help in both homes
  - page tours for user-visible changes
  - an AI-cost category for every AI feature
  - copilot routers on the `core` parent
  - departments compared by slug
  - ruff before Python is done
- **No deploys happen during this work.** No Cubie infrastructure exists yet, and the old company's infrastructure is never touched. Scripts are proven with a `--dry-run` mode that prints every `gcloud` and SQL action, and with unit tests in the style of today's deploy-guard tests.
- **Builds and deploys run from Jose's workstation.** CI runs checks only (lint, tests, banned-terms scan, frontend build); it doesn't deploy.
- **Acme exists only locally and in tests.**
- The Docker image runs Python 3.10. Keep code 3.10-compatible unless the image is upgraded deliberately.
- **Google:**
  - Sign-in uses one External OAuth client (needs RubikSoft brand verification).
  - Gmail and Drive access go through domain-wide delegation granted by each client's admin.
  - Both are manual steps outside code.
- **Connection budget:** workspaces × instances × pool must stay under the Postgres `max_connections`.

## 8. Left to the builder
- Exact shades, token names, the font-loading method and the icon set within the brand constraints.
- Module layout inside `rubik_core` (for example `tenancy/`, `connectors/`, `messaging/`, `platform/`).
- Control-database column details, JWT claim names, pool sizes and LRU limits.
- Env var naming after the old product-name prefix goes.
- How the banned-terms list is stored.
- How tests are re-pointed (a new `acme.example` test domain in the shared conftest first, then a fixture rewrite).
- Which harvested lessons go into `docs/ENGINEERING_NOTES.md`.

## 9. Calls Claude made (check these)
- Package `rubik_core`; env prefix `CUBIE_`; service and job names `cubie*`.
- Each client creates its own Slack app from Cubie's manifest. This avoids Slack's throttling of non-Marketplace distributed apps, at the cost of one extra setup step for the client.
- RubikSoft platform admins can enter a client workspace for support, with a banner and an audit trail.
- A workspace sets one accent colour; the rail stays Cubie ink; low-contrast accents are rejected.
- The external Sales Coach database bridge is removed now; coaching signals return with the add-on.
- A neutral starter skills taxonomy replaces the old one.
- Workspaces may set their own logo, colours and company details, but not the product name.
- English only.
- A per-workspace product catalog (CSV upload, empty by default) replaces the deleted price list.
- One service account per workspace for Google, used keyless.
- One distributed Slack app.
- HubSpot by pasted token first.
- Custom MCP servers are remote-only.
- The abandoned `v2` frontend shell is deleted.
- Git history is re-initialised as one clean commit at the end; nothing is pushed without Jose.
