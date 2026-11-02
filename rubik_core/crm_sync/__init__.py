"""UC-46 CRM Sync — call transcripts in, approved HubSpot updates out.

Cubie reads an AE's recent external call transcripts, compares what was said
against what HubSpot currently holds, and proposes specific record changes. The
AE approves (or edits, or dismisses) each one individually; only then is
anything written, and the write goes through the gated
``rubik_core/retrieval/hubspot_write.py`` surface.

Nothing in this package ever applies a change on its own.
"""
