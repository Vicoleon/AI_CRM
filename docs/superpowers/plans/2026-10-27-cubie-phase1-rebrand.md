# Cubie Phase 1 — Rebrand and clean-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-company copilot into "Cubie by RubikSoft". Delete the eight old-company modules, rename the package, make industry content generic and per-company, and leave zero trace of the previous brand, with every kept feature still green.

**Architecture:**
- Work in the design's §4.2.1 order: delete modules and files, freeze stored identifiers as constants, move the package, externalise per-company values, rename env vars and infra, swap in brand tokens, rewrite the copy, squash migrations.
- A banned-terms scan test is written first. It stays `xfail(strict=True)` until the last task flips it to a hard pass.
- Multi-workspace routing is Phase 2. In this phase "per-company" data lives in the (single) workspace database, which Phase 2 multiplies.

**Tech Stack:**
- Backend: Python 3.10, FastAPI, SQLAlchemy, Alembic, pytest + xdist, ruff 0.16.0.
- Frontend: React + Vite + TypeScript, Tailwind, node:test.
- Local Postgres binaries (`/usr/local/bin/pg_ctl`) for the schema diff.

**Spec:** `docs/superpowers/specs/2026-10-27-cubie-platform-design.md`. Executors read both documents.

**Reference maps** (read-only survey output, file:line detail):
- `$MAPS/module_removal.md` — the removal map for every module.
- `$MAPS/branding.md` — every brand occurrence category, plus the do-not-blind-replace list.
- `$MAPS/files_docs.md` — keep/delete verdict per non-core file.
- `$MAPS/frontend_shell.md` — frontend shell and tokens.
- `$MAPS/tenancy_identity.md` — the org-specific config table.

`$MAPS` = `/private/tmp/claude-502/-Users-joseleonsalgado-Documents-Mirror-Mirror-development-AI-CRM/18f6dcfe-1d12-42b1-aa59-383dedb269bb/scratchpad/map`. Line numbers in the maps refer to the baseline commit and drift as tasks land, so re-grep.

## Global Constraints

- Product name `Cubie`; vendor `RubikSoft`; product slug `cubie`; Python package `rubik_core`; env prefix `CUBIE_`.
- Brand colours:
  - ink `#0E0729`, violet `#7C3AED`
  - lavenders `#A47BFF`, `#C9B1FA`, `#D5CAFC`
  - off-white `#F2F0F7`, green `#03553E` (success only)
- Fonts: Unbounded for display, Montserrat for body.
- Banned anywhere in the repo, in both file contents and file names, outside `.git/`, `node_modules/`, `.venv/` and `dist/`:
  - the old company's name, abbreviation and email domain
  - the old product names: the copilot's name, the assistant's two names, the old "<abbrev> Brain"
  - the old client and the sister company
  - the old GCP project id and number, and the old HubSpot portal id
  - real staff names and emails
  - the old maroon palette (`<maroon>`, `<maroon-dark>`, `<maroon-darker>`, `<maroon-tint>`)
- Test addresses use `@acme.example`; off-domain probes use `@outside.example`; demo people are fictional.
- Python stays 3.10-compatible. Run `ruff check --fix . && ruff format .` before every commit that touches Python.
- **Project rules (CLAUDE.md) still bind:**
  - Feature gating only through `features/registry.py`.
  - Help in both homes: the frontend topic file and the backend `help/content.py`.
  - Tours follow anchors (`tours.test.ts`).
  - Every AI feature has an AI-cost label.
  - Compare departments by slug.
- Frontend lint baseline is **113 errors / 13 warnings**. A task may not raise either count.
- **Baselines:**
  - frontend tests: 1085/1085 passing; frontend build: passes.
  - backend tests: see `$BASE/pytest.log` and `$BASE/junit.xml`, where `$BASE` is the sibling `baseline/` folder of `$MAPS`.
  - A backend test that failed at baseline may stay failing. Every other test must pass, or be deleted together with the module it tests.
- **Verification commands, run from the repo root.** The env block is the CI env:

  ```bash
  export APP_MODE=dev_mock_auth DATABASE_URL=sqlite:///./test.db GOOGLE_WORKSPACE_DOMAIN=company.com CLOUDSQL_CONNECTION_NAME="" DB_USER="" GCP_PROJECT_ID=mock-project PYTHONPATH=.
  .venv/bin/ruff check . && .venv/bin/ruff format --check .
  rm -f test.db && .venv/bin/alembic upgrade head
  .venv/bin/pytest -n 8 -q -p no:cacheprovider
  (cd frontend && npm test && npm run build && npm run lint)
  ```

- **Commits** are local only, with the repository's configured Git identity, a conventional-commit subject, and a trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Nothing is pushed.

## Review Focus

1. **Empty company profile.** With no profile saved, every prompt and every piece of UI copy that used to name the old company must still read naturally, using "the company" or "your company". It must never show "None", an empty name, or a raw `{company}` token. Pinned in Task 12.
2. **Unknown feature keys left over in storage.** A `feature_access` row or a user preference that names a deleted feature (for example `help_desk`) must not crash `/api/users/me` or the Feature Access tab. Unknown keys are ignored. Pinned in Task 2.
3. **A person with no department signs in.** They get 200, `features.copilot == true`, and land on `/`, not an EOS page that is off. Pinned in Task 11.
4. **Brand terms in generated artifacts.** Download filenames (Scorecard/VTO XLSX templates), PDF/DOCX exports and the built frontend bundle must contain no banned term. Pinned in Tasks 15 and 20.
5. **A browser holding the old storage keys** (the old token, tour progress, chat streaming flag) still loads the app. The old keys are simply ignored, with no crash. Pinned in Task 14.

---

### Task 1: Banned-terms guard (red first)

**Files:**
- Create: `backend/tests/test_no_legacy_brand.py`

**Interfaces:**
- Produces: `LEGACY_PATTERN: re.Pattern` and `scan_repo(root: Path) -> list[tuple[str, int, str]]`, which returns `(path, line_no, match)` and treats a file-name hit as line 0. Task 20 removes the `xfail`.

