"""Scheduled tasks — per-user recurring Cubie runs created from Chat.

Layout, in the order a task moves through it:

* ``schedule`` — when does this fire next (pure, timezone-aware, testable).
* ``store``    — persistence, including the two compare-and-swap claims that
  make concurrent ticks and Cloud Run retries safe.
* ``dispatch`` — the Cloud Scheduler tick: claim due tasks, launch a worker.
* ``runner``   — one run: replay the prompt as its owner through the chat agent.
* ``delivery`` — Slack DM / email / notification fan-out.

The chat-facing tools live in ``rubik_core/tools/scheduled_tasks_tools.py``,
the HTTP surface in ``backend/routers/scheduled_tasks.py``, and the worker
entrypoint in ``backend/jobs/scheduled_task_run_job.py``.
"""
