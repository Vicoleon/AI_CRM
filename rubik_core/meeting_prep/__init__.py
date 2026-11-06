"""Meeting Prep: versioned, editable briefs and the signals they are built from.

The generation pipeline itself still lives in ``backend/routers/home.py``
(``collect_meeting_prep_context`` → ``generate_meeting_prep_analysis``). This
package holds what sits around it:

* ``sections``  — the registry of brief sections a person can edit or have
  Cubie rewrite, with their labels and what each one is for.
* ``versions``  — the per-meeting version chain on ``meeting_prep_runs``.
* ``rewrite``   — the one-section rewrite worker.
* ``signals``   — the newer data sources (CRM, X-Ray, promises, time off…).
* ``background`` — building a brief off the request (202 + a notification
  when it is ready): the pending row, dedupe, TTL, the runner and the worker.
"""