- [ ] **Step 1:** Write the test.
  - Build the banned terms from fragments, so this file never matches itself. Example: `"mq" + "mr"`, `"mqm" + "research"`, `"At" + "las"` (word-bounded, case-sensitive for the product name), `"\\bBri" + "an\\b"`, `"Jar" + "vis"`, `"Ato" + "mus"`, `"\\bS" + "QC\\b"`, `"4746" + "18"`, `"5947116" + "09963"`, `"52183" + "64"`, `"#8527" + "33"`, `"#6a1f" + "29"`, `"#5E1B" + "24"`, `"#f4e6" + "e8"`. Use a case-insensitive pattern for everything except the product names.
  - Skip `.git/`, `node_modules/`, `.venv/`, `dist/`, `__pycache__/` and binary files: a file counts as binary when it has a NUL byte in its first 8 KB. Scan file names as well as contents.
  - `test_repo_has_no_legacy_brand` asserts `scan_repo(REPO_ROOT) == []` and prints the first 50 hits.
  - Mark it `@pytest.mark.xfail(strict=True, reason="Phase 1 in progress — Task 20 removes this")`.
- [ ] **Step 2:** Run `.venv/bin/pytest backend/tests/test_no_legacy_brand.py -q`. Expected: `1 xfailed`.
- [ ] **Step 3:** Commit: `test: add legacy-brand scan (xfail until Phase 1 completes)`.

### Task 2: Delete Regulatory Watch, Vendor Questionnaire and Daily Giggle

**Files:**
- Delete:
  - Regulatory Watch: the package (`regwatch/`), `database/regwatch.py`, `routers/regwatch.py`, `jobs/regwatch_poll_job.py`, `RegulatoryWatch.tsx`, its help topic and `test_regwatch.py`.
  - Vendor Questionnaire: `routers/vendor_questionnaire.py`, `services/voice_vendor.py`, `pages/VendorQuestionnaire.tsx`, `components/VendorQuestionnairePanel.tsx`, `api/vendorQuestionnaire.ts` and its tests.
  - Daily Giggle: the giggle package, the `DailyGiggle` model, `components/DailyGiggle.tsx`, `pages/GiggleReview.tsx`, its help topic and `test_giggle_generation.py`.
- Modify every inbound reference listed for these three modules in `$MAPS/module_removal.md` §3, §6 and §8: `main.py` mounts, `models.py` side-effect imports, registry specs, config, AI-cost labels, voice commands and records, home endpoints, browser glance and the Chrome extension giggle view, App routes, Layout rows, AuthContext keys, help in both homes, tours, What's New `products.py`/`logic.ts` path lists, and deploy scripts.
- Create: `backend/tests/test_removed_modules.py`.

**Interfaces:**
- Produces: `REMOVED_FEATURE_KEYS: frozenset[str]` in `test_removed_modules.py`. Later tasks add to it.

- [ ] **Step 1: Write the failing test** `test_removed_modules.py`:
  - `test_removed_features_absent` asserts that none of `{"regulatory_watch","vendor_questionnaire_tool","daily_giggle_review"}` is in `FEATURES`.
  - `test_removed_packages_not_importable` uses `importlib.util.find_spec` and asserts `None` for each removed package.
  - `test_unknown_feature_rows_ignored` inserts a `feature_access` row with key `"regulatory_watch"`, then asserts that `GET /api/users/me` returns 200 and that `"regulatory_watch" not in body["features"]` (Review Focus 2).
- [ ] **Step 2:** Run it. Expected: FAIL on the first two tests. The third may already pass; keep it.
- [ ] **Step 3:** Delete the files and cut the inbound references. Make `features/policy.py` resolution skip keys not present in `FEATURES`, if it doesn't already.
- [ ] **Step 4:** Run the full verification block. Expected: green against the baseline, and lint counts not raised.
- [ ] **Step 5:** Commit: `refactor: remove Regulatory Watch, Vendor Questionnaire and Daily Giggle`.

### Task 3: Delete Compliance Score

**Files:**
- Move first: `frontend/src/components/complianceScore/briefCopy.ts` → `frontend/src/components/proposals/briefCopy.ts`. Drop the `compliance_review` origin and update its importers.
- Delete: the `compliance_score` package, `database/compliance_score.py`, `routers/compliance_score{,_admin}.py`, `components/complianceScore/`, `api/complianceScore.ts`, `admin/ComplianceRubricTab.tsx`, its help topic, its docs and its tests.
- Modify: the inbound references in `$MAPS/module_removal.md` §5, including the Proposals hand-off (`from_compliance_review`, `start_from_compliance_review`, `existing_for_compliance_review`, `origin` value), the X-Ray tab, Insights, `adminSections.ts` and tours.

- [ ] **Step 1:** Add `compliance_score` and `compliance_score_approval` to `REMOVED_FEATURE_KEYS`, and the package to the not-importable list. Run; expected FAIL.
- [ ] **Step 2:** Delete and rewire as above.
- [ ] **Step 3:** Run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `refactor: remove Compliance Score`.

### Task 4: Delete Conferences

**Files:**
- Delete: the conferences package, `database/conferences.py` (6 tables), `routers/conferences.py`, the 3 conference jobs, `services/conference_feedback_email.py`, `pages/sales/{Conferences,ConferenceDetail}.tsx`, `api/conferences.ts`, its help topic and the 12 `test_conference_*.py`.
- Modify: `$MAPS/module_removal.md` §2 inbound edits (priorities injection and models, voice records, jobs registry, insights, sales model roles, App/Layout, tours). **Do not** touch Meet "conference records" (`calls.py`, `sales_kaizen.py`, calendar `conferenceData`).

- [ ] **Step 1:** Extend `test_removed_modules.py`: the package is not importable, and the `/api/conferences` route is absent (`app.routes` has no path starting `/api/conferences`). Run; expected FAIL.
- [ ] **Step 2:** Delete and rewire.
- [ ] **Step 3:** Run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `refactor: remove Conferences`.

### Task 5: Delete Buyer Personas

