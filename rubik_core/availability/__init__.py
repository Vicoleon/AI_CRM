"""Availability / out-of-office awareness for Cubie.

This package turns Google Calendar out-of-office ("OOO") events into a
first-class fact the rest of the app can reason about — instead of the old
behaviour where OOO was the one calendar signal we *discarded*
(:func:`rubik_core.tools.calendar.is_non_meeting_event`).

Two consumers, one detector:

* **Your own PTO** — detected live, per run, from your own calendar
  (:func:`detect_pto`), and carried on the priorities ``TemporalContext`` so
  every reasoning stage knows you're out.
* **Teammates' PTO** — materialised into the ``ooo_periods`` table by a daily
  refresh job so chat / priorities / the manager digest can answer
  "who's out this week?" with a cheap SQL read instead of impersonating N
  calendars per question.
"""

from rubik_core.availability.pto import (
    PtoPeriod,
    PtoStatus,
    detect_pto,
    pto_periods_from_events,
)

__all__ = [
    "PtoPeriod",
    "PtoStatus",
    "detect_pto",
    "pto_periods_from_events",
]
