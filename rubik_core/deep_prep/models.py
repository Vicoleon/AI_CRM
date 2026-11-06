"""Structured result of a Deep Prep council run.

Unlike the on-screen brief's fixed 14 fields, a deep prep is a variable set of
sections the council *agreed* this meeting warrants — so the shape is a list of
sections plus the honest extras (what we don't know, open questions) and the
council's own audit trail (the agreed plan + the verifier's verdict).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class DeepPrepSection:
    heading: str
    body: str
    sources: list[str] = field(default_factory=list)


@dataclass
class DeepPrepResult:
    title: str
    headline: str = ""
    sections: list[DeepPrepSection] = field(default_factory=list)
    # Called out honestly rather than papered over.
    data_gaps: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    all_sources: list[str] = field(default_factory=list)
    # Council audit trail — stored for transparency/debugging, not shown raw.
    plan: dict = field(default_factory=dict)
    verdict: dict = field(default_factory=dict)
    is_sales_user: bool = False
    is_internal: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
