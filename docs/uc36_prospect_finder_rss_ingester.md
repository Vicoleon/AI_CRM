UC-36 — Prospect Finder (RSS ingester)
=======================================

Status: SCOPING
Depends on: UC-30 (jobs framework), UC-14 (consumer-side, already on main)
Supersedes: UC-14 abandoned RSS work (``claude/uc14-slice2-rss-config``)
LinkedIn item: #4

Problem
-------

UC-14 left a half-built feature: the consumer side
(``fetch_market_triggers``; its ``build_market_trigger_recommendations``
companion was deleted with the retired UC-10 plugin — see git history)
shipped to main, reading from ``hubspot_sales_marts.market_triggers`` in
BigQuery (short-circuited to ``[]`` today since the table was never
provisioned). But the producer
side (the RSS / news ingester that POPULATES that table) never
landed — slice 1 + slice 2 sit on the abandoned branch
``origin/claude/uc14-slice2-rss-config``.

The result: ``market_triggers`` is empty on dev BQ, no triggers
surface in priorities, and the Prospect Finder workflow from the
LinkedIn post has no upstream.

Scope
-----

**Port and finish** the abandoned branch. Land it on top of the
current branch, refit to UC-20→25's patterns where they've drifted,
and schedule it to run before the daily AI Priorities run so
``market_triggers`` is fresh.

Files to port from ``claude/uc14-slice2-rss-config``
----------------------------------------------------

- ``rubik_core/adapters/news_sources.py`` — ``NewsSource`` Protocol +
  ``FederalRegisterAPI`` concrete (slice 1).
- ``backend/agents/market_research.py`` — ``MarketResearchAgent``
  ingester: news fetch → LLM classify → BQ write.
- ``backend/services/market_research_config.py`` — per-department
  RSS list stored in ``workspace_settings`` (matches our UC-23 pattern).
- ``backend/routers/market_research_settings.py`` — admin CRUD.
- ``backend/tests/test_market_research{,_settings}.py``.
- ``scripts/create_market_triggers_table.py`` — BQ table DDL.

Refits needed before landing
----------------------------

1. The branch was authored before UC-21 / UC-22. The admin endpoints
   currently use a bespoke pattern; refit to mount under
   ``/api/admin/priorities/news-sources`` so it lives next to the
   playbook + agent-config endpoints UC-21 / UC-23 already shipped.
2. Slice 2's "add RSS via ``feedparser``" work was unfinished. Finish
   it: new ``RSSNewsSource(name, url, category)`` class implementing
   the same Protocol, calling ``feedparser.parse(url)`` and shaping
   each entry into a ``NewsItem``.
3. Add ``feedparser`` to ``requirements.txt``.
4. The LLM classifier on the branch uses an older model id; refit to
   ``settings.VERTEX_LLM_MODEL_FLASH`` (cheap classification — flash
   is right).
5. The branch's BigQuery writer didn't dedupe — add a content hash
   on ``(source_url + triggered_at)`` so re-running the ingester
   doesn't double-insert.

Schedule
--------

Cloud Scheduler ``daily-rss-ingest`` at ``30 1 * * *`` UTC (30 min
before the AI Priorities daily run), hitting
``POST /api/jobs/run/rss_ingest``. The UC-20 nightly pipeline already
runs at 02:00 UTC, so ``market_triggers`` is fresh by the time
UC-22's HubSpot agent reads it.

Storage decisions (per user direction)
--------------------------------------

- **Feed list** lives in the Cubie DB (Postgres) via
  ``workspace_settings`` JSON — same pattern as UC-23 playbooks.
  Editable from admin UI.
- **Triggered events** continue to land in BigQuery
  ``hubspot_sales_marts.market_triggers`` (existing schema:
  ``company_name, trigger_text, triggered_at, source_url``). The
  UC-10 consumer reads from there unchanged.

Admin UI
--------

New subsection under **Admin → AI Priorities → News sources**:
list / add / edit / disable per source. Department dropdown
populated from the live departments table (matches UC-23). "Test"
button hits the source once and shows a preview of recent items
classified as triggers.

Test plan
---------

- Carry over the branch's existing tests (``test_market_research``,
  ``test_market_research_settings``) with minor refits for the
  endpoint mount point.
- New tests:
  - ``RSSNewsSource.fetch_recent`` parses a fixture RSS feed.
  - Dedupe by ``(source_url, triggered_at)`` — second run inserts
    zero rows.
  - LLM classifier mock returns "not a trigger" → no BQ write.

Open questions
--------------

- Initial RSS list — start with the trade press and the
  industry-body newsrooms of the company's own industry? Get the
  exact URLs from the company before launch.
- BigQuery write IAM: the runtime SA already has BQ write to the
  dataset (UC-14 consumer assumes it); no IAM change expected.

Estimate
--------

Medium. ~5-6h: the port itself is mechanical, the refits + the
remaining RSS implementation + dedupe is where the time goes.
