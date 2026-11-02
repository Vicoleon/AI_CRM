"""Cubie Sales Cockpit — commitment aging, escalation and the anti-tsunami digest.

The one rule this package exists to enforce: **a HubSpot stage timestamp is
never the aging clock.** ``hs_v2_date_entered_{stageId}`` may be read for
cross-check, and may seed ``opened_at`` exactly once in the one-off go-live
backfill. Nowhere else. Someone moves a deal into a drafting stage and nobody
moves it again, so an alert built on that property fires on stale data and the
team stops trusting it within two weeks. The clock is ``Commitment.opened_at``,
which Cubie owns, started by evidence and closed by a human confirming evidence.
"""
