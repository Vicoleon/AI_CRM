"""Offboard handoff — who inherits a departed teammate's open work, and how
Cubie keeps following up on it.

Three moving parts:

- :mod:`rubik_core.offboarding.store` — the ``offboard_records`` /
  ``offboard_items`` persistence: create a record when an admin offboards a
  user, query open items for a successor, resolve items.
- :mod:`rubik_core.offboarding.sweep` — the exit sweep (a cubie-worker task):
  inventory everything open the departed person owned (EOS, HubSpot, recent
  priorities-history commitments, and — while their Google account is still
  impersonable — a last pass over Gmail/Calendar/Drive), synthesize the
  handoff dossier, and persist the items.
- ``InheritedChannelAgent`` (:mod:`rubik_core.priorities.agents.inherited_agent`)
  — feeds open items into the successor's daily AI Priorities until each one
  is marked done or dismissed.

Raw mailbox/Drive/calendar access is deliberately NOT proxied here: Google
Workspace's own transfer flow hands those artifacts to the manager, and the
items link out to them. See docs/offboarding_handoff.md for the operational
sequencing (run the offboard BEFORE IT suspends the Google account).
"""
