"""Smart Teams — who may see whose profile, answered in exactly one place.

Two questions get asked all over this feature, and every surface has to answer
them identically (plan Decision #3, "open guides, private raw"):

* **Raw data** — the interview transcript, the per-item answers, the numeric
  scores. Private to the person it describes, plus the support admins
  (:func:`is_support_admin`).
* **The guide** — the "How to work with {NAME}" write-up generated from that
  raw data. Company-visible once its subject has published it; a draft
  belongs to its author alone until then.

They live here rather than in ``backend/auth.py`` because neither is a
router-level gate: both turn on WHOSE profile is being read, which is path or
row data, not caller identity. ``backend.auth.require_smart_teams_access``
answers the only question a dependency can answer — "may this caller use Smart
Teams at all" — and everything per-subject is answered below.

They live in ``rubik_core`` rather than ``backend`` because the router is not
the only caller. The comparison chat tool (ST-7) and the Team Builder matcher
(ST-9) read profiles from inside ADK tool code, which never passes through a
FastAPI dependency, and the guide-ingest and un-ingest paths run on the
cubie-worker Job with no request at all. A caller that re-derives "is this
guide public" from a flag comparison of its own is how a guide someone
retracted keeps getting quoted in chat. So: pure predicates, no framework
imports, and the denial copy sits next to the rule it belongs to so every
surface refuses in the same words.

**Every predicate re-checks the company boundary itself, because most callers
have not met an authenticated request.** Being reachable from ADK tool code and
from a Job means "the caller is one of the company's employees" cannot be
assumed upstream, and two live sources push external addresses into exactly
those paths: ``meeting_participants`` carries external attendee addresses from
synced transcripts, and a HubSpot portal can host a CLIENT pipeline, so
client-side contacts flow through CRM readers. Local mock auth adds more: it
signs in any address it is handed (``X-Mock-User``, or a bearer that looks like
an email) before the Google domain check runs, and stock example addresses such
as ``dev@example.com`` and ``admin@example.com`` sit in dev databases.
:func:`is_employee` is therefore
the base of every helper here, and it resolves the Workspace domain the same way
the login check does.

**Nothing here has a production call site yet.** The only callers today are
``backend.auth``'s Smart Teams gate (``user_can_access_smart_teams`` and
``require_smart_teams_access``, which between them use :func:`is_employee` and
:func:`is_impersonation_identity`) plus these predicates' tests — no per-subject
helper below is called from anywhere yet. The Smart Teams routers land in ST-3
and the publish / guide-ingest paths in ST-6, and each of them is what wires
these rules to a request. That is deliberate for this ticket — the rules ship
before the surfaces that must obey them — but it also means no integration test
can prove a router honours them until ST-3 adds one.

**Unpublishing is not enforceable end to end — an ST-6 requirement, not a fact.**
:func:`is_guide_published` denies a retracted guide to every reader that asks it,
and that is the whole of what this module can do. A guide that was already
RAG-ingested (``st_documents.rag_ingested``) stays answerable in chat after its
subject retracts consent, because nothing purges the vectors: ST-6 owes a purge
that calls ``rubik_core.dam.vectorstore.delete_document_vectors`` against BOTH
corpora (the DAM routing asymmetry means a single-corpus delete leaves the
chunks live in the other one), and there is no test here covering that purge
because there is no purge to cover.

Nothing here is a performance boundary. Profiles are ADVISORY — they inform
collaboration and team staffing, and never feed performance review, promotion,
or compensation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = [
    "GUIDE_NOT_PUBLISHED_DENIED",
    "RAW_PROFILE_DENIED",
    "SHARED_GUIDE_NOT_PUBLISHED_DENIED",
    "SUPPORT_FEATURE_KEY",
    "can_view_guide",
    "can_view_multi_subject_guide",
    "can_view_raw_profile",
    "is_employee",
    "is_guide_published",
    "is_impersonation_identity",
    "is_subject",
    "is_support_admin",
    "workspace_domain",
]

#: The Feature Access row that names which admins support Smart Teams and can
#: therefore be shown raw answers to debug a bad score or a stuck interview
#: (``rubik_core/features/registry.py``). Every admin by default.
SUPPORT_FEATURE_KEY = "smart_teams_support"

# Shown when someone asks for another person's interview answers or scores.
# Deliberately points at the thing they CAN read rather than just refusing.
RAW_PROFILE_DENIED = (
    "Interview answers and scores stay private to the person they describe. "
    "You can read their published working guide instead."
)

# Shown when someone asks for a guide that its subject has not published (or
# has unpublished). Says "not published yet" rather than "you can't see it" —
# the guide may simply not exist for anyone but its author.
GUIDE_NOT_PUBLISHED_DENIED = (
    "This working guide hasn't been published yet. Only the person it "
    "describes can see their draft."
)

# Shown for a pair or team guide when at least one of the people in it has not
# published their own guide. Separate copy because the single-subject wording
# ("only the person it describes") would be a lie about a document describing
# several people, and because the fix is different: somebody else has to publish.
SHARED_GUIDE_NOT_PUBLISHED_DENIED = (
    "This guide covers more than one person, and not everyone in it has "
    "published their working guide yet."
)


def _normalized_email(value: object) -> str:
    """An email folded for comparison, or ``""`` when there isn't one.

    Smart Teams rows are keyed by email, and ``users.email`` is a case-sensitive
    primary key stored verbatim from the Google token — ``auth.py`` never
    lowercases it, which is the same latent trap that sits under
    ``eos_people.email``. Every comparison in this module therefore folds both
    sides, so a person whose token spells them ``JDoe@…`` can still read their
    own profile.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _viewer_email(viewer: str | object) -> str:
    """The viewer's folded address, whether we were handed a profile or a string.

    Both shapes reach these predicates. A router hands over the ``UserProfile``
    from ``get_current_user``; ADK tool code (ST-7, ST-9) and the cubie-worker
    guide paths usually hold nothing but the email they were invoked for. A
    profile-only reader would silently deny a person their OWN data whenever the
    caller passed the string it actually has, so both are accepted explicitly.

    A bare string carries no roles or departments, so a string viewer can never
    satisfy :func:`is_support_admin` — it can only ever be the subject. That is
    the correct reading rather than a limitation: support access is a property of
    a resolved account, and an email alone is not one.
    """
    if isinstance(viewer, str):
        return _normalized_email(viewer)
    return _normalized_email(getattr(viewer, "email", None))


