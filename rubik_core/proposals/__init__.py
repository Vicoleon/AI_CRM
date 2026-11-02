"""Proposal Generator — a client proposal drafted from a captured sales call.

Three steps, and the order is the product decision:

1. **Brief** (``service.start_from_call``): Cubie reads the call transcript and
   the HubSpot company / contact / deal and fills a structured brief — who it is
   for, the service, the scope items that were actually discussed, the outcomes
   the PROSPECT stated (each with its verbatim quote), any pricing that was
   spoken, and the open questions. The AE confirms or corrects it. Nothing is
   generated until they do, because a proposal built on an unconfirmed price
   is the three-or-four-drafts problem this replaces.
2. **Generate** (``service.generate``): the confirmed brief becomes a one-page
   proposal — Executive Summary, Scope of Work, Desired Outcomes & Key
   Deliverables, the chosen template's write-up, Pricing & Investment, notes —
   on the company's letterhead (``brand.py``). The model writes prose keyed to
   the brief's item ids; it cannot add a service, a deliverable or a number,
   because ``brief.normalize_content`` drops anything not in the brief and any
   sentence that states a price, and the pricing table is rendered from the
   brief itself. Templates and their catalog prices are the workspace's own
   data (``templates.py``).
3. **Revise** (``service.revise``): the AE types an instruction — "tighten the
   summary", "change the price to 5,500", "drop item 3" — and gets a NEW
   version. Every version is kept and any can be downloaded as PDF or Word.

Cubie never sends the document. The AE downloads it and sends it themselves.
"""

from __future__ import annotations

#: The AI Cost category. ``@attribute_to(COST_FEATURE)`` sits on the functions
#: that call Vertex (``service._llm_json``); the matching ``_FEATURE_LABELS``
#: entry in ``backend/routers/admin.py`` is pinned by a test.
COST_FEATURE = "proposal_generator"
