"""My To-Dos — the promises ledger (user-facing name: To-Dos).

Design: ``docs/todos_promises_design.md``. Phase 0 modules:

- ``store``         — the only writer of ``promises`` / ``promise_events``.
- ``readthrough``   — the user's EOS To-Dos and Sales Cockpit commitments,
                      shown on the same page WITHOUT a second copy.
- ``parse``         — one cheap Flash call that turns "send Jordan the SOW
                      Thursday" into title / who / when.
- ``priority_sync`` — keeps the AI Priorities card in step with the row.
"""