def workspace_domain() -> str:
    """The Workspace domain an address must sit on to belong to the company.

    :func:`rubik_core.company.domain.workspace_domain` reads the primary
    verified domain from the current workspace's control rows. With no primary
    domain, the value is empty and ``is_employee()`` refuses everyone.
    """
    from rubik_core.company.domain import workspace_domain as resolve

    return resolve()


def is_impersonation_identity(email: str | None) -> bool:
    """True iff ``email`` is the pseudo-identity minted by impersonation.

    Admin department impersonation masks the admin's address as
    ``impersonated_user@<department>.local`` (see
    ``backend.auth._apply_impersonation``) and registers a real ``users`` row for
    it, because ``chat_sessions`` has a foreign key. Both halves are matched
    here — the ``impersonated_user`` localpart and the ``.local`` domain — so a
    department whose name changes the domain, or a future pseudo-identity on a
    different ``.local`` host, is still caught.

    Named separately from :func:`is_employee` so the router gate can tell the
    two denials apart: an impersonating admin needs to be told to switch back to
    themselves, while an off-domain caller needs a different sentence entirely.
    """
    folded = _normalized_email(email)
    if not folded or "@" not in folded:
        return False
    if folded.endswith(".local"):
        return True
    return folded.split("@", 1)[0] == "impersonated_user"


def is_employee(email: str | None) -> bool:
    """True iff ``email`` is a company employee's address, and so may be a
    subject or a viewer of Smart Teams data.

    Three things have to hold, and each one has already produced a real row in a
    real database:

    * **On the Workspace domain** (:func:`workspace_domain`). This is the check
      that cannot be inherited from the request: the mock-auth branch of
      ``backend.auth._authenticate_user`` returns before the Google domain check
      runs, and ADK tool code and cubie-worker Jobs never reach it at all. Left
      out, ``vendor@outside.example`` (a client's contact in the HubSpot portal),
      an external meeting attendee from ``meeting_participants`` and a stock
      example address signed in by mock auth (``dev@example.com``,
      ``admin@example.com``) all read published guides.
    * **Not the impersonation pseudo-user**
      (:func:`is_impersonation_identity`). Smart Teams data is SELF-GIVEN and
      keyed by email, so letting that identity through writes an interview, a
      profile and a guide for a person who does not exist, then ranks them in
      Team Builder and counts them on the completion dashboard. Rows from this
      exact pseudo-user have already reached the priorities roster and the
      feedback table; EOS excludes it the same way
      (``backend.routers.eos.people._is_real_user``). An admin who wants to see
      the feature can act as themselves.
    * **An actual address** — a value with an ``@`` in it. ``None`` and ``""``
      are denials, never wildcards.

    Fails closed when no domain resolves at all: with nothing configured, every
    address is off-domain and every read is refused. Config guarantees that
    cannot happen by accident (see :func:`workspace_domain`), and a feature
    holding people's private interview answers is the wrong place to guess.
    """
    folded = _normalized_email(email)
    if not folded or "@" not in folded:
        return False
    if is_impersonation_identity(folded):
        return False
    from rubik_core.company.domain import workspace_domains

    domains = workspace_domains()
    if not domains:
        return False
    return folded.rsplit("@", 1)[-1] in domains


