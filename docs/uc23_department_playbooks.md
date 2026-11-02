UC-23 — Department playbooks (admin-managed, multi-per-department)
==================================================================

Status: SCOPING
Depends on: UC-20 (multi-agent pipeline), UC-21 (admin UI)
Blocks:    UC-24 (correlation framework consumes playbooks)

Problem
-------

UC-20's decision-maker prompt is Sales-shaped: "external > internal",
"deal silence > 5d", etc. Rolling the AI Priorities feature to Marketing,
Operations, Finance, or any other department in the ``departments``
table requires a per-department description of *what matters in this
role*. Within one department (e.g. Ops), several sub-roles will need
distinct playbooks — Vendor Mgmt cares about SLA misses; Facilities
cares about lease dates; Production cares about incident MTTR.

These playbooks must evolve. Super-users need to author and refine them
from the admin UI without code changes.

Scope
-----

Build a DB-backed, admin-managed playbook system that the UC-24
decision-maker consumes at runtime.

A playbook is a free-form markdown blob that describes:

- High-value entities for this role
- What "matters" / "doesn't matter"
- Bias rules (external > internal, etc.)
- Anything else useful for the decision-maker's prompt

Multiple playbooks per department are supported. At pipeline time the
decision-maker concatenates **all active playbooks attached to the
user's department** plus the default playbook (if any). Admins can
mark playbooks active/inactive and reorder them.

Out of scope
------------

- Per-user playbooks. Resolution stays at the department level.
- A WYSIWYG markdown editor. Plain ``<textarea>`` with monospace font
  is fine for v1; admins are technical.
- Versioning / diff history beyond ``updated_at``. Audit trail rows
  can come later if the team starts stepping on each other's edits.

Data model
----------

New table ``priority_playbook``::

    CREATE TABLE priority_playbook (
        id              TEXT PRIMARY KEY,            -- uuid
        name            TEXT NOT NULL,               -- "Vendor management v2", "Q4 push"
        department      TEXT NULL,                   -- FK→departments.name; NULL = default for all
        body            TEXT NOT NULL,               -- markdown
        is_active       BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order      INTEGER NOT NULL DEFAULT 100,-- lower = injected earlier
        created_by      TEXT NOT NULL,               -- user email
        updated_by      TEXT NOT NULL,
        created_at      TIMESTAMP NOT NULL,
        updated_at      TIMESTAMP NOT NULL
    );
    CREATE INDEX idx_priority_playbook_dept_active
      ON priority_playbook (department, is_active);

Department list comes from the existing ``departments`` table — no
hardcoded enum. The admin UI fetches ``GET /api/departments`` to
populate the department dropdown when authoring a playbook.

``department = NULL`` is the magic "default for everyone" playbook. The
resolution rule: ``effective_playbooks(user) = all active rows where
department = user.department UNION all active rows where department IS
NULL``, sorted by ``sort_order`` ascending.

API
---

All endpoints admin-gated via ``admin_required``. Live under
``/api/admin/priorities/playbooks``.

``GET /api/admin/priorities/playbooks``
  List all playbooks. Returns ``[{id, name, department, is_active,
  sort_order, updated_by, updated_at, body_preview}]``. Bodies are
  truncated to 200 chars in the list view.

``GET /api/admin/priorities/playbooks/{id}``
  Full playbook including ``body``.

``POST /api/admin/priorities/playbooks``
  Create. Body: ``{name, department, body, is_active?, sort_order?}``.

``PUT /api/admin/priorities/playbooks/{id}``
  Partial update.

``DELETE /api/admin/priorities/playbooks/{id}``
  Hard delete. (Soft-delete via ``is_active=false`` is the recommended
  path; ``DELETE`` is for genuinely abandoned drafts.)

``GET /api/admin/priorities/playbooks/effective?department=Ops``
  Preview the concatenation the decision-maker will see for that
  department — useful for super-users to sanity-check their edits.
  Returns ``{department, playbooks[{id, name, sort_order, body}],
  concatenated_text}``.

Admin UI changes
----------------

Extend the existing **Admin → AI Priorities** tab (UC-21) with a new
"Playbooks" subsection below the agent/model grid.

UI shape::

    ┌─ Playbooks ──────────────────────────────────────────────────┐
    │  [+ New playbook]    Department: [All ▾]   ☐ Inactive only   │
    │                                                              │
    │  ┌──────────────────────────────────────────────────────┐    │
    │  │ Sales – default                          [Edit] [⋮]  │    │
    │  │ Department: Sales · Order 100 · Updated 2026-05-26   │    │
    │  │ "High-value entities: HubSpot deals in {Decision, …" │    │
    │  └──────────────────────────────────────────────────────┘    │
    │  ┌──────────────────────────────────────────────────────┐    │
    │  │ Ops – Vendor Mgmt                        [Edit] [⋮]  │    │
    │  │ Department: Operations · Order 100 · Updated …       │    │
    │  └──────────────────────────────────────────────────────┘    │
    │  ┌──────────────────────────────────────────────────────┐    │
    │  │ Default (all departments)                [Edit] [⋮]  │    │
    │  │ Department: — · Order 50 · Updated …                 │    │
    │  └──────────────────────────────────────────────────────┘    │
    └──────────────────────────────────────────────────────────────┘

Edit modal: name input, department dropdown (populated from
``/api/departments``), sort_order number, active toggle, body
``<textarea>`` (monospace, ~30 rows), [Preview] button that calls
``GET /api/admin/priorities/playbooks/effective`` to show what the
decision-maker would see.

Default seed playbooks
----------------------

Migration writes one row per existing department, body left empty for
super-user to fill, **plus** one "Default" row with ``department=NULL``
seeded with the 5-meta-rule explanation from UC-24 (so the system has
a workable starting point even before any department customizes).

Initial seeds the team should author after launch (super-user task,
not a migration concern):

- Sales – Pipeline focus
- Sales – Renewal/expansion focus
- Marketing – Campaign cadence
- Operations – Vendor management
- Operations – Facilities
- Operations – Production / incidents

Test plan
---------

- ``test_playbook_crud_admin_only`` — non-admin 403, admin CRUD round-trip
- ``test_effective_resolution_concatenates_dept_and_default``
- ``test_effective_resolution_respects_sort_order``
- ``test_inactive_playbooks_excluded``
- ``test_department_list_pulled_from_db`` — no hardcoded list

Migration / rollout
-------------------

1. Alembic migration creating the table + index.
2. Seed one default playbook row with the UC-24 meta-rules text.
3. Ship the API + admin UI behind the existing **AI Priorities** tab.
4. UC-24 consumes this; until UC-24 lands, the playbooks are
   inert (stored but not injected anywhere).

Open questions
--------------

- Should we offer a "fork" action (duplicate an existing playbook to
  start a new one)? Recommend: yes, trivial to add.
- Markdown rendering in the read view? Recommend: only in the
  ``effective`` preview, not in the list cards.

Estimate
--------

Small. Mostly CRUD + a textarea + an Alembic migration. ~3 hours.
