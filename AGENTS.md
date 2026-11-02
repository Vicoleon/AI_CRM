# Cubie by RubikSoft (package `rubik_core`) — project rules for Codex

Read `docs/ENGINEERING_NOTES.md` for the full engineering notes; the rules
below are the ones that must survive every session.

## Feature gating: who may reach a feature is DATA, never a hard-coded roster

Admins onboard departments to features in **Admin → Feature Access**
(`feature_access` table, resolved by `rubik_core/features`). A new page, tab,
router or surface with a department-scoped audience MUST, in the same PR:

1. Register a `FeatureSpec` in `rubik_core/features/registry.py::FEATURES`
   (key, label, business-outcome description, where it shows up, the roster it
   ships with, `membership`, optional env `kill_switch`, admin `notes`). A
   default never names a department or a person: user-facing features ship
   open to everyone in the workspace, manager and admin views ship role-scoped.
2. Gate the backend with `feature_allows("<key>", user)` from
   `rubik_core.features`. No `frozenset({...})` of departments, no
   `settings.*_DEPARTMENTS` compare, no inline `dept in {...}` — CI
   (`backend/tests/test_feature_access.py`) fails on a roster literal in
   `backend/auth.py`.
3. Add the key to `FEATURE` in `frontend/src/context/AuthContext.tsx` and gate
   the nav/route on `features.<key>` from `/api/users/me` — never on a
   department string in the client.

Env kill switches (`COCKPIT_ENABLED` …) decide whether a feature RUNS AT ALL
and stay in `rubik_core/config.py`; the registry decides WHO may reach it once
it does. Structural rules that are not rosters (the Smart Teams
Workspace-domain check, per-row transcript confidentiality) stay in code.

## NEVER deploy inside a declared deploy freeze

A deploy restarts the service, so a team that runs a live meeting in Cubie (an
L10, say) can declare a weekly window in which no deploy may start. The
window lives in that workspace's control setting. No deploy of any kind
(service, prod, a Job, a job re-sync) inside that window, nor in the 20 minutes
before it.
Writing code, opening and merging PRs is fine. Only the deploy waits.

`scripts/platform/deploy.sh --env <private-file> [--dry-run]` is the release
entrypoint. It checks all workspace windows and the 20-minute lead with
`scripts/lib/deploy_guards.sh` (exit 7). A rollback is a deploy too. Codex
never sets `DEPLOY_FREEZE_OVERRIDE_REASON` and never works around the guard
(e.g. a raw `gcloud run deploy`). Break-glass is Jose typing it by hand. When a
fix is ready inside the window, say so and give the time the window opens.

`scripts/platform/bootstrap_gcp.sh --env <private-file> --dry-run --fresh-install`
is for a new platform only; `--fresh-install` permits its first control
baseline. Normal deploys never create or repair control tables.
`scripts/platform/workspace.sh` is the explicit workspace operator CLI.

## Other standing rules (details in docs/ENGINEERING_NOTES.md)

- Every shipped change updates Help in BOTH homes: the topic's page in
  `frontend/src/pages/help/topics/<Topic>.tsx` (a new topic also gets a row in
  `frontend/src/pages/help/helpTopics.ts` and `topicPages.tsx`) and its
  section in `rubik_core/help/content.py`.
- A user-visible change to a page ships its page tour in the same PR: a `new`
  tour in `frontend/src/tours/content/<area>.ts` (id `<page>.new.<feature>`,
  `since` = the ship date) and the page's intro updated to cover it; a new page
  gets an intro. Anchor cards with `data-tour="…"`; `src/tours/tours.test.ts`
  fails on a card or anchor that drifted.
- Every new AI feature gets its own AI Cost category (`@attribute_to` on the
  worker + a `_FEATURE_LABELS` entry) — never ship into "Unattributed".
- New copilot routers mount on the `core` parent in `backend/main.py`.
- Compare departments by RAG slug (`slugify_department`), never raw `==`.
- Alembic: the chain starts at `0001_cubie_baseline`; a new migration takes
  the next number off `origin/main`'s head (`--rev-id 000N_short_name`, at most
  32 characters) after checking every worktree; CI requires a single head.
  Default rows go in `rubik_core/database/seed.py`, never in a migration.
- Run `ruff check --fix . && ruff format .` before pushing Python.
- The app is "Cubie" to users. Runtime strings take the name from
  `PRODUCT_NAME` (`rubik_core/brand.py`, `frontend/src/brand/brand.ts`); the
  company a prompt or screen talks about comes from the workspace's company
  profile, with "your company" when none is saved.
- No trace of the previous product, company, clients or staff anywhere in the
  repo. `backend/tests/test_no_legacy_brand.py` scans every tracked file and
  the built frontend, and fails CI on a hit.