**Files:**
- Delete: the buyer-personas package, `crm_sync/buyer_personas.py`, `components/meetingPrep/{BuyerPersonasCard.tsx,buyerPersonas.ts}`, `pages/meetingPrep/buyerPersonas.test.ts`, its help topic, `docs/buyer_personas/` and the 7 persona tests.
- Modify: `$MAPS/module_removal.md` §1. This covers the Meet add-on payload and panel, home/meeting-prep sections, CRM Sync (remove the `update_contact_buyer_persona` change type and its HubSpot write fence for the old custom properties), Deep Prep PDF/DOCX, chat tool `lookup_buyer_persona`, the coordinator prompt, insights, sales model roles, tours and help.

- [ ] **Step 1:** Extend `test_removed_modules.py` with `sales_buyer_personas` and the package. Add a test that CRM Sync's change-type enum has no buyer-persona member. Run; expected FAIL.
- [ ] **Step 2:** Delete and rewire. Meeting Prep, the Meet panel, CRM Sync and Deep Prep render without the persona block, and their existing tests are edited only to drop persona assertions.
- [ ] **Step 3:** Run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `refactor: remove Buyer Personas`.

### Task 6: Delete Help Desk / tickets

**Files:**
- Delete everything in `$MAPS/module_removal.md` §4 "Delete":
  - the tickets package, the five ticket routers and the ticket-resolution job
  - `tools/tickets_tools.py`, `priorities/agents/tickets_agent.py`, `cockpit/ticket_source.py`, `help/help_desk_content.py`
  - the `TicketCredential` and `TicketResolutionState` models
  - `components/tickets/`, `pages/HelpDesk.tsx`, `api/tickets.ts`, its help topic, its docs and its 16 tests
- Modify the §4 inbound edits:
  - `backend/auth.py` tasks-tab gates
  - priorities models, orchestrator and agents
  - `home.py` ticket endpoints and ownership kinds (keep the generic `covered_entity_*` fields)
  - coordinator tools and prompt; confirmation renderer
  - Smart Teams skill evidence; the Cockpit ticket poll (scheduler route, sales_kaizen, jobs registry, commitments source)
  - What's New products; insights
  - `AIPrioritiesPanel.tsx` ticket components; Homefeed, Productivity and Settings tabs
  - Chat suggestions; Layout Support area; tours; help

- [ ] **Step 1:** Extend `test_removed_modules.py` with `help_desk` and `tasks_tab`, the package, and no `/api/tickets` routes. Add a test that `ChannelKind` has no `TICKETS` member. Run; expected FAIL.
- [ ] **Step 2:** Delete and rewire.
- [ ] **Step 3:** Run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `refactor: remove Help Desk integration`.

### Task 7: Delete System KB and KB capture; reduce What's New to a reader

**Files:**
- Delete:
  - System KB: `system_kb.py`, `system_kb_watcher.py`, `tools/system_kb_tools.py`, `routers/system_kb.py`, `pages/SystemKB.tsx` and its help topic.
  - KB capture: the `kb_capture` package, `services/kb_capture_actions.py`, the `kb_capture` feature and their tests.
  - What's New drafting: drafts, sync, approvals and approver Slack buttons. This means `whats_new/drafts.py`, the sync and approval parts of `whats_new/*`, `whats_new_admin.py` approval routes, `components/whatsNew/ApprovalsPanel.tsx` + `approvals/*`, and the approval/sync scheduler routes.
