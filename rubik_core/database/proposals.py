"""Proposal Generator tables — one proposal per sales call, every version kept.

A *proposal* here is the short client-facing document an AE sends right after a
discovery call to gauge interest before anyone writes an SOW — the thing AEs
used to produce by hand by pasting a Fathom transcript into a chatbot. The
Sales Calls wrap-up already detects the moment a prospect asks for one (the
``sow_request`` extraction item) and starts the SOW clock in the Cockpit; these
rows are what turns that detection into a draft.

Two tables, and the split is the whole design:

* ``proposals`` holds the BRIEF — the structured facts the document is built
  from (who it is for, the service, scope items, the outcomes the prospect
  stated, pricing lines, open questions). The brief is what the AE confirms
  before anything is generated, because not all the information is always
  confirmed on the call, and a generator that guesses
  the price produces three or four wrong versions.
* ``proposal_versions`` holds every generated document as ``(brief snapshot,
  content)``. A revision ("tighten the summary", "change the price to 5,500")
  appends a version rather than overwriting, so the AE can always go back —
  and the pricing table is rendered from the version's OWN brief snapshot, never
  from prose, which is the structural fence against the model adding a service
  or a number that was never agreed.

Style follows ``rubik_core/database/call_capture.py``: ``String`` PKs holding
uuid4 hex minted at the call site, Python-side ``datetime.utcnow`` defaults,
``JSON`` payloads, and a module-level status tuple behind a portable
``CheckConstraint``. ``owner_email`` is lower-folded and carries NO foreign key,
like ``commitments.owner_email``: a departed AE's proposals must outlive their
``users`` row so the offboard sweep can hand them to a successor.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from rubik_core.database.constraints import in_check
from rubik_core.database.models import Base

#: ``proposals.status``. ``brief`` = the facts are being confirmed, nothing
#: generated yet; ``drafted`` = at least one version exists; ``archived`` = the
#: AE put it away (a superseded opportunity, a duplicate). There is no ``sent``
#: state on purpose — Cubie never sends a proposal and does not yet observe the
#: AE doing so; the Cockpit's SOW-sent detector owns that question.
PROPOSAL_STATUSES: tuple[str, ...] = ("brief", "drafted", "archived")


class Proposal(Base):
    """One proposal per captured call — the brief, and the pointer to its versions."""

    __tablename__ = "proposals"

    id = Column(String, primary_key=True)  # uuid4 hex
    #: Lower-folded. No FK — survives offboarding (see module docstring).
    owner_email = Column(String, nullable=False, index=True)
    #: The call this was drafted from. UNIQUE: a second "Draft proposal" click on
    #: the same wrap-up must open the existing proposal, not mint a twin. NULL
    #: once the call is gone (``SET NULL``).
    call_session_id = Column(
        String,
        ForeignKey("call_sessions.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    hubspot_company_id = Column(String, nullable=True, index=True)
    hubspot_contact_id = Column(String, nullable=True)
    hubspot_deal_id = Column(String, nullable=True, index=True)
    #: Resolved once at creation with the READ token, like
    #: ``call_sessions.attrs["company_name"]`` — the list view must not pay a
    #: HubSpot round-trip per row.
    company_name = Column(String, nullable=True)
    #: Mirrors ``brief["service_title"]`` so a list can render without parsing JSON.
    title = Column(String, nullable=False)
    status = Column(String, nullable=False, default="brief", index=True)
    #: The CURRENT brief — the model's extraction until the AE saves the form,
    #: then whatever the AE confirmed. Shape: ``rubik_core/proposals/brief.py``.
    brief = Column(JSON, nullable=True)
    #: The model's ORIGINAL extraction from the call, untouched. Kept so the
    #: gap between what Cubie heard and what the AE corrected can be measured
    #: and the prompt tuned against it; never rendered.
    brief_source = Column(JSON, nullable=True)
    brief_confirmed_at = Column(DateTime, nullable=True)
    #: Model that produced ``brief_source``. Versions carry their own.
    model = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    versions = relationship(
        "ProposalVersion",
        back_populates="proposal",
        cascade="all, delete-orphan",
        order_by="ProposalVersion.version_no",
    )

    __table_args__ = (
        CheckConstraint(
            in_check("status", PROPOSAL_STATUSES), name="ck_proposals_status"
        ),
    )


class ProposalVersion(Base):
    """One generated document. Append-only; ``version_no`` is 1-based per proposal."""

    __tablename__ = "proposal_versions"

    id = Column(String, primary_key=True)  # uuid4 hex
    proposal_id = Column(
        String,
        ForeignKey("proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no = Column(Integer, nullable=False)
    #: The brief THIS version was rendered from. A revision may legitimately
    #: change it ("change the price to 5,500"), so it is snapshotted per version
    #: rather than read from the parent — otherwise downloading v1 after a v3
    #: price change would print v3's table under v1's prose.
    brief = Column(JSON, nullable=False)
    #: The prose. Shape: ``rubik_core/proposals/brief.py::validate_content``.
    content = Column(JSON, nullable=False)
    #: What the AE asked for. NULL on a version generated straight from the
    #: brief; the AE's own words on a revision. Together with ``change_summary``
    #: this IS the revision chat — no separate message table.
    instruction = Column(Text, nullable=True)
    change_summary = Column(Text, nullable=True)
    model = Column(String, nullable=True)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    proposal = relationship("Proposal", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("proposal_id", "version_no", name="uq_proposal_versions_no"),
    )