def is_subject(viewer: str | object, subject_email: str | None) -> bool:
    """True iff ``viewer`` is the employee ``subject_email`` describes.

    ``viewer`` may be a ``UserProfile``-shaped object or a bare email string
    (see :func:`_viewer_email`). An unresolvable or off-domain subject is a
    denial rather than a match on ``""``.
    """
    folded = _normalized_email(subject_email)
    if not is_employee(folded):
        return False
    return _viewer_email(viewer) == folded


def is_support_admin(viewer: str | object) -> bool:
    """True iff ``viewer`` is a support admin, and may see raw data for support.

    BOTH halves are required — the admin role AND the ``smart_teams_support``
    feature (:data:`SUPPORT_FEATURE_KEY`, Admin → Feature Access). The feature
    ships with every admin on it; an admin narrows it to the admins of one team
    by unticking Admins and naming that team (primary or managed department).
    No Feature Access row can widen it past the admin role. This is the
    narrowest gate in the app on purpose, and the two obvious near-misses are
    both denied:

    * an **exec** gets nothing here. ``is_exec`` bypasses the department
      lockout elsewhere, so it is the flag people reach
      for when they want someone to "see more" — and raw psychometric answers
      about their reports is the one thing seniority must not buy. A leader who
      wants to know how to work with someone reads their published guide, the
      same artifact everyone else reads.
    * a **Team Manager** gets nothing here either. ``managed_departments``
      confers running a team's L10 and owning its Rocks; it does not confer
      reading what that team typed into an interview about themselves.

    Admins the Feature Access row leaves out are excluded too. An admin can of
    course edit that row and come back — nothing in an app whose admins can
    rewrite access can prevent that — but it takes a deliberate, audited
    write rather than a silent read, which is the difference this predicate is
    here to make.

    A bare email string is never a support admin: it carries no roles. Callers
    that only hold an email (ADK tools, worker paths) therefore get subject
    access or nothing, which is the right default for code with no session.
    """
    if not is_employee(_viewer_email(viewer)):
        return False
    if not bool(getattr(viewer, "is_admin", False)):
        return False

    from rubik_core.features import feature_allows  # local: keep leaf light

    return feature_allows(SUPPORT_FEATURE_KEY, viewer)


def can_curate_skill_taxonomy(viewer: str | object) -> bool:
    """True iff ``viewer`` may add or retire a curated skill.

    Same principals as :func:`is_support_admin` — the admin role AND the
    ``smart_teams_support`` feature — but named for what it authorizes rather
    than for reading raw data, because the two are different kinds of permission that happen to land
    on the same people today. A separate name is the seam to change one without
    the other.

    Why this narrow, when the taxonomy is not psychometric data: the skill list
    is a COMPANY-WIDE vocabulary. Every declaration, every inference and every
    staffing answer is expressed in it, and a row added here cannot be deleted
    later — ``st_person_skills`` holds a RESTRICT foreign key, so a skill people
    have claimed can only ever be retired. That makes adding one closer to a
    schema change than to a content edit, and it belongs with the admins who
    support Smart Teams.

    Excludes execs and Team Managers for the reason :func:`is_support_admin`
    gives: seniority over people is not authority over the vocabulary those
    people are described in.
    """
    return is_support_admin(viewer)


def can_view_raw_profile(viewer: str | object, subject_email: str | None) -> bool:
    """True iff ``viewer`` may read ``subject_email``'s interview answers/scores.

    The person themselves, or a support admin supporting them. Nobody else —
    not their manager, not an exec, not an admin the support row leaves out.
    Callers refuse with :data:`RAW_PROFILE_DENIED`.

    An unknown subject (``None``, blank, or an address outside the company) is a
    denial for EVERYONE, support admins included. Support access is permission to
    help a named colleague, not a wildcard: answering "whose raw data?" with
    "we don't know" and handing rows over anyway is how a caller that lost its
    subject on the way — a missing path param, an empty row, a lookup that
    returned nothing — turns into a dump of somebody's private answers.
    """
    if not is_employee(subject_email):
        return False
    return is_subject(viewer, subject_email) or is_support_admin(viewer)


