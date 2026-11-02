"""Pull EOS rows out of `eos_*` tables and shape them into `EosDoc`s
ready for upload to GCS + import into Vertex AI.

Why this exists (instead of streaming straight from SQLAlchemy rows):

1. The Vertex RAG import API (`rag.import_files`) takes file URIs and
   chunks them itself. It does NOT accept per-chunk metadata. So we
   encode the metadata in the **file content header** — Markdown-ish,
   reads naturally for the LLM:

       # EOS · Rocks · "Launch v2 portal"
       Status: off_track · Quarter: Q2 2026 · Owner: Jordan
       ---
       <body>

   When a chunk comes back from retrieval, this header is visible to
   the LLM as part of the context — it knows the source + key facts
   without needing structured metadata fields.

2. Each EOS record becomes one file (one upload, one import). Small
   per-record files keep retrieval results focused; the LLM gets the
   right rock and not a wall of rocks. A future optimization could
   batch many records into one file, but for the data volumes EOS
   produces (~hundreds of rocks/year), per-record is cheap.

3. Filename convention `eos__<kind>__<id>.md` makes EOS files easy to
   identify for future cleanup utilities, distinct from Cubie's other
   uploaded assets.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from rubik_core.database.eos import (
    EosProcess,
    Issue,
    IssueStatus,
    L10Meeting,
    L10Session,
    Organization,
    Person,
    Quarter,
    Rock,
    VTOSection,
    VTOSectionType,
    VTOVersion,
)
from rubik_core.ingestion.eos.redact import redact_pii
from rubik_core.ingestion.eos.vto_xlsx import bullets_from_content

# Public dataclass passed between extractor and pipeline.


@dataclass
class EosDoc:
    """One unit of EOS content destined for the RAG corpus.

    `filename` is the GCS-friendly basename (no path); `pipeline.py`
    writes the file locally, uploads to GCS, and imports to Vertex.

    `header` is a Markdown-ish block prepended to `body` so retrieval
    chunks self-describe their source. Body can be empty if header is
    enough (rare).
    """

    kind: str  # one of: vto | rocks | todos | issues | l10 | processes
    source_id: str  # the row's UUID (or "<version_id>__<section_type>" for VTO)
    filename: str  # `eos__<kind>__<source_id>.md`
    header: str
    body: str
    # SOC 2 H-14: Exec-team-only content. When True the pipeline routes this doc
    # into the exec-only RAG corpus so ONLY Exec-team members can retrieve it.
    is_exec: bool = False

    @property
    def text(self) -> str:
        return self.header.rstrip() + "\n\n---\n\n" + self.body.lstrip()


# ─── Helpers ────────────────────────────────────────────────────────────


def _safe_filename(kind: str, source_id: str) -> str:
    safe_id = source_id.replace("/", "_").replace(" ", "_")
    return f"eos__{kind}__{safe_id}.md"


def _person_name(person: Person | None) -> str:
    if person is None:
        return "—"
    name = f"{person.first_name or ''} {person.last_name or ''}".strip()
    return name or person.email or "—"


def _flatten_content(content) -> str:
    """VTO section content is bullets shaped
    ``{"text": str, "confidential": bool}`` (``vto_xlsx.bullets_from_content``
    also tolerates the legacy plain-``list[str]`` shape). Render as a bullet
    list for the body — this feeds the already Exec-gated ``vto`` RAG
    corpus, so confidential bullets are included same as open ones;
    exec chat already sees everything here."""
    bullets = bullets_from_content(content)
    return "\n".join(f"- {b.text}" for b in bullets)


# ─── Per-source extractors ──────────────────────────────────────────────


def extract_vto(db: Session) -> list[EosDoc]:
    """One doc per VTO section of the (single) active VTO version. We
    do not re-index every historical version — the LLM should answer
    against current direction; historical context lives in chat history.
    """
    org = (
        db.query(Organization)
        .filter(Organization.is_deleted == False)  # noqa: E712
        .first()
    )
    if org is None:
        return []
    version = (
        db.query(VTOVersion)
        .filter(
            VTOVersion.organization_id == org.id,
            VTOVersion.is_deleted == False,  # noqa: E712
        )
        .order_by(VTOVersion.is_active.desc(), VTOVersion.created_at.desc())
        .first()
    )
    if version is None:
        return []
    sections = (
        db.query(VTOSection)
        .filter(
            VTOSection.vto_version_id == version.id,
            VTOSection.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    docs: list[EosDoc] = []
    for s in sections:
        section_type = (
            s.section_type.value
            if hasattr(s.section_type, "value")
            else str(s.section_type)
        )
        body = _flatten_content(s.content)
        if not body:
            continue
        source_id = f"{version.id}__{section_type}"
        header = (
            f"# EOS · VTO · {_human_section_label(section_type)}\n"
            f"Version: {version.version_name} · "
            f"Status: {version.status}"
        )
        docs.append(
            EosDoc(
                kind="vto",
                source_id=source_id,
                filename=_safe_filename("vto", source_id),
                header=header,
                body=body,
            )
        )
    return docs


_VTO_LABELS = {
    VTOSectionType.VISION.value: "Vision",
    VTOSectionType.MISSION.value: "Mission",
    VTOSectionType.VALUES.value: "Core Values",
    VTOSectionType.FOCUS.value: "Core Focus",
    VTOSectionType.TEN_YEAR_TARGET.value: "10-Year Target",
    VTOSectionType.THREE_YEAR_PICTURE.value: "3-Year Picture",
    VTOSectionType.ONE_YEAR_PLAN.value: "1-Year Plan",
    VTOSectionType.QUARTERLY_ROCKS.value: "Quarterly Rocks",
    VTOSectionType.ISSUES_LIST.value: "Issues List",
}


def _human_section_label(section_type: str) -> str:
    return _VTO_LABELS.get(section_type, section_type.replace("_", " ").title())


def extract_rocks(db: Session, only_active_quarter: bool = True) -> list[EosDoc]:
    """One doc per Rock. Defaults to current quarter only — historical
    rocks are out of scope for retrieval (use the alignment provider for
    'what's our current direction')."""
    org = (
        db.query(Organization)
        .filter(Organization.is_deleted == False)  # noqa: E712
        .first()
    )
    if org is None:
        return []
    rocks_query = db.query(Rock).filter(Rock.is_deleted == False)  # noqa: E712
    quarter_name: str | None = None
    if only_active_quarter:
        q = (
            db.query(Quarter)
            .filter(
                Quarter.organization_id == org.id,
                Quarter.is_current == True,  # noqa: E712
                Quarter.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if q is None:
            return []
        quarter_name = q.name
        rocks_query = rocks_query.filter(Rock.quarter_id == q.id)

    rocks = rocks_query.all()
    people = {p.id: p for p in db.query(Person).all()}

    docs: list[EosDoc] = []
    for r in rocks:
        status = r.status.value if hasattr(r.status, "value") else str(r.status)
        visibility = (
            r.visibility.value if hasattr(r.visibility, "value") else str(r.visibility)
        )
        owner = _person_name(people.get(r.owner_id))
        header = (
            f"# EOS · Rock · {visibility}\n"
            f"Status: {status}"
            + (f" · Quarter: {quarter_name}" if quarter_name else "")
            + f" · Owner: {owner}"
        )
        docs.append(
            EosDoc(
                kind="rocks",
                source_id=str(r.id),
                filename=_safe_filename("rocks", str(r.id)),
                header=header,
                body=r.description or "",
                is_exec=bool(getattr(r, "is_exec", False)),
            )
        )
    return docs


def extract_issues(db: Session, only_solved: bool = False) -> list[EosDoc]:
    """One doc per Issue. By default we ingest all non-archived issues;
    pass `only_solved=True` for the event-driven hook that fires when an
    issue moves to status=solved.
    """
    q = db.query(Issue).filter(Issue.is_deleted == False)  # noqa: E712
    if only_solved:
        q = q.filter(Issue.status == IssueStatus.SOLVED)
    issues = q.all()
    people = {p.id: p for p in db.query(Person).all()}

    docs: list[EosDoc] = []
    for i in issues:
        status = i.status.value if hasattr(i.status, "value") else str(i.status)
        priority = i.priority.value if hasattr(i.priority, "value") else str(i.priority)
        owner = _person_name(people.get(i.owner_id)) if i.owner_id else None
        bits = [f"Status: {status}", f"Priority: {priority}"]
        if owner:
            bits.append(f"Owner: {owner}")
        if i.department_name:
            bits.append(f"Department: {i.department_name}")
        header = "# EOS · Issue\n" + " · ".join(bits)
        # Lead the body with the title (the human-facing headline) when it adds
        # signal beyond the description, so retrieval matches on it too.
        title = (i.title or "").strip()
        description = i.description or ""
        if title and title != description.strip():
            body = f"{title}\n\n{description}"
        else:
            body = description
        docs.append(
            EosDoc(
                kind="issues",
                source_id=str(i.id),
                filename=_safe_filename("issues", str(i.id)),
                header=header,
                body=body,
                is_exec=bool(getattr(i, "is_exec", False)),
            )
        )
    return docs


def extract_l10(
    db: Session,
    session_id: str | None = None,
    only_ended: bool = True,
) -> list[EosDoc]:
    """One doc per L10 session. `notes` is the session's free-form
    closing text; if empty, we skip the session (no point indexing an
    empty doc).

    `session_id`: when set, extract only that one session — used by the
    event-driven hook fired from `POST /sessions/{id}/end`.
    `only_ended`: when True (default), skip sessions that haven't yet
    been concluded.
    """
    q = db.query(L10Session).filter(L10Session.is_deleted == False)  # noqa: E712
    if session_id:
        q = q.filter(L10Session.id == session_id)
    if only_ended:
        q = q.filter(L10Session.actual_end.isnot(None))
    sessions = q.all()
    meetings = {m.id: m for m in db.query(L10Meeting).all()}

    docs: list[EosDoc] = []
    for s in sessions:
        notes = (s.notes or "").strip()
        if not notes:
            continue
        meeting = meetings.get(s.meeting_id)
        meeting_name = meeting.name if meeting else "(unknown meeting)"
        dept = meeting.department_name if meeting and meeting.department_name else None
        bits = [f"Meeting: {meeting_name}"]
        if dept:
            bits.append(f"Department: {dept}")
        bits.append(f"Date: {s.actual_start.date().isoformat()}")
        if s.average_rating is not None:
            bits.append(f"Avg rating: {s.average_rating}/10")
        header = "# EOS · L10 Session Notes\n" + " · ".join(bits)
        docs.append(
            EosDoc(
                kind="l10",
                source_id=str(s.id),
                filename=_safe_filename("l10", str(s.id)),
                header=header,
                body=redact_pii(notes),
                is_exec=bool(getattr(meeting, "is_exec_meeting", False))
                if meeting
                else False,
            )
        )
    return docs


def extract_processes(db: Session) -> list[EosDoc]:
    """One doc per Core Process / FBA document. The name itself is the
    body — when documentation URLs exist we also include the URL so the
    LLM can cite it."""
    processes = (
        db.query(EosProcess).filter(EosProcess.is_deleted == False).all()  # noqa: E712
    )
    people = {p.id: p for p in db.query(Person).all()}

    docs: list[EosDoc] = []
    for p in processes:
        owner = _person_name(people.get(p.owner_id)) if p.owner_id else None
        bits = []
        if owner:
            bits.append(f"Owner: {owner}")
        if p.documentation_url:
            bits.append(f"Doc: {p.documentation_url}")
        header = "# EOS · Core Process\n" + (" · ".join(bits) if bits else "")
        docs.append(
            EosDoc(
                kind="processes",
                source_id=str(p.id),
                filename=_safe_filename("processes", str(p.id)),
                header=header.rstrip(),
                body=p.name or "",
            )
        )
    return docs


def extract_all(db: Session) -> list[EosDoc]:
    """Convenience: every source. Used by the weekly cron's full sweep."""
    return [
        *extract_vto(db),
        *extract_rocks(db),
        *extract_issues(db),
        *extract_l10(db),
        *extract_processes(db),
    ]
