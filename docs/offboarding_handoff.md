# Offboard Handoff — who inherits a departed teammate's work, and how Cubie follows up

**Status:** live.

## What it does

When an admin offboards a user (Administration → Users → row menu → **Offboard…**):

1. A durable `offboard_records` row snapshots who left, their department, the
   **successor** (prefilled from the Accountability Chart boss), and who ran it.
2. Their uploaded docs (`assets`) are reassigned to the successor — the
   "docs go to the manager, never deleted" convention, now enforced in code.
3. The `users` row is deleted — same convention as before (every module reads
   "no users row" as departed). Config rows (roles, groups, policies,
   preferences, Slack credentials, priority memory) cascade away; **history
   rows survive** (home_cards, chat, feedback, recommendations — they carry no
   foreign key to `users`).
4. A **cubie-worker task** (`backend/jobs/offboard_sweep_job.py`) runs the exit
   sweep and writes the **handoff dossier** + `offboard_items` checklist.
5. Open items feed the successor's daily AI Priorities via the `inherited`
   channel until each is done/dismissed — from the Handoffs page or the
   priority card itself (both write the same status).

## The exit sweep (rubik_core/offboarding/sweep.py)

| Source | What it captures | Works after Google suspension? |
|---|---|---|
| EOS | L10s they managed, pending to-dos, open rocks + milestones, scorecard measurables | ✅ |
| HubSpot | open/at-risk deals they owned | ✅ |
| Priorities history | unresolved commitment-shaped priorities from their last 14 days of bundles | ✅ |
| Google last pass | one final Gmail/Calendar/Drive read to extract open commitments | ❌ — DWD impersonation of a suspended account fails |

One LLM call (cost category **offboard_handoff**) writes the narrative and may
extract up to 8 extra commitments from the raw Google signals; everything else
is deterministic and ships even with no LLM.

## ⚠️ Operational sequencing — put Cubie FIRST in the IT checklist

Google refuses impersonation of suspended accounts. If IT suspends before the
Cubie offboard runs, the sweep degrades to DB+HubSpot-only (`sweep_status =
"degraded"`, and the dossier says so). The right order:

1. **Cubie: Administration → Users → Offboard** (sweep runs within minutes)
2. Google Workspace: suspend / transfer Drive / set forwarding (as today)
3. Slack deactivation, etc.

Already suspended? Offboard anyway — you still get EOS/HubSpot/history — and
use **resweep** (`POST /api/offboarding/records/{id}/resweep`) if the account
is ever temporarily re-enabled.

## Access model

- **Offboard action:** admin only. Bare `DELETE /api/users/{email}` now 409s
  for anyone with real usage (use `?force=true` for junk accounts).
- **Dossier / checklist / departed person's past priorities:** the record's
  successor, org-wide exec (`is_exec` + `managed_departments: ["*"]`, e.g. the CEO),
  or admin. Departed-priorities reads are audit-logged
  (`DEPARTED_PRIORITIES_VIEW`).
- Raw mailbox/Drive/calendar access is **Google Workspace's job** (data
  transfer / forwarding). Cubie links out to inherited artifacts; it never
  proxies a dead mailbox.

## Read-filter poisoning guard

Inherited cards are entity-keyed to the `offboard_items.id`
(`entity_kind="offboard_item"`), never to the underlying thread/deal — so
resolving one can't blacklist the successor's OWN future priorities on that
entity.

## Follow-ups / not in v1

- Chat over the dossier ("what did John Doe leave open with Acme Corp?") — the
  Handoffs page holds the content; a chat tool is a later slice.
- Slack notification to the successor when a dossier lands (Slack outbound is
  UC-15, still read-side only).
- Auto-suggest re-pointing L10 manager roles (breakglass flow exists on the
  Meetings page).