- Modify: `whats_new` keeps only the reader: the drawer, button, spotlight, seen/opened counts, and the per-workspace digest switch.
- Create: `rubik_core/whats_new/changelog.py`. (While the package still has its old name, write it at the same relative path under the current package; Task 9's move carries it over.)

**Interfaces:**
- Produces:
  - `ChangelogEntry` (frozen dataclass: `id: str, title: str, body: str, feature_key: str | None, path: str | None, published_at: datetime`).
  - `ChangelogSource` (Protocol: `def list_published(self) -> list[ChangelogEntry]`).
  - `EmptyChangelogSource`.
  - `get_changelog_source() -> ChangelogSource`.
- The What's New router reads `get_changelog_source().list_published()`, filtered by `feature_allows`. Phase 3 adds the control-database implementation.

- [ ] **Step 1:** Write the failing tests:
  - `test_removed_modules.py` gains `system_kb` and `kb_capture`.
  - `test_whats_new_router.py` is rewritten so that `GET /api/whats-new` returns `{"items": []}` with `EmptyChangelogSource`, and with a fake source returning one entry for a disabled feature, still returns `[]`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Delete, implement `changelog.py`, and rewire the What's New router and frontend so the drawer is empty-state friendly ("No updates yet").
- [ ] **Step 4:** Run the full verification. Expected: green.
- [ ] **Step 5:** Commit: `refactor: remove System KB and KB capture; What's New reads a changelog source`.

### Task 8: Delete files and dead code

**Files:**
- Delete everything marked DELETE in `$MAPS/files_docs.md`:
  - Root one-off scripts, probes and `scratch/`.
  - The deploy log, the refactoring `.docx` and the EOS blueprint PDF.
  - The product catalog CSV/MD **and** its runtime copy under `retrieval/resources/`. Remove the Dockerfile `test -s` line. Proposals and meeting-prep get catalog data back in Task 12; until then, catalog readers return an empty list.
  - Stray package scripts.
  - The `scripts/` repair, backfill, probe and debug scripts and the Vector Search scripts.
  - The executive scorecard seed and data. Rewrite `test_eos_scorecard_format.py:214` against a fictional fixture.
  - The root `tests/`, after moving `test_ingestion.py` into `backend/tests/`.
  - `docs/kb/`, the docs of deleted modules, docs naming real people or clients, `docs/diagrams/`, the sales-kaizen decision and email drafts. First move the 19 HubSpot property names that `test_call_capture_hubspot_writes.py:251` needs into a test fixture.
  - `.claude/kb-capture/` and its hook in `.claude/settings.json`.
  - `frontend/src/v2/` and the `/v2` routes. Keep the `body[data-density]` CSS variables only if something else uses them.
  - `App.css`, `react.svg`, the frontend README and `uv.lock`.
  - The external Sales Coach DB bridge: `integrations/sales_coach.py`, `backend/signals/sales_coach.py`, its config fields and its tests. Remove it from the `multi_source_priorities` signal list.
- Create: `docs/ENGINEERING_NOTES.md`, harvested from `AI_NOTES.md` using the still-valid section list in `$MAPS/files_docs.md`, scrubbed of names, ids and the old company. Then delete `AI_NOTES.md`.

- [ ] **Step 1:** Write a failing test, `test_removed_modules.py::test_removed_files_absent`. It asserts that a list of representative paths does not exist (the deploy log, AI notes, the `.docx`, the PDF, the CSV runtime copy, `scratch/`, `frontend/src/v2`, `.claude/kb-capture`, `integrations/sales_coach.py`). Run; expected FAIL.
- [ ] **Step 2:** Delete, and fix any import or test that breaks.
- [ ] **Step 3:** Run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `chore: delete one-off scripts, old docs and dead code`.

### Task 9: Freeze stored identifiers, then move the package

**Files:**
- Create: `rubik_core/identifiers.py` (written inside the old package in Step 2; it moves with the package in Step 3).
- Move: the old package directory → `rubik_core/`. Update every import, quoted module path, `patch()` target, `python -m` string, Dockerfile `COPY`, `.gcloudignore`, `alembic/env.py`, `conftest.py`, and frontend `readFileSync('…/whats_new/…')` paths.
- Rename symbols: the old retrieval-chain class → `KnowledgeChain`; the old feedback model → `AssistantFeedback`, with table `assistant_feedback`. The migration is squashed in Task 19.

**Interfaces:**
- Produces, in `rubik_core/identifiers.py`:

  ```python
  ADK_APP_NAME = "cubie"
  ADK_AGENT_NAME = "cubie_agent"
  ADK_COORDINATOR_NAME = "cubie_coordinator"
  NIGHTLY_APP_NAME = "cubie_nightly"
  IMPERSONATION_APP_NAME = "cubie_impersonation"
  CHROMA_COLLECTION = "cubie"
  CALL_ORIGIN_ASSISTANT = "cubie"
  SMART_TEAMS_ASSISTANT_SPEAKER = "cubie"
  WHATS_NEW_PRODUCT = "cubie"
  AUTO_FILTER_REVIEWER = "cubie:auto-filter"
  CRM_LOGGED_DETAIL_KEY = "cubie_logged"
  ASSET_TAG_GENERATED = "cubie-generated"
  AUDIT_VIA_ASSISTANT = "cubie"
  FATHOM_SKIP_HOST_OUTSIDE = "skipped_host_outside_workspace"
  ```

- [ ] **Step 1:** Write the failing test `backend/tests/test_identifiers.py`.
  - Import `rubik_core.identifiers`.
  - Assert that the call-capture default origin equals `CALL_ORIGIN_ASSISTANT`, and that the ADK orchestrator uses `ADK_APP_NAME`. Introspect the module constants they import.
  - Run; expected FAIL (module missing).
- [ ] **Step 2:** In the old package, replace each stored-value literal listed in `$MAPS/branding.md` (f) with the constant. These include `origin="<old>"`, speaker `"<old>"`, product `"<old>"`, `<old>:auto-filter`, `<old>_logged`, `<old>-generated`, `{"via":"<old>"}`, `skipped_host_not_<old>`, the ADK and nightly `app_name`s, and the Chroma collection. Mirror the TS literals: `smartTeams.ts` speaker and `rules.ts` reviewer.
- [ ] **Step 3:** Move the package with a script in `$SCRATCH` (the scratchpad, not the repo). The script does `git mv` and then ordered regex passes over `*.py, *.sh, *.ini, *.toml, Dockerfile, .gcloudignore, *.ts, *.tsx, *.md, *.yml, *.yaml, *.json`:
  1. the old package name, word-bounded → `rubik_core`
  2. the old package path prefix → `rubik_core/`
  3. the old retrieval-chain class name, word-bounded → `KnowledgeChain`
- [ ] **Step 4:** Run `ruff check --fix . && ruff format .`, then the full verification. Expected: green, and a grep for the old package name (excluding `.git`, `node_modules` and `.venv`) prints nothing.
- [ ] **Step 5:** Commit: `refactor: move package to rubik_core and freeze stored identifiers`.

### Task 10: Brand constants (backend + frontend)

**Files:**
- Create: `rubik_core/brand.py`, `frontend/src/brand/brand.ts`, `frontend/src/brand/assets/cube.svg`, `frontend/public/cubie.svg` (favicon).

**Interfaces:**
- Produces, in `rubik_core/brand.py`:
  - `PRODUCT_NAME = "Cubie"`, `PRODUCT_SLUG = "cubie"`, `VENDOR_NAME = "RubikSoft"`, `VENDOR_URL = "https://www.rubik-soft.com"`
  - `BRAND_HEX = "#7C3AED"`, `BRAND_HOVER_HEX = "#6D28D9"`, `INK_HEX = "#0E0729"`, `TINT_HEX = "#F2F0F7"`, `SOFT_HEX = "#D5CAFC"`, `ACCENT_HEX = "#A47BFF"`, `SUCCESS_HEX = "#03553E"`
  - `DISPLAY_FONT = "Unbounded"`, `BODY_FONT = "Montserrat"`
- Produces, in `brand.ts`: the same names in camel/upper case (`PRODUCT_NAME`, `VENDOR_NAME`, `VENDOR_URL`) and `cubeMark` (the imported SVG URL).
- **Cube icon source:** artboard "ICONO" of the locally approved RubikSoft logo pack.
  - Export: `pdftocairo -svg -f N -l N "<ai>" out.svg` for the artboard that shows only the icon. Find it by rendering pages to PNG.
  - Simplify the SVG to its paths with a `viewBox`, a `currentColor`-friendly fill and no embedded raster.

- [ ] **Step 1:** Write the failing test `backend/tests/test_brand.py`. It asserts `PRODUCT_NAME == "Cubie"`, that every hex matches `^#[0-9A-F]{6}$`, and that `frontend/public/cubie.svg` exists, starts with `<svg`, and has no `<image`. Run; expected FAIL.
- [ ] **Step 2:** Create the files and export the icon.
- [ ] **Step 3:** Run the test; expected PASS. Then commit: `feat: add Cubie brand constants and cube mark`.

### Task 11: Open access by default and strip people rosters

**Files:**
- Modify:
  - `rubik_core/features/registry.py`: delete every email roster and every default that names the old company's departments. User-facing features default to everyone. Admin, manager and exec features keep their role-based defaults. `copilot` defaults to everyone.
  - `backend/auth.py` `_apply_access_gates`: remove the "no department → 403" lockout. Keep the admin-set logic, but source it only from settings and the DB `admin_email`; there are no default emails.
  - `frontend/src/context/AuthContext.tsx`:
    - remove `SMART_TEAMS_RAW_DEPARTMENTS`, and the `department === 'sales'` auto-ingest. Gate the latter on a feature key: add `fathom_auto_ingest` to the registry, default off.
    - collapse `canAccess*` booleans into `hasFeature` where touched.
  - `frontend/src/App.tsx`: collapse the 11 guard components into one `FeatureRoute({feature, fallback = "/"})`. `FullAppRoute` falls back to `/`.
- Test: `backend/tests/test_feature_access.py`, `backend/tests/test_default_access.py` (create).

- [ ] **Step 1:** Write the failing tests in `test_default_access.py`:
  - `test_no_department_user_gets_core`: a mock user with department `""` gets `GET /api/users/me` → 200 and `features["copilot"] is True` (Review Focus 3).
  - `test_registry_has_no_email_rosters`: no `FeatureSpec` default references a string containing `@`.
  - `test_core_features_default_open`: for every spec whose group is user-facing (name the keys explicitly from the current registry: copilot, todos, scheduled_tasks, voice_input, voice_commands, chat_file_upload, page_tours, whats_new, open_commitments, insights, chrome_extension, meeting skills…), resolution for a no-department non-admin user is `True`.
  - Run; expected FAIL.
- [ ] **Step 2:** Implement the registry, auth and frontend changes. Update `test_feature_access.py` expectations that encoded the old rosters.
- [ ] **Step 3:** Run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `feat: core app open to everyone in the domain; drop people rosters`.

### Task 12: Company profile, catalog, proposal templates, call scripts, neutral taxonomy

**Files:**
- Create:
  - `rubik_core/company/__init__.py`, `rubik_core/company/profile.py`, `rubik_core/company/catalog.py`
  - `backend/routers/company.py` (mounted on the `core` parent, admin-only writes)
  - `frontend/src/api/company.ts`
  - `frontend/src/pages/admin/CompanyTab.tsx`, with three sections: Profile, Catalog, Proposal templates
  - help topic `frontend/src/pages/help/topics/CompanySetup.tsx` plus its section in `rubik_core/help/content.py`
  - a tour `admin.new.company-setup`
- Modify:
  - `rubik_core/database/models.py`: add `CatalogItem` and `ProposalTemplate`.
  - `rubik_core/proposals/templates.py`: delete the old service library and the sister letterhead; read `ProposalTemplate` rows.
  - `proposals/brand.py`: letterhead comes from the company profile and `rubik_core.brand`.
  - `retrieval/product_catalog.py`: reads `CatalogItem`.
  - `call_capture/scripts.py` and `database/call_scripts.py`: delete the old scripts and play keys; data comes from rows; empty by default.
  - `smart_teams/skill_taxonomy.py`: replace with a neutral starter taxonomy of at most 120 skills across five domains (business, sales and customer, operations, technology, people and leadership). Keep the public API and `seed_taxonomy` signature.
  - Every prompt that named the old company or its industry. Find them with `$MAPS/branding.md` (c), plus a grep for "mortgage", "compliance firm", "lender", "QC". Each one includes `company_context_block(get_company_profile(db))` and says "the company" when empty.
  - `adminSections.ts`: add a `company` tab under a new group "Workspace".
  - `/api/users/me`: add `workspace: {company_name: str}` (empty string when unset).

**Interfaces:**
- Produces, in `rubik_core/company/profile.py`:
  - `@dataclass(frozen=True) class CompanyProfile: name: str = ""; legal_name: str = ""; website: str = ""; address: str = ""; blurb: str = ""; industry: str = ""; offering: str = ""; customers: str = ""; sales_process: str = ""; products: tuple[str, ...] = ()`
  - `get_company_profile(db: Session | None = None) -> CompanyProfile` reads the `workspace_settings` key `"company_profile"`.
  - `save_company_profile(db: Session, profile: CompanyProfile) -> CompanyProfile`
  - `company_name_or_default(profile: CompanyProfile, default: str = "the company") -> str`
  - `company_context_block(profile: CompanyProfile) -> str` returns `""` when every field is empty, otherwise a short plain-text paragraph. It never contains "None".
- Produces, in `rubik_core/company/catalog.py`:
  - `CatalogRow` (frozen dataclass: `name, description, category, price: Decimal | None, unit, url`)
  - `parse_catalog_csv(text: str) -> tuple[list[CatalogRow], list[str]]` takes the header `name,description,category,price,unit,url`. Only `name` is required, and errors are `"row N: …"`.
  - `replace_catalog(db, rows) -> int`
  - `list_catalog(db) -> list[CatalogRow]`
- **API:**
  - `GET/PUT /api/company/profile`
  - `GET /api/company/catalog`, `POST /api/company/catalog/import` (text/csv body), which returns `{created, errors}`
  - `GET/POST/PUT/DELETE /api/company/proposal-templates[/{id}]`
  - `ProposalTemplate` has `{id, key, name, body_md, catalog_item_names: list[str], active: bool}`.

- [ ] **Step 1:** Write the failing tests:
  - `test_company_profile.py`:
    - empty profile gives `company_context_block == ""` and `company_name_or_default == "the company"`
    - a round-trip save and load
    - PUT is admin-only (403 for a non-admin)
  - `test_company_catalog.py`:
    - CSV with a missing `name` gives an error `"row 2: name is required"`
    - a bad price gives an error
    - import replaces the old rows
  - `test_prompts_neutral.py` (Review Focus 1): build the chat system prompt, X-Ray research prompt, call-capture extraction prompt, CRM Sync prompt and proposal prompt with an empty profile. Assert each contains none of `None`, `{company}`, `mortgage`, `lender`, and contains `the company` or omits company wording. Build them again with `CompanyProfile(name="Acme Corp", industry="Industrial cleaning")` and assert `"Acme Corp"` appears.
  - `test_proposal_templates.py` is rewritten: no templates means the proposal is built with AI sections only and no prices in prose; one template plus 2 catalog rows means the priced table comes from the rows.
  - Run; expected FAIL.
- [ ] **Step 2:** Implement the backend (models, modules, router, prompt rewiring, taxonomy, call scripts).
- [ ] **Step 3:** Implement the frontend: `CompanyTab`, with empty states that link to each section, CSV upload showing per-row errors, and a simple template editor (name, body textarea, catalog picker). Add the help topic in both homes and the tour with `data-tour` anchors.
- [ ] **Step 4:** Run the full verification. Expected: green.
- [ ] **Step 5:** Commit: `feat: per-company profile, catalog and proposal templates; neutral prompts`.

### Task 13: Externalise the remaining per-company config; rewrite test fixtures

**Files:**
- Modify: `rubik_core/config.py`. For every org-specific field in `$MAPS/tenancy_identity.md` §8:
  - Set the default to empty (`""`, `[]` or `None`).
  - Delete the backstop that forces an admin email and domain.
  - Delete `ALLOWED_ORIGINS` entries for old hosts; local dev keeps `http://localhost:5173`.
  - Set the time-zone defaults to `"UTC"`.
  - HubSpot: portal id, pipeline ids and custom property names default to empty. The code path treats "no pipeline allow-list" as "all pipelines".
  - Delete the hard-coded RAG corpus path (`adk_agent/tools.py`) and the GCP project fallbacks (`home.py`, `xray.py`, `jobs/launcher.py`, `jobs/__init__.py`). A missing project means the operation raises a clear "GCP_PROJECT_ID not set" error.
- Modify: `backend/tests/conftest.py`. Pin `GOOGLE_WORKSPACE_DOMAIN` to `acme.example` and rewrite its docstring. Then run a scratch script (kept in `$SCRATCH`) over `backend/tests/**` and `frontend/src/**/*.test.ts`:
  - `@<old-domain>` → `@acme.example`
  - the old client probe domain → `outside.example`
  - real staff first names and handles (listed in `$MAPS/files_docs.md` "Personal data") → fictional ones (`alex`, `blair`, `casey`, `devon`, `emery`, `finley`, `gray`, `harper`…). Keep a one-to-one mapping so test meaning is kept.
  - test function names containing old brand words → neutral names.
- Test: `backend/tests/test_config_defaults.py` (create).

- [ ] **Step 1:** Write the failing tests in `test_config_defaults.py`:
  - a fresh `Settings()` with an empty env has `ADMIN_EMAILS == []`, `GOOGLE_WORKSPACE_DOMAIN in (None, "")`, `HUBSPOT_PORTAL_ID in (None, "")` and `INTERNAL_EMAIL_DOMAINS == ""`;
  - no field's default contains `@` or `.run.app`.
  - Run; expected FAIL.
- [ ] **Step 2:** Implement the config and code-path changes, then the fixture rewrite.
- [ ] **Step 3:** Run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `refactor: no company values in config; fixtures use acme.example`.

### Task 14: Rename env vars, infra names, storage keys and extension protocol

**Files:**
- Modify:
  - Every setting and env var with the old product-name prefix → `CUBIE_*`, in config, code, deploy scripts, `.env.example`, `/health` markers (`CUBIE_MIGRATION_*`), and tests.
  - Infra names in `scripts/**`, `Dockerfile`, `.github/**` and `.claude/skills/deploy/**`:
    - service `cubie`, jobs `cubie-worker`, `cubie-priorities-batch`, `cubie-deep-prep`, `cubie-xray-deep`
    - log metrics `cubie_*`, edited together with `test_monitoring_filters.py`
    - project, bucket, SQL instance and SA values become variables with no defaults
    - Phase 2 reshapes these scripts; this task only removes old values.
  - Browser storage keys and events (`$MAPS/branding.md` (f)) → `cubie.*` / `cubie:*` / `cubie_*`.
  - Extension message protocol: `cubie-web`, `cubie-for-chrome`, `cubie-for-chrome:connect(ed)`, changed in both the page and `chrome_extension/src/bridge.js`.
  - Chrome extension: manifest name "Cubie for Chrome", keyword `cubie`, `host_permissions` `http://localhost:5173/*` plus `${CUBIE_PUBLIC_ORIGIN}`, written by `scripts/pack_chrome_extension.py --origin`. Icons are regenerated from `frontend/public/cubie.svg` with `rsvg-convert`, or `qlmanage` if absent, at 16/32/48/128.
  - Meet add-on docs and `deployment.json`: name "Cubie", URLs `https://<CUBIE_PUBLIC_HOST>/…` placeholders.
  - Slack manifest: name "Cubie", description "Cubie by RubikSoft".
  - The Deploy-freeze guard: `guard_not_during_et_l10` → `guard_not_during_freeze`. It reads `CUBIE_DEPLOY_FREEZE` (format `"DOW HH:MM-HH:MM TZ"`, empty means no freeze); Phase 2 replaces the source with the control database. `test_deploy_guards.py` is updated: an empty value never blocks, and a set value blocks inside the window and in the 20 minutes before it.
- Test: `frontend/src/brand/storage.test.ts` (create).

- [ ] **Step 1:** Write the failing tests:
  - `storage.test.ts` (Review Focus 5): with a fake `localStorage` holding only old-named keys, `readTourProgress()` and `readChatStreamingPref()` return defaults and do not throw.
  - `test_env_names.py`: no `Settings` field name starts with the old product-name prefix.
  - Run; expected FAIL.
- [ ] **Step 2:** Implement the renames.
- [ ] **Step 3:** Run the full verification, plus `bash -n scripts/*.sh scripts/lib/*.sh`. Expected: green.
- [ ] **Step 4:** Commit: `refactor: Cubie env vars, infra names, storage keys and extension protocol`.

### Task 15: Brand tokens, fonts and logo across the frontend and generated files

**Files:**
- Modify:
  - `frontend/package.json`: add `@fontsource-variable/unbounded` and `@fontsource-variable/montserrat`. Import them in `main.tsx` and the Meet entry.
  - `frontend/src/index.css`: drop the Inter/Manrope `@import`. Define `:root` RGB-channel variables: `--brand 124 58 237`, `--brand-hover 109 40 217`, `--brand-tint 242 240 247`, `--brand-soft 213 202 252`, `--accent 164 123 255`, `--rail 14 7 41`, `--rail-active 124 58 237`, `--ink 14 7 41`, `--success-brand 3 85 62`, `--font-display`, `--font-body`.
  - `frontend/tailwind.config.js`: add `colors.brand.{DEFAULT,hover,tint,soft}`, `accent`, `rail.{DEFAULT,active}`, `ink`, mapped as `rgb(var(--x) / <alpha-value>)`. `fontFamily.sans` = body and `display` = display. Remove the old company-named and unused `primary` tokens.
  - A codemod script in `$SCRATCH` over `frontend/src/**/*.{ts,tsx,css}`:
    - `[<maroon>]` → `brand`
    - `[<maroon-dark>]` and `[<maroon-darker>]` → `brand-hover`
    - `[<maroon-tint>]` → `brand-tint`
    - `<abbrev>-hover` → `brand-hover`, `<abbrev>-tint` → `brand-tint`, `(?<=[-:])<abbrev>` → `brand`
    - JS hex literals (`'<maroon>'`) → `getBrandColor('brand')` from `src/brand/colors.ts`
    - `red-700/800/900` uses that are brand rather than error → `brand`. These are reviewed by hand: errors stay red.
  - `Layout.tsx`: the rail is `bg-rail`, the active item `bg-rail-active`, and the logo is `cubeMark` plus a "Cubie" wordmark in `font-display`.
  - `Login.tsx`: a "Cubie by RubikSoft" lockup and the text "Welcome to Cubie".
  - `index.html` and `meet-addon.html`: titles "Cubie" / "Cubie call checklist", favicon `/cubie.svg`.
  - Delete `public/<old>*.{svg,png}` and `src/assets/<old-logo>.svg`.
  - `chrome_extension/styles.css`: `--accent: #7C3AED`.
  - Backend generators (`deep_prep/pdf.py`, `smart_teams/guide_pdf.py`, `proposals/render.py`, `ingestion_council/report.py`, `scorecard_xlsx.py`, `vto_xlsx.py`, `l10_summary_email.py`): colours from `rubik_core.brand`. Download filenames become `Cubie_Scorecard_<…>_template.xlsx` and `Cubie_VTO_template.xlsx`.
- Create: `frontend/src/brand/colors.ts` (`getBrandColor(name: 'brand'|'brandHover'|'tint'|'accent'|'ink'): string` reads the CSS variable and returns `rgb(...)`), `frontend/src/brand/colors.test.ts`.

- [ ] **Step 1:** Write the failing tests:
  - `colors.test.ts`: `getBrandColor('brand')` with a stubbed `getComputedStyle` returning `"124 58 237"` gives `"rgb(124 58 237)"`, and with an unset variable falls back to `"rgb(124 58 237)"`.
  - `test_generated_filenames.py` (Review Focus 4): the scorecard and VTO template filename functions return names starting `Cubie_` and matching no banned term (reuse `LEGACY_PATTERN` from Task 1).
  - Run; expected FAIL.
- [ ] **Step 2:** Implement and run the codemod. Review the red-* list by hand.
- [ ] **Step 3:** Run the full verification. Expected: green. Also grep `frontend/src` for the old abbreviation and the old maroon, which must print nothing.
- [ ] **Step 4:** Commit: `feat: RubikSoft brand tokens, fonts and logo across Cubie`.

### Task 16: Rewrite copy — frontend UI, tours and help

**Files:**
- Modify `frontend/src/**`, excluding tests until the end. These are split into disjoint slices that can run in parallel:
  - (a) `pages/help/**`
  - (b) `tours/**`
  - (c) `components/**`
  - (d) `pages/**` except help
  - (e) `meetAddon/**`, `api/**`, `context/**`, `utils/**`, plus `public/chrome-privacy.html` and `chrome_extension/**` copy
- **Rules:**
  - The product name → "Cubie" ("Ask <old>" → "Ask Cubie").
  - Old company name → "your company" in static copy, or `workspace.company_name || 'your company'` where the UI already has the user object (Task 12 field).
  - Old staff names in copy → generic roles.
  - Help topic ids and slugs are renamed: `cubie-for-chrome`, `what-cubie-reads`, `cubie-heard-you-promise`, `cubie-how-to`, `cubie-whos-on-the-call`. Rename the component file `<Old>ForChrome.tsx` → `CubieForChrome.tsx` and the `<Old>ForChrome*` symbols, plus `ask<Old>*`/`<old>Link` → `askCubie*`/`cubieLink`.
  - `tours.test.ts`: the banned copy regex adds the old product and company names, and the test is renamed "says Cubie and avoids engineering words".
- Modify `rubik_core/help/content.py` and `help/_retrieval.py`: the backend help mirror gets the same copy and slugs. Drop the old-name stopwords and the "called X, never Y" sanitiser. Rename `search_<old>_help` → `search_cubie_help` and update its tests.

- [ ] **Step 1:** Write the failing tests:
  - Update `tours.test.ts` and `helpTopics.test.ts` banned patterns.
  - `test_help_chat_link.py` asserts the renamed slugs.
  - Run; expected FAIL.
- [ ] **Step 2:** Rewrite the copy (slices a–e in parallel; each slice touches only its own files).
- [ ] **Step 3:** Run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `feat: Cubie copy across UI, tours and help`.

### Task 17: Rewrite copy — backend prompts, emails, Slack, and code comments

**Files:**
- Modify `rubik_core/**` and `backend/**`. These are split into disjoint slices that can run in parallel:
  - (a) chat runtime: `adk_agent/**`, `agent_runtime/**`, `orchestration/**`
  - (b) `priorities/**`, `backend/agents/**`, `backend/signals/**`
  - (c) `smart_teams/**`, `insights/**`, `whats_new/**`, `meeting_skills/**`, `meeting_prep/**`, `deep_prep/**`
  - (d) `call_capture/**`, `crm_sync/**`, `loop_closure/**`, `proposals/**`, `xray/**`, `cockpit/**`, `sales/**`, `graph/**`
  - (e) every remaining module, plus `backend/routers/**`, `backend/services/**` and `backend/jobs/**`
- **Rules:**
  - Prompts say `{PRODUCT_NAME}`, imported from `rubik_core.brand`, and read the company from the profile (Task 12).
  - Outbound email and Slack copy use `PRODUCT_NAME`.
  - Comments and docstrings that cite the old company, its staff or its clients are rewritten neutrally or dropped.
  - The old assistant name goes, including `eos/Meetings.tsx`.
  - Real-person emails in code (for example `Chat.tsx` `DEBUG_USER_EMAILS` and `whats_new/settings.py` approvers) are deleted. Debug access becomes the existing admin role.
- Test: reuse `test_prompts_neutral.py` (Task 12) and `test_no_legacy_brand.py`.

- [ ] **Step 1:** Run `scan_repo` limited to `rubik_core/` and `backend/` (excluding tests) and save the hit list to `$SCRATCH/copy_hits.txt`. Expected: hits remain.
- [ ] **Step 2:** Rewrite slices a–e in parallel.
- [ ] **Step 3:** Run `scan_repo` again on those trees. Expected: 0 hits. Then run the full verification. Expected: green.
- [ ] **Step 4:** Commit: `feat: Cubie copy across prompts, emails and Slack; scrub comments`.

### Task 18: Docs, project rules and repo meta

**Files:**
- Rewrite:
  - `README.md` (Cubie by RubikSoft: what it is, local quickstart, architecture, the feature list harvested from the old capabilities doc)
  - `CLAUDE.md` (Cubie project rules; the freeze rule is per-workspace setting text; point to `docs/ENGINEERING_NOTES.md`)
  - `.env.example`
  - `.claude/skills/deploy/**` (Cubie wording; real targets arrive in Phase 2)
  - `.github/workflows/*.yml` (drop the old-file exemptions; image name `cubie`; add the frontend `npm ci && npm test && npm run build` job)
  - `frontend/README.md` (short)
- Rebrand the kept docs per `$MAPS/files_docs.md` "KEEP+REBRAND", and delete the rest. Rename files under `docs/` whose names carry banned words.
- Scrub `docs/superpowers/**` too. The spec and plans quote old names. Reword them as "the old company" / "the old product name" so the scan passes.

- [ ] **Step 1:** Run `scan_repo` limited to `docs/`, `*.md`, `.github/` and `.claude/`. Expected: hits.
- [ ] **Step 2:** Rewrite and delete.
- [ ] **Step 3:** Run `scan_repo` on those paths again. Expected: 0. Then commit: `docs: Cubie README, project rules and engineering notes`.

### Task 19: Squash migrations into one baseline and prove it

**Files:**
- Delete: `alembic/versions/*`.
- Create:
  - `alembic/versions/0001_cubie_baseline.py`
  - `rubik_core/database/seed.py`, with `seed_workspace(db: Session) -> None`, which is idempotent: the default priority playbook, Drive label priorities, SLA placeholders and the neutral skills taxonomy. It is called by the local dev setup and by tests that need defaults.
  - `scripts/dev/schema_diff.sh`, which builds two throwaway Postgres databases with `/usr/local/bin/initdb` + `pg_ctl` on port 55432 in `$SCRATCH/pg` and diffs `pg_dump --schema-only`.
- Modify: add `fathom_call_sync_logs` as a model if code still uses it; otherwise drop it.

- [ ] **Step 1:** Write the failing tests:
  - `test_migrations_baseline.py`: `alembic heads` has exactly one head, `0001_cubie_baseline`, and `upgrade head` on SQLite gives the same table set as `Base.metadata.sorted_tables`.
  - `test_seed_workspace.py`: running `seed_workspace` twice leaves the row counts unchanged.
  - Run; expected FAIL.
- [ ] **Step 2:** Build reference database A on Postgres from the **pre-squash** chain at HEAD of Task 18, then drop the tables of deleted modules if any remain. Autogenerate the baseline from `Base.metadata` against an empty Postgres. Hand-check enums, partial indexes and CHECK constraints. Build database B from the baseline.
- [ ] **Step 3:** Run `scripts/dev/schema_diff.sh A B`. Expected: no differences other than the intended renames (`assistant_feedback`) and removed tables. Record the diff output in the commit message body.
- [ ] **Step 4:** Run the full verification, including `alembic upgrade head` on SQLite. Expected: green.
- [ ] **Step 5:** Commit: `refactor: collapse migrations into a single Cubie baseline`.

### Task 20: Zero trace — flip the guard

**Files:**
- Modify:
  - `backend/tests/test_no_legacy_brand.py`: remove the `xfail` and add `test_frontend_dist_has_no_legacy_brand`, which builds the frontend if `dist/` is missing and scans `frontend/dist/**` (Review Focus 4).
  - `.github/workflows/python-app.yml`: the scan runs in CI with the rest of the suite.

- [ ] **Step 1:** Remove the `xfail` and run the test. Expected: it fails with the remaining hits listed.
- [ ] **Step 2:** Fix every remaining hit. Binary assets with old branding are deleted or replaced.
- [ ] **Step 3:** Run the full verification and `npm run build`, then run both scan tests. Expected: all green, with lint counts at or below baseline.
- [ ] **Step 4:** Commit: `test: legacy-brand scan is a hard gate`.

---

## Self-review notes

- **Spec coverage:**
  - §4.1 brand → Tasks 10, 15
  - §4.2.1 order → task order
  - §4.2.2 lock-in → Tasks 1 and 20; git re-init is end of Phase 3
  - §4.2.3 deletions → Tasks 2–7
  - §4.2.4 files → Tasks 8 and 18
  - §4.2.5 industry content → Task 12
  - §4.2.6 migrations → Task 19
  - §4.3.4 default access → Task 11
  - §4.6.1 changelog reader seam → Task 7
- **Deferred to Phase 2/3 plans:** workspaces, scripts, connectors, wizard, Workspace profile and the changelog authoring.
- **Parallelism:** Tasks 16 and 17 fan out by disjoint file slices. Every other task is sequential, because the tasks share `main.py`, the registry, App/Layout, help and tours.