def is_guide_published(published: object) -> bool:
    """True iff an ``st_documents`` row's ``published`` flag means company-visible.

    Publication is a BOOLEAN — ``SmartTeamsDocument.published`` is
    ``Column(Boolean, nullable=False, default=False)``, set when the subject
    approves their guide and cleared (with ``unpublished_at`` stamped) when they
    retract it. It is emphatically NOT that row's ``status`` column, which is
    ``pending | ready | failed`` and reports whether cubie-worker finished
    generating the prose: reading generation state as consent would publish
    every guide the moment it rendered.

    Only a real ``True`` counts — plus the ``1`` a raw DBAPI cursor returns for
    the same column, since SQLAlchemy's ``Boolean`` conversion applies to ORM
    reads and not to a ``text()`` SELECT or a psql-style tuple. Everything else
    is a denial, including ``None`` and any string: this is the consent switch,
    so a value we do not recognize must fail closed rather than guess.
    """
    if isinstance(published, bool):
        return published
    if isinstance(published, int):  # raw DBAPI row: 1/0, not True/False
        return published == 1
    return False


def can_view_guide(
    viewer: str | object, subject_email: str | None, published: object
) -> bool:
    """True iff ``viewer`` may read ``subject_email``'s guide.

    ``published`` is the ``st_documents.published`` boolean for that guide. A
    published guide is readable by every employee — that is the whole point of
    writing one. An unpublished one is readable by its subject, and by a support
    admin (who can already see the raw answers the draft was generated from, so
    hiding the draft from them would protect nothing while making a support
    request unanswerable). Callers refuse with
    :data:`GUIDE_NOT_PUBLISHED_DENIED`.

    Both parties must be employees: an off-domain or unknown viewer is refused
    even for a published guide, and an unknown subject is refused to everyone —
    the same fail-closed reading as :func:`can_view_raw_profile`.

    Only for a guide with ONE subject. A pair or team guide carries N subjects
    behind a single flag, which is not N consents; use
    :func:`can_view_multi_subject_guide` for those.
    """
    if not is_employee(_viewer_email(viewer)):
        return False
    if not is_employee(subject_email):
        return False
    if is_subject(viewer, subject_email) or is_support_admin(viewer):
        return True
    return is_guide_published(published)


def can_view_multi_subject_guide(
    viewer: str | object,
    subject_emails: Sequence[str | None],
    published_by_subject: Mapping[str, object],
) -> bool:
    """True iff ``viewer`` may read a pair or team guide about ``subject_emails``.

    ``st_documents.subject_emails`` holds N people behind ONE ``published``
    flag, so that flag cannot be the rule: whoever asked for the pair guide
    would be consenting on their colleague's behalf. The rule is instead
    per-person, and ``published_by_subject`` maps each subject's address to the
    ``published`` flag of THEIR OWN personal guide:

    * every subject other than the viewer must have published their own guide.
      One person's unpublished profile must not become readable by wrapping it
      in a document that also mentions somebody who did publish.
    * the viewer's own entry is not required. A pair guide is partly about them,
      and consent is something they give others, not something they need in order
      to read about themselves.
    * a support admin reads it for support, exactly as with a single guide.
    * an empty subject list, or any subject that is not an employee, is a
      denial. A shared guide whose subjects we cannot resolve is not readable by
      anybody.

    The document's own ``published`` flag is deliberately ignored here. It stays
    meaningful for a personal guide, where the one subject IS the one consent;
    on a shared document it records nothing more than that somebody pressed a
    button. Callers refuse with :data:`SHARED_GUIDE_NOT_PUBLISHED_DENIED`.
    """
    viewer_email = _viewer_email(viewer)
    if not is_employee(viewer_email):
        return False

    subjects = [_normalized_email(e) for e in (subject_emails or [])]
    if not subjects or not all(is_employee(e) for e in subjects):
        return False

    if is_support_admin(viewer):
        return True

    published = {
        _normalized_email(k): v for k, v in (published_by_subject or {}).items()
    }
    return all(
        subject == viewer_email or is_guide_published(published.get(subject))
        for subject in subjects
    )
