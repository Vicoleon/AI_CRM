"""Loose Ends — the AE loop-closure scan (``docs/loose_ends/README.md``).

Every weekday morning one central scan (a cubie-worker run, not a per-AE
assistant) reads each rostered AE's own Gmail and Slack, reconciles what it
finds against HubSpot, and keeps a durable list of loose ends per AE in
``loop_items``: client emails that never reached HubSpot, client emails still
waiting on a reply, and promises / HubSpot tasks past their date.

Module map (README §4):

* ``constants`` — vocabularies, labels, ``thresholds()`` from settings.
* ``roster`` — who is scanned (the ``loop_closure`` Feature Access roster).
* ``gmail_reader`` — per-AE thread gather, metadata only.
* ``hubspot_index`` — one HubSpot index per run, shared by every AE.
* ``detect`` — pure detection and closure rules.
* ``judge`` — the one Flash call per AE (cost category ``loop_closure``).
* ``store`` — the ONLY writer of ``loop_items`` / ``loop_mutes``.
* ``scan`` — ``run_scan``, the orchestration.
* ``digest`` — the daily channel digest and its send-once guard.
* ``scorecard`` — the weekly per-AE scorecard upsert and ``setup_metrics``.
* ``views`` — the read models for the router and the chat tool.
* ``drafts`` / ``crm_log`` — the two click actions ("Draft a reply",
  "Log it in HubSpot"), which run in the request, never in the scan.

Nothing is imported here on purpose: the router and the chat tool import the
one module they need, and none of them should pay for the scan's imports.
"""
