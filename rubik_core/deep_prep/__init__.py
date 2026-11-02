"""Deep Prep — async, emailed meeting prep.

The on-screen Meeting Prep (``backend/routers/home.py``) generates a brief the
user waits for. Deep Prep runs the same grounded generation in the background,
renders it to a branded PDF, and emails it — so it can be richer without a
latency budget. The generation stage is deliberately pluggable: today it reuses
the grounded ``generate_meeting_prep_analysis``; the multi-model "council that
agrees what to deliver" swaps in here later without touching PDF/delivery.
"""
