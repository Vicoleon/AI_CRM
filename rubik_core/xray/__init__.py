"""UC-45 Customer X-Ray — customer-relationship intelligence.

Domain + persistence for the Customer X-Ray feature (Fast X-Ray + Deep
Research). HTTP handlers live in ``backend/routers/xray.py``; the ORM tables
(``XraySnapshot`` / ``XrayTimelineEvent``) live in
``rubik_core/database/models.py``; the UI lives in ``frontend/``.

Package layout follows the house rule (docs/ENGINEERING_NOTES.md): ``rubik_core`` never imports
from ``backend``.

Modules
-------
* ``config``        — churn weights/bands, per-agent budgets, model routing, flags
* ``schemas``       — typed digests / factor / opportunity / bundle dataclasses
* ``fetchers``      — HubSpot pulls (delta-aware, with backoff the base client lacks)
* ``churn``         — deterministic weighted-factor churn score (0–100 + band)
* ``opportunities`` — whitespace against the workspace's product catalog
* ``checkpoint``    — load-latest, cursor management, timeline dedupe/append, carry-forward
* ``persistence``   — snapshot version writes + batched timeline inserts + read helpers
* ``prompts``       — system instructions + prompt builders (fast synth, analyst, critic, agents)
* ``agents``        — Deep Research channel agents + analyst + critic
* ``orchestrator``  — Deep Research fan-out (mirrors priorities/orchestrator mechanics)
* ``service``       — run_fast / run_deep_job entry points (what the router calls)
* ``crm_link``      — stamps the researched company in HubSpot with a link back
  to its X-Ray page. Called from the ROUTER, not from ``service`` — the write
  token lives on the Service only, never on the ``cubie-xray-deep`` Job.
"""
