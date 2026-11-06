"""Cross-cutting concerns shared by the sales features.

The sales surfaces each live in their own package (``proposals``, ``xray``,
``call_capture``, ``crm_sync``); this one holds what they
share. Today that is model routing — see :mod:`rubik_core.sales.model_config`.
"""
