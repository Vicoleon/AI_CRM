"""Sales Call Capture System (CCS) — Google Meet -> Cubie -> HubSpot.

The one rule this package exists to enforce: **every proposed HubSpot write
carries a verbatim quote and resolves to a stored transcript utterance.** It is
enforced at the database level (``call_extraction_items.evidence_text`` is NOT
NULL) because without it the approval screen is a rubber stamp with extra steps.
"""
