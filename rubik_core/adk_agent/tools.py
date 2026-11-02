"""
ADK-compatible tool functions for the chat assistant.

Each tool is a method of the AgentTools class.
ADK auto-registers these as callable tools for the Agent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cost.usage import attribute_to
from rubik_core.tenancy.secrets import workspace_secret
from rubik_core.tenancy.threads import ContextThreadPoolExecutor

logger = logging.getLogger(__name__)


def _run_blocking(fn, *args, **kwargs):
    """Run a blocking callable in a worker thread that is guaranteed a loop.

    The chat tools are ``async def`` but several call synchronous retrieval
    (HubSpot REST + google-genai). Calling that directly blocks the request's
    event loop, so under concurrency one HubSpot turn stalls every other chat.
    Offloading via ``asyncio.to_thread(_run_blocking, ...)`` keeps the loop free.
    We also ensure the worker thread has an event loop — mirroring
    ``backend.routers.chat._run_with_loop`` — so any Vertex SDK path that expects
    one won't trip when run off the main thread.
    """
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    return fn(*args, **kwargs)


# Recency-style meeting questions ("summarize my last meeting", "what happened in
# my most recent call") can't be answered by a semantic search over the recency
# phrase itself — those words don't match any transcript *content*, so the vector
# store returns nothing. When the query reads like this we resolve the meeting by
# date instead. Kept deliberately tight so genuinely topical queries that merely
# contain the word "meeting" still go through semantic search.
_RECENCY_RE = re.compile(
    r"\b("
    r"last meeting|latest meeting|most recent meeting|previous meeting|recent meeting|"
    r"last call|latest call|most recent call|previous call|recent call|"
    r"last sync|latest sync|recent sync|"
    r"last one|latest one|most recent one|"
    r"my last|just had|just finished|earlier today|this morning|"
    r"yesterday'?s meeting|recent meetings|recent calls|"
    r"last few meetings|past few meetings|last \d+ meetings"
    r")\b",
    re.IGNORECASE,
)

# Plural recency ("my recent meetings", "the last few calls") → return several;
# singular ("my last meeting") → just the latest one.
_PLURAL_RECENCY_RE = re.compile(
    r"\b(meetings|calls|syncs|few|several|recent ones|last \d+)\b",
    re.IGNORECASE,
)


def _is_recency_query(query: str) -> bool:
    """True when the question is about the user's most recent meeting(s) by date."""
    return bool(query and _RECENCY_RE.search(query))


# Transcript retrieval caps. A named / recency lookup is a deliberate "give me
# THIS meeting" intent, so we read the whole transcript up to a generous cap
# rather than a short excerpt — a 90-minute L10 clipped at 7k chars is exactly
# why "full L10 notes" came back as a thin summary and drafted follow-ups
# dropped call to-do's. 50k aligns with TranscriptService's 50KB read and covers
# most full meetings. Topical (semantic) hits stitch several chunks together, so
# each chunk still returns a smaller slice.
FULL_TRANSCRIPT_MAX_CHARS = 50000
SEMANTIC_CHUNK_MAX_CHARS = 12000


def _read_transcript_text(
    source_path: str | None, max_chars: int = FULL_TRANSCRIPT_MAX_CHARS
) -> str:
    """Best-effort fetch of a transcript's raw text from its stored source_path.

    GCP transcripts live at ``gs://…`` (the asset's source_path); local/dev ones
    at a filesystem path. Returns ``""`` when the text can't be read so callers
    degrade gracefully. Truncated to ``max_chars`` so a long transcript can't blow
    the model's context window.
    """
    if not source_path:
        return ""
    try:
        from rubik_core.tenancy.storage import assert_own_uri

        checked = assert_own_uri(source_path)
        if source_path.startswith("gs://"):
            from google.cloud import storage

            from rubik_core.config import settings

            rest = checked[len("gs://") :]
            bucket_name, _, blob_name = rest.partition("/")
            if not blob_name:
                return ""
            client = storage.Client(project=settings.GCP_PROJECT_ID)
            blob = client.bucket(bucket_name).blob(blob_name)
            if not blob.exists():
                return ""
            text = blob.download_as_text()
        elif os.path.exists(checked):
            with open(checked, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        else:
            return ""
    except Exception as e:  # noqa: BLE001 — never let a read failure break the tool
        logger.warning("Could not read transcript text from %s: %s", source_path, e)
        return ""
    return text[:max_chars]


# A knowledge-base request that names a specific document ("summarize the Sales
# End User UAT *file*", "open the onboarding *policy*") can't be served reliably
# by a semantic search over the request phrase: the instruction words match no
# document *content*, and the file may live in a corpus outside the user's normal
# RAG fan-out — e.g. a doc they own that was filed under another department. So
# when the query points at a named artifact we resolve it against the DAM
# registry by filename and read its text directly, the same "resolve by lookup,
# not vector search" approach the recency meeting path uses. Kept deliberately
# tight (artifact word + ≥2 distinctive title tokens + strong filename coverage +
# an access check) so generic knowledge questions still go to semantic search.
_DOC_REFERENCE_RE = re.compile(
    r"\b("
    r"file|files|document|documents|doc|docs|report|reports|policy|policies|"
    r"playbook|playbooks|guide|guides|guidelines|memo|memos|deck|decks|"
    r"spreadsheet|spreadsheets|sheet|sheets|template|templates|script|scripts|"
    r"manual|manuals|sop|sops|checklist|checklists|handbook|handbooks|"
    r"attachment|attachments|titled|named|called|"
    # Collection nouns. People name a lookup table by what it holds — "the
    # client acronym LIST", "the vendor ROSTER" — and that shape of document
    # is exactly the one where a single row answers the question, so it must
    # be read directly rather than sampled by vector search.
    r"list|lists|roster|rosters|glossary|glossaries|index|indexes|directory|"
    r"catalog|catalogue|catalogs|register|registry|tracker|trackers|"
    r"workbook|workbooks|database|databases|matrix|log|logs|table|tables"
    r")\b|\.(docx?|pdf|xlsx?|pptx?|csv|txt|md)\b",
    re.IGNORECASE,
)

# Artifact-type nouns + filler / instruction words stripped from a query before
# matching it against document filenames, so "summarize the X file and give me
# key actions" matches a file named "X …" on its *distinctive* tokens (X) and
# not on "summarize", "file", or "actions".
_ARTIFACT_WORDS = frozenset(
    {
        "file",
        "files",
        "document",
        "documents",
        "doc",
        "docs",
        "report",
        "reports",
        "policy",
        "policies",
        "playbook",
        "playbooks",
        "guide",
        "guides",
        "guidelines",
        "memo",
        "memos",
        "deck",
        "decks",
        "spreadsheet",
        "spreadsheets",
        "sheet",
        "sheets",
        "template",
        "templates",
        "script",
        "scripts",
        "manual",
        "manuals",
        "sop",
        "sops",
        "checklist",
        "checklists",
        "handbook",
        "handbooks",
        "attachment",
        "attachments",
        "docx",
        "pdf",
        "xlsx",
        "pptx",
        "csv",
        "txt",
        "md",
    }
)

_TITLE_STOPWORDS = _ARTIFACT_WORDS | frozenset(
    {
        # instruction verbs
        "summarize",
        "summarise",
        "give",
        "list",
        "show",
        "find",
        "open",
        "read",
        "pull",
        "get",
        "tell",
        "provide",
        "explain",
        "describe",
        "fetch",
        "retrieve",
        "search",
        "look",
        "recap",
        "outline",
        "draft",
        "review",
        "share",
        "send",
        "create",
        "generate",
        # filler / function words
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "for",
        "to",
        "me",
        "my",
        "our",
        "us",
        "in",
        "on",
        "at",
        "by",
        "about",
        "please",
        "can",
        "could",
        "would",
        "you",
        "your",
        "i",
        "we",
        "with",
        "from",
        "this",
        "that",
        "these",
        "those",
        "all",
        "any",
        "some",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "how",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "their",
        "his",
        "her",
        "into",
        "up",
        "out",
        "do",
        "does",
        "need",
        "want",
        "like",
        "regarding",
        "related",
        # instruction nouns ("give me key actions / takeaways / a summary")
        "key",
        "action",
        "actions",
        "item",
        "items",
        "point",
        "points",
        "takeaway",
        "takeaways",
        "summary",
        "detail",
        "details",
        "info",
        "information",
        "perform",
        "step",
        "steps",
    }
)


def _title_tokens(query: str) -> list[str]:
    """Distinctive lowercase tokens from a query, with filler/instruction and
    artifact words removed — the candidate title words to match a filename on."""
    raw = re.split(r"[^a-z0-9]+", (query or "").lower())
    return [t for t in raw if len(t) >= 2 and t not in _TITLE_STOPWORDS]


# A filename's identity tokens are the words a person actually says when they
# refer to the file. Export/version noise isn't part of the name: nobody calls
# "Client_Acronyms_2026-09-02.pdf" anything but "the client acronym list".
_TITLE_NOISE_TOKENS = _ARTIFACT_WORDS | frozenset(
    {
        "final",
        "draft",
        "copy",
        "updated",
        "update",
        "revised",
        "rev",
        "version",
        "new",
        "old",
        "current",
        "latest",
        "export",
        "exported",
        "internal",
        # month names, which date-stamped exports carry
        "jan",
        "january",
        "feb",
        "february",
        "mar",
        "march",
        "apr",
        "april",
        "may",
        "jun",
        "june",
        "jul",
        "july",
        "aug",
        "august",
        "sep",
        "sept",
        "september",
        "oct",
        "october",
        "nov",
        "november",
        "dec",
        "december",
    }
)


#: How many query words may be tried as a server-side filename filter before
#: giving up and letting semantic search take the question. Each is one cheap
#: substring query against the (small) assets table.
_MAX_TITLE_PROBES = 4


def _fold(token: str) -> str:
    """Fold a trailing plural so "acronyms" and "acronym" match.

    Crude on purpose: it is applied to BOTH sides of every comparison, so all
    that matters is that the two sides fold the same way.
    """
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _fold_tokens(tokens) -> set[str]:
    return {_fold(t) for t in tokens}


def _filename_identity_tokens(filename: str) -> set[str]:
    """The tokens that NAME a document, taken from its filename.

    Drops artifact words, pure numbers and version/date noise, so
    ``Client_Acronyms_2026-09-02.pdf`` identifies as ``{client, acronym}`` —
    exactly the words in "the Client Acronym List".
    """
    raw = {t for t in re.split(r"[^a-z0-9]+", (filename or "").lower()) if t}
    return _fold_tokens(
        t
        for t in raw
        if len(t) >= 2
        and t not in _TITLE_NOISE_TOKENS
        and not t.isdigit()
        and not re.fullmatch(r"v\d+", t)
    )


def _clip_with_truncation_notice(text: str, limit: int | None, *, label: str) -> str:
    """Return ``text``, or a clipped copy that SAYS it was clipped.

    Silent truncation is how a lookup question gets a false answer: asked for
    one row of a 400-row glossary export, the model was handed the first slice
    of the table and reported every row past the cut as missing.
    Whenever content is withheld, the model is told so in the text it reads,
    in the terms that matter — absence here is not absence from the document.
    """
    if not text or not limit or limit <= 0 or len(text) <= limit:
        return text
    return (
        text[:limit] + f"\n\n[TRUNCATED: this is the first {limit:,} of {len(text):,} "
        f'characters of "{label}". The remainder was NOT retrieved. Anything '
        "you do not see here may still appear later in the document — do NOT "
        "tell the user an entry is missing based on this partial text; say you "
        "could only read part of the document and offer to open it.]"
    )


def _resolve_named_documents(
    query: str, user_email: str, *, limit: int = 3, max_scan: int = 60
) -> list:
    """Resolve documents the user named directly, by filename, access-checked.

    Returns the matching ``AssetMetadata`` objects (most-confident first), or
    ``[]`` when the query doesn't reference a specific artifact, no confident
    filename match exists, or the user can't access the match. Fully defensive:
    any failure returns ``[]`` so the caller falls back to semantic search.

    Two ways a document qualifies, because there are two ways people name one:

    * The query is (nearly) all filename — "summarize the Sales End User UAT
      file". Measured as coverage of the QUERY's distinctive tokens.
    * The query names the document and then asks about something INSIDE it —
      "the acronym for Globex Industries from the Client Acronym List". Here
      the row words ("globex", "industries") are most of the query, so query
      coverage is low by construction; what identifies the file is that the
      query contains the document's whole NAME. Measured as coverage of the
      FILENAME's identity tokens, which is the test that was missing.
    """
    try:
        distinctive = _title_tokens(query)
        # Need at least two distinctive tokens so a lone keyword ("the policy")
        # can't pull an arbitrary file — that stays a semantic-search question.
        if len(distinctive) < 2:
            return []
        # An explicit artifact word ("file"/"document"/…) lets us relax the match
        # threshold; without one we require an *exact* all-token filename hit, so
        # the agent rephrasing "the X file" down to just "X" still resolves.
        has_artifact = bool(query and _DOC_REFERENCE_RE.search(query))

        from rubik_core.auth.access import has_document_access
        from rubik_core.auth.registry import UserRegistry
        from rubik_core.dam.registry import DAMRegistry

        user = UserRegistry().get_user(user_email)
        if not user:  # no DB record → fail closed, no direct doc access
            return []

        registry = DAMRegistry()

        # Folded (singular/plural-insensitive) so "the client acronym list"
        # reaches a file named "Client_Acronyms_….pdf".
        needed = _fold_tokens(distinctive)

        def _score_candidate(doc_id: str, dto: dict):
            """Sort tuple for a candidate, or ``None`` if it isn't a confident,
            accessible match."""
            fname = (dto.get("original_filename") or "").lower()
            if not fname:
                return None
            # Meeting transcripts are owned by search_past_meetings, not here.
            if (dto.get("topic") or "") == "Meeting Transcripts":
                return None
            fname_tokens = _fold_tokens(t for t in re.split(r"[^a-z0-9]+", fname) if t)
            overlap = needed & fname_tokens
            coverage = len(overlap) / len(needed)
            # Demand an unambiguous match. Every distinctive token in the
            # filename always qualifies; near-total coverage only counts when the
            # user explicitly named an artifact ("…file"/"…document"), keeping
            # long natural-language questions out of the direct-document path.
            exact = overlap == needed
            relaxed = has_artifact and coverage >= 0.8 and len(overlap) >= 2
            # …or the query contains this document's entire name. Two identity
            # tokens minimum, so a one-word filename ("Acronyms.pdf") can't be
            # pulled in by a passing mention and stays a semantic-search question.
            identity = _filename_identity_tokens(fname)
            titled = len(identity) >= 2 and identity <= needed
            if not (exact or relaxed or titled):
                return None
            asset = registry.get_asset(doc_id)
            if not asset or asset.status in ("deleted", "archived"):
                return None
            if not has_document_access(user, asset):
                return None
            return (len(identity & needed), coverage, -len(fname), asset)

        # Narrow server-side one token at a time, then refine on token coverage
        # in Python — a single substring match is robust to filename separators
        # ("user" ⊂ "end-user") in a way a multi-word substring is not. Probing
        # only the LONGEST token is what buried the Client Acronyms list: the
        # longest word in "the acronym for Globex Industries from the Client
        # Acronym List" is "industries", which appears in no filename, so the
        # named document was never even a candidate. Probes are folded (so
        # "%acronym%" finds "Acronyms"), run longest-first, and stop at the
        # first confident match — the ordinary case is still a single query.
        probes: list[str] = []
        for tok in sorted(distinctive, key=len, reverse=True):
            folded = _fold(tok)
            if folded not in probes:
                probes.append(folded)
            if len(probes) >= _MAX_TITLE_PROBES:
                break

        seen: set[str] = set()
        scored: list[tuple[int, float, int, object]] = []
        for probe in probes:
            listing = registry.list_assets(q=probe, limit=max_scan)
            candidates = listing.get("assets", {}) if isinstance(listing, dict) else {}
            for doc_id, dto in candidates.items():
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                hit = _score_candidate(doc_id, dto)
                if hit:
                    scored.append(hit)
            if scored:
                break

        # Most of the document's own name matched first, then query coverage; on
        # ties prefer the shorter (more specific) filename.
        scored.sort(key=lambda s: (-s[0], -s[1], -s[2]))
        return [asset for _, _, _, asset in scored[:limit]]
    except Exception as e:  # noqa: BLE001 — never let resolution break the tool
        logger.warning("Named-document resolution failed for %s: %s", user_email, e)
        return []


def _resolve_named_meeting(
    query: str, accessible_doc_ids, dam_registry, *, limit: int = 3
) -> list:
    """Resolve a specific past meeting the user named, by transcript filename.

    The meeting-transcript counterpart of :func:`_resolve_named_documents`. When
    the user names a meeting ("summarize the Sales / HubSpot Weekly Open Office"),
    a semantic search over the title text whiffs — the title words aren't in the
    transcript *body*, and the meeting competes against the whole company corpus,
    so its chunks rarely survive the distance threshold. So resolve it the same
    way the recency path does: by lookup, not vector search. Match the query's
    distinctive tokens against the filenames of the meetings the user actually
    attended — access-safe by construction, since only ``accessible_doc_ids`` are
    scanned. Returns matching ``AssetMetadata`` objects (best first), or ``[]``
    when the query doesn't confidently name a meeting on record.
    """
    try:
        distinctive = _title_tokens(query)
        # Need ≥2 distinctive tokens so a lone keyword ("the meeting") can't pull
        # an arbitrary transcript — that stays a topical/recency question.
        if len(distinctive) < 2:
            return []
        needed = set(distinctive)
        scored: list[tuple[float, datetime, object]] = []
        for doc_id in accessible_doc_ids:
            asset = dam_registry.get_asset(doc_id)
            if not asset or asset.status == "deleted":
                continue
            fname = (asset.original_filename or "").lower()
            if not fname:
                continue
            fname_tokens = {t for t in re.split(r"[^a-z0-9]+", fname) if t}
            overlap = needed & fname_tokens
            # Demand a strong, unambiguous title hit: ≥2 query tokens present and
            # most of the query covered. Topical questions ("what did we decide
            # about pricing") don't cover a single meeting's *title* this well, so
            # they fall through to semantic search.
            if len(overlap) < 2:
                continue
            coverage = len(overlap) / len(needed)
            if coverage < 0.6:
                continue
            scored.append((coverage, asset.created_at or datetime.min, asset))
        if not scored:
            return []
        # Keep only near-best matches (so a recurring same-title meeting returns
        # every instance, but a weaker coincidental hit is dropped), newest first.
        best = max(s[0] for s in scored)
        keep = [s for s in scored if s[0] >= best - 0.15]
        keep.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [asset for _, _, asset in keep[:limit]]
    except Exception as e:  # noqa: BLE001 — never let resolution break the tool
        logger.warning("Named-meeting resolution failed: %s", e)
        return []


def _read_asset_document_text(
    source_path: str | None,
    original_filename: str | None,
    max_chars: int | None = None,
) -> str:
    """Best-effort extraction of a knowledge-base document's text from its stored
    source (``gs://`` or local path), for summarizing a file the user named.

    Handles ``.docx`` / ``.pdf`` via :class:`DocumentLoader` and plain text
    directly. Returns ``""`` when the bytes can't be read/parsed so the caller
    degrades to semantic search.

    Clipped to ``max_chars`` (default ``settings.KB_NAMED_DOC_MAX_CHARS``) to
    protect context — but a clip is ANNOUNCED in the returned text. The user
    named this document precisely because the answer is one line somewhere in
    it, and that line is as likely to be on page 9 as page 1: the old silent
    8,000-char cut is what made the assistant report the second half of a
    glossary as nonexistent.
    """
    if not source_path:
        return ""
    from rubik_core.config import settings

    limit = settings.KB_NAMED_DOC_MAX_CHARS if max_chars is None else max_chars
    label = original_filename or source_path
    import tempfile
    from pathlib import Path

    suffix = Path(original_filename or source_path).suffix.lower()
    tmp_path: str | None = None
    try:
        from rubik_core.tenancy.storage import assert_own_uri

        checked = assert_own_uri(source_path)
        if source_path.startswith("gs://"):
            from google.cloud import storage

            from rubik_core.config import settings

            rest = checked[len("gs://") :]
            bucket_name, _, blob_name = rest.partition("/")
            if not blob_name:
                return ""
            client = storage.Client(project=settings.GCP_PROJECT_ID)
            blob = client.bucket(bucket_name).blob(blob_name)
            if not blob.exists():
                return ""
            # Plain-text formats: skip the temp-file round-trip.
            if suffix in (".txt", ".md", ".csv"):
                return _clip_with_truncation_notice(
                    blob.download_as_text(), limit, label=label
                )
            fd, tmp_path = tempfile.mkstemp(suffix=suffix or ".bin")
            os.close(fd)
            blob.download_to_filename(tmp_path)
            local_path = tmp_path
        elif os.path.exists(checked):
            local_path = checked
        else:
            return ""

        from rubik_core.ingestion.loader import DocumentLoader

        raw = DocumentLoader().load_file(Path(local_path))
        text = (raw.text if raw else "") or ""
        return _clip_with_truncation_notice(text, limit, label=label)
    except Exception as e:  # noqa: BLE001 — never let a read failure break the tool
        logger.warning("Could not extract document text from %s: %s", source_path, e)
        return ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# Returned by search_knowledge_base when retrieval found nothing relevant, or
# only weak/tangential hits. status="low_confidence" (not "success") tells the
# model this is a real "I don't have that" — the root-cause fix for the
# "Migration Plan" hallucination, where a nonexistent doc returned 5 loosely
# matching chunks shaped as success. See project_chat_hallucination_rca.
_RAG_LOW_CONFIDENCE_REPORT = (
    "No sufficiently relevant documents were found for this query. Do not "
    "invent or infer document names, projects, or contents — tell the user you "
    "don't have a document covering this. This is a RETRIEVAL miss and says "
    "NOTHING about what any document contains: if the user named a specific "
    "document, tell them you could not retrieve it — never tell them the entry "
    "they asked about is absent from it."
)


def _filter_rag_hits_by_score(scores: list[float]) -> tuple[list[bool], bool]:
    """Decide which knowledge-base hits are strong enough to surface.

    Vertex `retrieval_query` returns each context's vector distance in
    ``score`` (LOWER = closer). We only trust that reading when the observed
    scores are internally consistent with distances under the active
    ``vector_distance_threshold`` — i.e. all within it. If they look like
    similarities (any value above the threshold), or are unpopulated (all 0.0),
    we FAIL OPEN: keep every hit and never flag low-confidence, so a metric
    surprise can never invert the gate and suppress good results. The raw
    scores are logged upstream (RagManager.query) for calibration.

    Returns ``(keep_mask, low_confidence)`` where ``keep_mask[i]`` is True to
    surface ``hits[i]`` and ``low_confidence`` is True when NO hit is strong.
    """
    from rubik_core.config import settings

    if not scores:
        return [], False

    threshold = settings.RAG_DISTANCE_THRESHOLD
    weak = settings.RAG_WEAK_HIT_DISTANCE
    populated = [s for s in scores if s and s > 0.0]

    # No usable scores, or scores that don't behave like distances under the
    # threshold → fail open (surface everything, don't gate).
    if not populated or any(s > threshold + 1e-6 for s in populated):
        return [True] * len(scores), False

    keep = [s <= weak for s in scores]
    return keep, not any(keep)


class AgentTools:
    """Container for the chat assistant's tools bound to a specific user context."""

    def __init__(self, user_email: str):
        self.user_email = user_email

    # ─── Calendar Tools ──────────────────────────────────────────────────────

    def calendar_list_events(self, max_results: int = 10) -> dict:
        """List the user's upcoming Google Calendar events.

        Args:
            max_results: Maximum number of events to return (default 10).

        Returns:
            dict: status and a report containing the events or an error message.
        """
        from rubik_core.tools.calendar import GoogleCalendarClient

        try:
            client = GoogleCalendarClient(user_email=self.user_email)
            events = client.get_upcoming_events(max_results=max_results)
            # An empty list only means "no events" when the read actually
            # succeeded. If auth/config failed or the API errored, report an
            # error — never tell the user their calendar is empty during an
            # outage (that is a confidently-wrong answer).
            outage = client.init_error or client.last_error
            if not events and outage:
                return {
                    "status": "error",
                    "error_message": (
                        f"Calendar is temporarily unavailable ({outage}). "
                        "This does NOT mean the calendar is empty — do not tell "
                        "the user they have no events."
                    ),
                }
            if not events:
                return {"status": "success", "report": "No upcoming events found."}

            lines = []
            for ev in events:
                start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get(
                    "date", "?"
                )
                end = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get(
                    "date", ""
                )
                summary = ev.get("summary", "(No title)")
                attendees = ", ".join(
                    a.get("email", "") for a in ev.get("attendees", [])
                )
                lines.append(
                    f"- {summary}  |  {start} → {end}"
                    + (f"  |  Attendees: {attendees}" if attendees else "")
                )

            return {"status": "success", "report": "\n".join(lines)}
        except Exception as e:
            logger.error("calendar_list_events error: %s", e)
            return {"status": "error", "error_message": str(e)}

    def calendar_check_availability(
        self, start_iso: str, end_iso: str, emails: list[str] = None
    ) -> dict:
        """Check whether the user and optional attendees are free or busy between two ISO-8601 timestamps.

        Args:
            start_iso: Start datetime in ISO 8601 format including the timezone offset (e.g. 2024-04-21T14:30:00-06:00). DO NOT use 'Z' unless the user is in UTC.
            end_iso: End datetime in ISO 8601 format including the timezone offset.
            emails: Optional list of email addresses to check. If empty, checks only the authenticated user.

        Returns:
            dict: status and a report with busy slots or an error message.
        """
        from rubik_core.tools.calendar import GoogleCalendarClient

        try:
            client = GoogleCalendarClient(user_email=self.user_email)
            result = client.get_availability(
                start_time=start_iso, end_time=end_iso, emails=emails
            )
            if "error" in result:
                return {"status": "error", "error_message": result["error"]}

            if result.get("is_busy"):
                busy_slots_by_email = result.get("busy_slots", {})
                slot_lines = []
                for email, slots in busy_slots_by_email.items():
                    if slots:
                        slot_lines.append(f"Busy slots for {email}:")
                        for s in slots:
                            slot_lines.append(f"  - {s.get('start')} → {s.get('end')}")
                return {
                    "status": "success",
                    "report": "There are conflicts during the requested period.\n"
                    + "\n".join(slot_lines),
                }
            return {
                "status": "success",
                "report": "All requested users are FREE during the requested period.",
            }
        except Exception as e:
            logger.error("calendar_check_availability error: %s", e)
            return {"status": "error", "error_message": str(e)}

    def calendar_create_event(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        description: str = "",
        attendees: list[str] = None,
    ) -> dict:
        """Create a new event on the user's Google Calendar.

        Use this to schedule meetings, blockers, or reminders.
        Requires valid ISO-8601 start and end times.

        Args:
            summary: Title of the event.
            start_time: Start time in ISO 8601 format, including the timezone offset (e.g. 2024-04-21T14:30:00-06:00). DO NOT use 'Z' unless the user is in UTC.
            end_time: End time in ISO 8601 format, including the timezone offset.
            description: Optional description or agenda.
            attendees: Optional list of email addresses to invite.

        Returns:
            dict: status and a report with the created event link or an error.
        """
        from rubik_core.tools.calendar import GoogleCalendarClient

        try:
            client = GoogleCalendarClient(user_email=self.user_email)
            event = client.create_event(
                summary=summary,
                start_time=start_time,
                end_time=end_time,
                description=description,
                attendees=attendees,
            )
            if "error" in event:
                return {"status": "error", "error_message": event["error"]}

            html_link = event.get("htmlLink", "")
            return {
                "status": "success",
                "report": f"Event '{summary}' created successfully!\nLink: {html_link}",
            }
        except Exception as e:
            logger.error("calendar_create_event error: %s", e)
            return {"status": "error", "error_message": str(e)}

    def check_person_availability(self, name_or_email: str) -> dict:
        """Check whether a coworker is out of office / on PTO, and when they return.

        Use this for questions like "is <person> out this week?", "is <person>
        available today?", or "when is <person-email> back?". It resolves a name to an
        email via the directory when needed, then reports that person's
        out-of-office status from the daily-refreshed availability table, with a
        live calendar fallback.

        For a SPECIFIC time slot ("are they free 2-3pm Thursday?") use
        calendar_check_availability instead — this tool answers "are they off /
        when do they return", not slot-level conflicts.

        Args:
            name_or_email: The coworker's name or email address.

        Returns:
            dict: status and a report describing their OOO status.
        """
        from datetime import date as _date
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        def _fmt(iso: str) -> str:
            try:
                d = _date.fromisoformat(iso[:10])
                return f"{d.strftime('%A, %b')} {d.day}"
            except ValueError:
                return iso

        try:
            email = name_or_email.strip()
            display = email
            if "@" not in email:
                from rubik_core.tools.directory import GoogleDirectoryClient

                users = GoogleDirectoryClient().search_users(email)
                if not users:
                    return {
                        "status": "success",
                        "report": f"No employee found matching '{name_or_email}'.",
                    }
                if len(users) > 1:
                    opts = ", ".join(
                        f"{u.get('name', {}).get('fullName', '?')} "
                        f"({u.get('primaryEmail')})"
                        for u in users[:5]
                    )
                    return {
                        "status": "success",
                        "report": (
                            f"Multiple people match '{name_or_email}': {opts}. "
                            "Ask the user which one they mean."
                        ),
                    }
                email = users[0].get("primaryEmail") or ""
                display = users[0].get("name", {}).get("fullName") or email
                if not email:
                    return {
                        "status": "error",
                        "error_message": "resolved user has no email",
                    }

            today = _dt.now(_tz.utc).date()

            # Prefer the materialised table (cheap, org-wide, daily-refreshed);
            # fall back to a live calendar read if this person isn't in it yet.
            try:
                from rubik_core.availability import store as ooo_store
                from rubik_core.database.db import SessionLocal

                with SessionLocal() as db:
                    row = ooo_store.get_user_ooo_on(db, email, today)
                    nxt = None if row else ooo_store.get_user_next_ooo(db, email, today)
                if row is not None:
                    return {
                        "status": "success",
                        "report": (
                            f"{display} is out of office today — out through "
                            f"{_fmt(row.end_date)}, back {_fmt(row.return_date)}."
                        ),
                    }
                if nxt is not None:
                    return {
                        "status": "success",
                        "report": (
                            f"{display} is in today. Next out of office: "
                            f"{_fmt(nxt.start_date)} – {_fmt(nxt.end_date)}, "
                            f"back {_fmt(nxt.return_date)}."
                        ),
                    }
            except Exception:
                logger.warning(
                    "check_person_availability: OOO table read failed", exc_info=True
                )

            # Live fallback (table empty / not yet refreshed for this person).
            from rubik_core.availability.pto import detect_pto
            from rubik_core.priorities.temporal import resolve_tz

            st = detect_pto(email, today=today, tz_name=resolve_tz()[1])
            if st.on_pto_today and st.current is not None:
                c = st.current
                return {
                    "status": "success",
                    "report": (
                        f"{display} is out of office today — out through "
                        f"{c.end.strftime('%A, %b')} {c.end.day}, back "
                        f"{c.return_date.strftime('%A, %b')} {c.return_date.day}."
                    ),
                }
            if st.upcoming:
                p = st.upcoming[0]
                return {
                    "status": "success",
                    "report": (
                        f"{display} is in today. Next out of office: "
                        f"{p.start.strftime('%b')} {p.start.day} – "
                        f"{p.end.strftime('%b')} {p.end.day}, back "
                        f"{p.return_date.strftime('%A, %b')} {p.return_date.day}."
                    ),
                }
            return {
                "status": "success",
                "report": f"{display} has no out-of-office scheduled right now.",
            }
        except Exception as e:
            logger.error("check_person_availability error: %s", e)
            return {"status": "error", "error_message": str(e)}

    def google_directory_search_user(self, name_or_email: str) -> dict:
        """Search the organization's directory for an employee's email and details.

        Use this tool when you need to find the email address of a person
        mentioned only by name (e.g. "<full name>") or to verify if someone
        works in the organization.

        Args:
            name_or_email: The name or email of the person to search for.

        Returns:
            dict: status and a report with the found employee(s) details.
        """
        from rubik_core.tools.directory import GoogleDirectoryClient

        try:
            client = GoogleDirectoryClient()
            users = client.search_users(name_or_email)
            if not users:
                return {
                    "status": "success",
                    "report": f"No employee found in the directory matching '{name_or_email}'.",
                }

            report_lines = [f"Found {len(users)} match(es) in the directory:"]
            for u in users:
                display_name = u.get("name", {}).get("fullName", "Unknown Name")
                primary_email = u.get("primaryEmail", "No Email")
                dept = u.get("organizations", [{}])[0].get("department", "General")
                report_lines.append(
                    f"- {display_name} ({primary_email}) | Dept: {dept}"
                )

            return {
                "status": "success",
                "report": "\n".join(report_lines),
            }
        except Exception as e:
            logger.info("google_directory_search_user error: %s", e)
            return {"status": "error", "error_message": str(e)}

    # ─── Gmail Tools ─────────────────────────────────────────────────────────

    def gmail_search(self, query: str = "", max_results: int = 20) -> dict:
        """Search the user's Gmail threads using standard Gmail search query syntax.

        For recent inbox emails use an empty query. For specific searches use
        Gmail operators like 'from:<sender>' or 'subject:<keyword>'.

        Args:
            query: Gmail search query string. Empty string returns recent threads.
            max_results: Maximum number of threads to return (default 5).

        Returns:
            dict: status and a report summarising matching threads or an error.
        """
        from rubik_core.tools.gmail import GmailClient

        try:
            client = GmailClient(user_email=self.user_email)
            threads = client.search_threads(query=query, max_results=max_results)
            if not threads:
                return {
                    "status": "success",
                    "report": "No matching email threads found.",
                }

            lines = []
            for t in threads:
                thread_id = t.get("id", "?")
                snippet = t.get("snippet", "")
                msgs = t.get("messages", [])
                subject = ""
                sender = ""
                for m in msgs[:1]:
                    headers = m.get("payload", {}).get("headers", [])
                    for h in headers:
                        if h["name"].lower() == "subject":
                            subject = h["value"]
                        if h["name"].lower() == "from":
                            sender = h["value"]
                lines.append(
                    f"- Thread {thread_id}: **{subject}** from {sender}\n  Snippet: {snippet[:120]}"
                )

            return {"status": "success", "report": "\n".join(lines)}
        except Exception as e:
            logger.error("gmail_search error: %s", e)
            return {"status": "error", "error_message": str(e)}

    def gmail_get_thread(self, thread_id: str) -> dict:
        """Retrieve the full messages for a specific Gmail thread.

        ONLY call this when you ALREADY have a concrete `thread_id` — either
        because the user provided one explicitly, or because a previous
        `gmail_search` / `gmail_summarize_unread` call returned one. NEVER ask
        the user to paste a thread ID; if they describe a topic or sender
        instead, call `gmail_search` first to locate it.

        Args:
            thread_id: The Gmail thread ID to retrieve.

        Returns:
            dict: status and a report containing the thread messages or an error.
        """
        from rubik_core.tools.gmail import GmailClient

        try:
            client = GmailClient(user_email=self.user_email)
            thread = client.get_thread(thread_id=thread_id)
            msgs = thread.get("messages", [])
            lines = []
            for m in msgs:
                headers = m.get("payload", {}).get("headers", [])
                hdr = {h["name"].lower(): h["value"] for h in headers}
                snippet = m.get("snippet", "")
                lines.append(
                    f"From: {hdr.get('from', '?')}  |  Date: {hdr.get('date', '?')}\n"
                    f"Subject: {hdr.get('subject', '?')}\n"
                    f"{snippet}\n---"
                )
            return {"status": "success", "report": "\n".join(lines)}
        except Exception as e:
            logger.error("gmail_get_thread error: %s", e)
            return {"status": "error", "error_message": str(e)}

    def gmail_summarize_unread(self, hours: int = 0, max_results: int = 15) -> dict:
        """Summarize the user's unread emails, one sentence each, with action-required flags.

        Use this for prompts like "summarize my unread emails", "what did I miss
        overnight", "while I was gone", or "give me an inbox digest". For
        "find emails from <person/company>", prefer `gmail_search` instead.

        Args:
            hours: If > 0, only include emails received within the last N hours.
                   Pass 0 to include all unread.
            max_results: Maximum number of emails to summarize (default 15).

        Returns:
            dict: status and a markdown report listing each unread email with a
            one-sentence summary and an [ACTION REQUIRED] tag where applicable.
        """
        import asyncio
        from datetime import datetime, timedelta

        from backend.services.gmail_service import GmailService

        try:
            service = GmailService(user_email=self.user_email)
            since = (
                datetime.now(timezone.utc) - timedelta(hours=hours)
                if hours and hours > 0
                else None
            )
            messages = service.get_unread(since=since, max_results=max_results)
            if not messages:
                return {"status": "success", "report": "No unread emails."}

            first_name = self.user_email.split("@")[0]
            try:
                summaries = asyncio.run(
                    service.summarize_messages(messages, user_first_name=first_name)
                )
            except RuntimeError:
                # Already inside a running loop (ADK runner) — schedule on a worker thread.
                with ContextThreadPoolExecutor(max_workers=1) as pool:
                    summaries = pool.submit(
                        lambda: asyncio.run(
                            service.summarize_messages(
                                messages, user_first_name=first_name
                            )
                        )
                    ).result()

            lines = [f"Unread emails ({len(summaries)}):"]
            for s in summaries:
                tag = " [ACTION REQUIRED]" if s.action_required else ""
                lines.append(
                    f"- **{s.subject}** — {s.sender}{tag}\n  {s.summary}\n  Thread: {s.thread_id}"
                )
            return {"status": "success", "report": "\n".join(lines)}
        except Exception as e:
            logger.error("gmail_summarize_unread error: %s", e)
            return {"status": "error", "error_message": str(e)}

    def gmail_create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
    ) -> dict:
        """Create a draft email in the user's Gmail. NEVER sends the email.

        Args:
            to: Comma-separated list of recipient email addresses.
            subject: Email subject line.
            body: Email body text (plain text or markdown).
            cc: Optional comma-separated CC recipients.

        Returns:
            dict: status and a report with the draft URL or an error.
        """
        from rubik_core.tools.gmail import GmailClient

        try:
            client = GmailClient(user_email=self.user_email)
            to_list = [e.strip() for e in to.split(",") if e.strip()]
            cc_list = [e.strip() for e in cc.split(",") if e.strip()] if cc else []
            result = client.create_draft(
                to=to_list, subject=subject, body_markdown=body, cc=cc_list
            )
            draft_url = result.get("gmail_draft_url", "")
            return {
                "status": "success",
                "report": f"Draft created successfully.\nDraft URL: {draft_url}",
            }
        except Exception as e:
            logger.error("gmail_create_draft error: %s", e)
            return {"status": "error", "error_message": str(e)}

    # ─── Meeting Transcript / RAG Tools ─────────────────────────────────────

    async def search_past_meetings(self, query: str, max_results: int = 5) -> dict:
        """Search or recall the user's past Google Meet / Fathom transcripts.

        Use this for ALL of:
        - Topic searches ("what did we decide about <topic>?"),
        - A meeting named by title ("summarize the <meeting title>",
          "recap the <meeting title> call") — resolved by matching the
          name against the meetings the user attended, so call the tool with the
          meeting name even though it won't appear in the transcript body, and
        - Recency requests ("summarize my last meeting", "what happened in my
          most recent call?") — these are resolved by meeting date, so you do
          NOT need to know today's date or pass it in; just call the tool with
          the user's question and the latest transcript(s) come back.

        Only meetings the user actually attended (per the meeting_participants
        table) are searched. The user can never see a meeting they were not
        part of, even if the vector store happens to return one.

        The returned text can include an "Action Items & Next Steps" and a
        "Meeting Summary" section (captured from Fathom at ingest). When you are
        drafting a follow-up email or recapping a call, treat every action item,
        to-do, and next step in the returned text as REQUIRED content — do not
        drop any. When the user asks for the FULL / COMPLETE / ALL notes of a
        meeting, reproduce the returned content completely instead of condensing
        it into a short summary.

        Args:
            query: Natural-language question about past meetings. May be a topic
                or a recency phrase like "my last meeting".
            max_results: Maximum number of meetings/excerpts to return.

        Returns:
            dict: status and a report with relevant meeting excerpts or an error.
        """

        from rubik_core.config import AppMode, settings
        from rubik_core.dam.registry import DAMRegistry
        from rubik_core.database.repository import SQLMeetingParticipantsRepository
        from rubik_core.models import get_embeddings

        dam_registry = DAMRegistry()
        participants_repo = SQLMeetingParticipantsRepository()

        # ── ACCESS BOUNDARY ─────────────────────────────────────────────────
        # Source of truth: the meeting_participants table. Every user — execs
        # included — is gated to the meetings they actually attended. If they
        # have no rows here, they query nothing. This is the security guarantee
        # that holds even if vector-store metadata is stale or wrong.
        accessible_doc_ids = set(
            participants_repo.list_doc_ids_for_user(self.user_email)
        )
        if not accessible_doc_ids:
            logger.info(
                "No accessible transcripts for %s; short-circuiting.", self.user_email
            )
            return {
                "status": "success",
                "report": "You have no past meetings on record.",
            }
        # ────────────────────────────────────────────────────────────────────

        # ── RECENCY PATH ────────────────────────────────────────────────────
        # "My last meeting" / "most recent call" can't be served by a semantic
        # search over the recency phrase (the words match no transcript content),
        # so resolve it by date — newest accessible transcript(s) first.
        if _is_recency_query(query):
            want_many = bool(_PLURAL_RECENCY_RE.search(query))
            limit = min(max_results, 5) if want_many else 1
            logger.info(
                "Recency meeting query for %s (limit=%d): %r",
                self.user_email,
                limit,
                (query or "")[:80],
            )
            return self._recent_meetings_report(participants_repo, dam_registry, limit)
        # ────────────────────────────────────────────────────────────────────

        # ── NAMED-MEETING PATH ──────────────────────────────────────────────
        # The user may name a specific meeting ("a summary of the Sales / HubSpot
        # Weekly Open Office"). A semantic search over the title whiffs — the
        # title words aren't in the transcript body and the meeting competes
        # against the whole company corpus — so resolve it by filename against the
        # meetings the user attended and return its text directly, the same
        # "resolve by lookup, not vector search" approach the recency path uses.
        named = _resolve_named_meeting(
            query, accessible_doc_ids, dam_registry, limit=min(max_results, 3)
        )
        if named:
            sections: list[str] = []
            for asset in named:
                title = asset.original_filename or asset.doc_id
                when = (
                    asset.created_at.strftime("%A, %B %d, %Y")
                    if asset.created_at
                    else ""
                )
                header = f"**{title}**" + (f" — {when}" if when else "")
                text = _read_transcript_text(asset.source_path)
                if text:
                    sections.append(f"{header}\n{text}")
                else:
                    sections.append(
                        f"{header}\n(The transcript is still processing or its text "
                        "is unavailable right now.)"
                    )
            if sections:
                logger.info(
                    "Named-meeting resolution for %s → %d transcript(s): %r",
                    self.user_email,
                    len(sections),
                    (query or "")[:80],
                )
                label = (
                    "Transcript for the meeting you named:"
                    if len(sections) == 1
                    else "Transcripts matching the meeting you named (newest first):"
                )
                return {
                    "status": "success",
                    "report": f"{label}\n\n" + "\n\n---\n\n".join(sections),
                }
        # ────────────────────────────────────────────────────────────────────

        if settings.APP_MODE == AppMode.GCP:
            from rubik_core.adapters.vertex_rag import VertexRagAdapter

            adapter = VertexRagAdapter()
        else:
            from rubik_core.adapters.chroma import ChromaAdapter

            adapter = ChromaAdapter()

        try:
            if settings.APP_MODE != AppMode.GCP:
                embeddings_model = get_embeddings()
                query_embedding = embeddings_model.embed_query(query)
                # Pre-filter to the caller's own transcripts; the
                # accessible_doc_ids gate below is the authoritative re-check.
                filters = {
                    "allowed_emails": {"$contains": f"|{self.user_email.lower()}|"}
                }
                raw_chunks = adapter.query(
                    query_embedding, top_k=max_results * 3, filters=filters
                )
            else:
                target_corpus_id = None
                from rubik_core.tenancy.corpora import corpus_id

                target_corpus_id = corpus_id("meetings")
                if not target_corpus_id:
                    return {
                        "status": "success",
                        "report": "No meeting transcripts found.",
                    }
                raw_chunks = adapter.query_rag_corpus(
                    query_text=query,
                    top_k=max_results * 3,
                    target_corpus_id=target_corpus_id,
                )

            safe_results = []
            for rc in raw_chunks:
                if len(safe_results) >= max_results:
                    break
                if settings.APP_MODE == AppMode.GCP:
                    source_uri = rc.chunk.metadata.get("source_path", "")
                    if not source_uri:
                        continue
                    filename = source_uri.split("/")[-1]
                    doc_id = filename[:-4] if filename.endswith(".txt") else filename
                else:
                    doc_id = rc.chunk.metadata.get("doc_id") or ""
                    # Chroma chunks may not carry doc_id; fall back to source path heuristic.
                    if not doc_id:
                        sp = rc.chunk.metadata.get("source_path", "")
                        if sp:
                            base = sp.split("/")[-1]
                            doc_id = base[:-4] if base.endswith(".txt") else base

                # ── ACCESS BOUNDARY ─────────────────────────────────────────
                # Re-check every retrieved chunk against the participants table.
                # Defense in depth: vector store filters can drift from SQL truth.
                if not doc_id or doc_id not in accessible_doc_ids:
                    logger.warning(
                        "Filtered out chunk for non-participant access: user=%s doc_id=%s",
                        self.user_email,
                        doc_id,
                    )
                    continue
                # ────────────────────────────────────────────────────────────

                asset = dam_registry.get_asset(doc_id)
                if not asset or asset.status == "deleted":
                    continue
                title = asset.original_filename or doc_id
                # Stamp each excerpt with its meeting date. The named/recency
                # paths already do; this semantic path did not — so a chunk from
                # a meeting two years ago read as the present, which is exactly
                # how a briefing called a long-won engagement a "previous
                # unsuccessful bid". created_at is our best meeting-date proxy
                # (transcripts ingest right after the meeting ends).
                when = (
                    asset.created_at.strftime("%A, %B %d, %Y")
                    if getattr(asset, "created_at", None)
                    else "date unknown"
                )
                safe_results.append(
                    f"**{title}** — {when}\n{rc.chunk.text[:SEMANTIC_CHUNK_MAX_CHARS]}"
                )

            if not safe_results:
                # Topical search whiffed (often because the distance threshold is
                # tight). Don't dead-end — point the user at their most recent
                # meeting so the agent can offer to summarize it or refine terms.
                recent_ids = participants_repo.list_recent_doc_ids_for_user(
                    self.user_email, limit=1
                )
                for doc_id in recent_ids:
                    asset = dam_registry.get_asset(doc_id)
                    if not asset or asset.status == "deleted":
                        continue
                    title = asset.original_filename or doc_id
                    when = (
                        asset.created_at.strftime("%B %d, %Y")
                        if asset.created_at
                        else ""
                    )
                    return {
                        "status": "success",
                        "report": (
                            "No past meeting closely matched that topic. For "
                            f"reference, the most recent meeting on record is "
                            f"**{title}**" + (f" ({when})" if when else "") + ". "
                            "Ask me to summarize it, or try different keywords."
                        ),
                    }
                return {
                    "status": "success",
                    "report": "No relevant past meetings found.",
                }

            return {
                "status": "success",
                "report": (
                    "Relevant excerpts from past meeting transcripts. Each is "
                    "dated — treat every excerpt as historical (the state as of "
                    "that date), not the current situation, unless a newer source "
                    "says otherwise:\n\n" + "\n\n---\n\n".join(safe_results)
                ),
            }
        except Exception as e:
            logger.error("search_past_meetings error: %s", e)
            return {"status": "error", "error_message": str(e)}

    def _recent_meetings_report(
        self, participants_repo, dam_registry, limit: int
    ) -> dict:
        """Return the user's most recent meeting transcript(s) by date.

        Used for recency-style questions ("my last meeting") where a semantic
        search over the recency phrase returns nothing useful. Bypasses the
        vector store entirely — recency is decided by Asset.created_at, the
        best available proxy for the meeting date (Fathom/Drive sync ingests a
        transcript right after the meeting ends). ACL-scoped to meetings the
        user attended (execs included — they are not privileged here).
        """
        recent_ids = participants_repo.list_recent_doc_ids_for_user(
            self.user_email, limit=limit
        )
        sections: list[str] = []
        for doc_id in recent_ids:
            asset = dam_registry.get_asset(doc_id)
            if not asset or asset.status == "deleted":
                continue
            title = asset.original_filename or doc_id
            when = (
                asset.created_at.strftime("%A, %B %d, %Y") if asset.created_at else ""
            )
            header = f"**{title}**" + (f" — {when}" if when else "")
            text = _read_transcript_text(asset.source_path)
            if text:
                sections.append(f"{header}\n{text}")
            else:
                sections.append(
                    f"{header}\n(The transcript is still processing or its text is "
                    "unavailable right now.)"
                )

        if not sections:
            return {
                "status": "success",
                "report": "You have no past meetings on record.",
            }

        label = (
            "Most recent meeting on record:"
            if len(sections) == 1
            else "Most recent meetings on record (newest first):"
        )
        return {
            "status": "success",
            "report": f"{label}\n\n" + "\n\n---\n\n".join(sections),
        }

    def get_daily_briefing(self) -> dict:
        """Retrieve the user's daily briefing / homefeed card for today.

        Returns:
            dict: status and a report with the daily briefing content or a message.
        """
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import HomeCard
        from rubik_core.priorities.cached_visibility import render_cached_briefing
        from rubik_core.priorities.temporal import user_today_str

        # Local-date key to match persist_bundle's local-date write key — a UTC
        # date here would miss today's card in the evening-Pacific window.
        today_str = user_today_str(self.user_email)
        try:
            with SessionLocal() as db:
                card = (
                    db.query(HomeCard)
                    .filter(
                        HomeCard.user_email == self.user_email,
                        HomeCard.date == today_str,
                        HomeCard.category == "daily_briefing",
                    )
                    .first()
                )

                if card:
                    try:
                        from rubik_core.control.repository import module_enabled

                        eos_enabled = module_enabled("eos")
                    except Exception:  # noqa: BLE001 - control failure hides EOS
                        eos_enabled = False
                    content = render_cached_briefing(
                        card.metadata_json,
                        today_str,
                        eos_enabled=eos_enabled,
                    )
                    if content is None:
                        return {
                            "status": "success",
                            "report": (
                                f"No verifiable daily briefing is available for "
                                f"{today_str} yet."
                            ),
                        }
                    return {
                        "status": "success",
                        "report": f"Daily briefing for {today_str}:\n\n{content}",
                    }
                return {
                    "status": "success",
                    "report": f"No daily briefing available for {today_str} yet.",
                }
        except Exception as e:
            logger.error("get_daily_briefing error: %s", e)
            return {"status": "error", "error_message": str(e)}

    # ─── CRM / Revenue Tools ──────────────────────────────────────────────────

    async def search_hubspot_data(self, query: str) -> dict:
        """Query the LIVE HubSpot CRM for sales-pipeline and outreach data.

        Answers, optionally scoped to a timeframe and/or a rep (or to the caller via
        "my"/"me"):
          - KPIs: deals won / created / open (counts and $), win rate, average deal
            value, sales-cycle length, open pipeline value, deals expected / estimated
            to close in a period (the pipeline forecast), expected-to-close rate,
            meetings booked / completed, new contacts, activities logged, turn times
            by product.
          - OUTREACH / LAST-TOUCH (any entity): when a specific COMPANY, DEAL, or
            CONTACT was last contacted ("when did we last reach out to <client>",
            "how long since anyone contacted <company>", "last touch on the <X>
            deal"), and which of a rep's / the team's deals have gone cold (not
            contacted in the last N days, incl. which of the deals closing in a period
            are stale).
          - A specific deal's stage / amount / close date / owner ("what stage is the
            <X> deal in?", "who owns the <Y> renewal?").
          - DAILY SALES RHYTHM worklists, for the caller ("my") or their team ("my
            team", "across the team"): which deals closing this month need action
            today (top 3 / top 10), deals in a proposal / negotiation / contract
            stage waiting on the decision-maker's reply, deals whose close date has
            been pushed more than twice or with no activity in 7 days,
            single-threaded deals (only one contact) over a dollar value, lost deals
            worth a re-engagement check-in, contacts / deals created or updated
            today that are missing a job title, persona / buying role or next step,
            overdue HubSpot tasks and past-due close dates, and today's logged
            calls / emails / meetings / stage changes (the raw material for an
            end-of-day update to a manager — summarize it as bullets).
        Route ANY "when did we last contact / reach out to / follow up with / how long
        since we heard from <a specific company, person, or deal>" question HERE (not
        to the person contact-lookup tool, which only returns a person's details).
        Examples: "what did we close last month?", "when was the last touch with
        <company>?", "which of my deals have I not contacted in 10 days?",
        "what stage is the <deal/renewal> in?", "what's closing next quarter?".

        OUT OF SCOPE — this tool CANNOT do the following. Do not call it for them, and
        never imply HubSpot was consulted for them:
          - A client's NON-SALES attributes or vendor relationships — e.g. their
            document custodian, payroll provider, or IT vendor. Their sales /
            outreach facts (deals, stage, and when they were last contacted) ARE in
            scope; other attributes are not.
          - Company policies / internal documents → use `search_knowledge_base`.
          - Current events or external facts → use `search_web`.
        If a request is out of scope, say so and route to the right tool instead of
        guessing or answering as if HubSpot had the data.

        Args:
            query: Natural-language question about HubSpot sales KPIs / pipeline.

        Returns:
            dict: status and a report with the requested CRM metrics, or an error message.
        """
        # Query the LIVE HubSpot account via REST. This is the
        # source of truth — always current, and uses HubSpot's computed
        # hs_is_closed_won / hs_is_closed flags rather than hard-coded stage
        # strings (a portal may use custom pipeline stages). Requires the
        # current workspace's HubSpot read token.
        if (workspace_secret("hubspot", "read_token") or "").strip():
            from rubik_core.retrieval.hubspot_api import (
                HubSpotAPIError,
                HubSpotAPIRetriever,
            )

            try:
                # Offload the blocking HubSpot REST + genai _plan calls to a
                # thread so a HubSpot turn doesn't stall other concurrent chats.
                context_block, _debug = await asyncio.to_thread(
                    _run_blocking,
                    lambda: HubSpotAPIRetriever().retrieve(
                        query, caller_email=self.user_email
                    ),
                )
                if not context_block:
                    return {
                        "status": "success",
                        "report": "No matching HubSpot CRM records found for this query.",
                    }
                return {"status": "success", "report": context_block}
            except HubSpotAPIError as e:
                # Real API/transport problem — surface it; do NOT silently fall
                # back to the stale BigQuery mirror (that masks live outages).
                logger.error("HubSpot live API error: %s", e)
                return {"status": "error", "error_message": str(e)}

        return {
            "status": "error",
            "error_message": "HubSpot read token is not configured for this workspace",
        }

    async def find_hubspot_contact(self, name: str) -> dict:
        """Find a workspace-linked CONTACT in live HubSpot by name or email.

        Use this to find a specific person's contact details — email address, phone,
        company, or job title — for example to draft a follow-up to them: "what's
        <person>'s email?", "find the contact record for <person>", "look up
        <person-email>". It searches across BOTH name and email, so it works whether
        you have the person's name or only a partial email.

        This is DIFFERENT from `search_hubspot_data`, which answers aggregate
        sales-pipeline KPIs (deals, revenue, win rate) and CANNOT look up an
        individual's contact info. For "who is X / what's X's email / find X's record",
        use THIS tool. If you need someone's email to draft a message and don't have it,
        call this FIRST rather than telling the user you couldn't find them.

        Args:
            name: A person's name or email address (partial is fine), e.g. "<full name>".

        Returns:
            dict: status and a report listing matching contacts (name, email, phone,
                company, title), or a message that no contact matched.
        """
        term = (name or "").strip()
        if not term:
            return {
                "status": "error",
                "error_message": "No name or email was provided to look up.",
            }

        from rubik_core.tenancy.hubspot_config import get_hubspot_config

        try:
            pipelines = await asyncio.to_thread(
                _run_blocking, lambda: get_hubspot_config().pipeline_ids
            )
        except Exception as exc:  # noqa: BLE001 - control outage closes contact access
            logger.warning(
                "HubSpot workspace config unavailable (%s)", type(exc).__name__
            )
            return {
                "status": "error",
                "error_message": "HubSpot workspace settings are unavailable.",
            }
        if not pipelines:
            return {
                "status": "error",
                "error_message": "HubSpot deal pipelines are not configured for this workspace. An admin must choose them before contacts can be searched.",
            }

        try:
            token = workspace_secret("hubspot", "read_token")
        except Exception as exc:  # noqa: BLE001 - secret outage closes contact access
            logger.warning("HubSpot read token unavailable (%s)", type(exc).__name__)
            return {
                "status": "error",
                "error_message": "HubSpot contact lookup failed.",
            }
        if not (token or "").strip():
            return {
                "status": "error",
                "error_message": "HubSpot is not configured, so I can't look up contacts.",
            }

        from rubik_core.retrieval.hubspot_api import (
            CONTACT_SEARCH_PROPERTIES,
            HubSpotAPIClient,
        )

        try:
            results = await asyncio.to_thread(
                _run_blocking,
                lambda: HubSpotAPIClient().search_owned_objects(
                    "contacts", term, CONTACT_SEARCH_PROPERTIES, limit=10
                ),
            )
        except Exception as exc:  # noqa: BLE001 - no partial contact data on failure
            logger.warning("find_hubspot_contact failed (%s)", type(exc).__name__)
            return {
                "status": "error",
                "error_message": "HubSpot contact lookup failed.",
            }

        if not results:
            return {
                "status": "success",
                "report": f'No HubSpot contact matched "{term}".',
            }

        lines = [f'HubSpot contacts matching "{term}":']
        for i, c in enumerate(results, 1):
            p = c.get("properties", {}) or {}
            full_name = (
                f"{p.get('firstname') or ''} {p.get('lastname') or ''}".strip()
                or "(no name)"
            )
            parts = [f"{i}. {full_name}"]
            if p.get("email"):
                parts.append(f"email: {p['email']}")
            if p.get("phone"):
                parts.append(f"phone: {p['phone']}")
            if p.get("company"):
                parts.append(f"company: {p['company']}")
            if p.get("jobtitle"):
                parts.append(f"title: {p['jobtitle']}")
            lines.append(" | ".join(parts))
        return {"status": "success", "report": "\n".join(lines)}

    async def search_knowledge_base(self, query: str, top_k: int = 5) -> dict:
        """Search the company's general knowledge base and documents (policies, products, etc.).

        Use this for questions about company policies, product info, procedures,
        or any general document that is NOT a meeting transcript.

        Args:
            query: Natural-language search query.
            top_k: Number of results to return.

        Returns:
            dict: status and a report with relevant document excerpts or an error.
        """
        from rubik_core.config import AppMode, settings

        # ── Named-document path: resolve a directly-referenced file by title ──
        # "Summarize the Sales End User UAT file" can't be served by a semantic
        # search over the request phrase, and the file may sit in a corpus
        # outside this user's fan-out (e.g. one they own under another
        # department). When the query names a specific document, resolve it
        # against the DAM registry (access-checked) and read its text directly —
        # the same way recency meeting questions resolve by date, not by vector
        # search. Falls through when no confident, accessible match is found.
        named = _resolve_named_documents(query, self.user_email)
        if named:
            sections: list[str] = []
            # Budget shared across the (at most 3) resolved documents, so one
            # long file can't crowd out the others or the conversation.
            budget = settings.KB_NAMED_DOC_TOTAL_MAX_CHARS
            for asset in named:
                if budget <= 0:
                    break
                text = _read_asset_document_text(
                    asset.source_path,
                    asset.original_filename,
                    max_chars=min(settings.KB_NAMED_DOC_MAX_CHARS, budget),
                )
                if not text:
                    continue
                budget -= len(text)
                title = asset.original_filename or asset.doc_id
                # Use the **Source:** convention so the orchestrator's citation
                # extractor surfaces the filename in the chat "Sources" panel.
                sections.append(f"**Source:** {title}\n{text}")
            if sections:
                logger.info(
                    "search_knowledge_base: resolved %d named document(s) by "
                    "title for %s",
                    len(sections),
                    self.user_email,
                )
                return {"status": "success", "report": "\n\n---\n\n".join(sections)}
        # ─────────────────────────────────────────────────────────────────────

        # ── UC-1: prefer audience-shaped multi-corpus fan-out (GCP only) ────
        # Resolves the user's allowed RAGs (their dept + companywide; admins
        # see all) and queries them in a single retrieval_query call. If no
        # corpora exist yet, NoUC1CorporaError → fall through to legacy.
        if settings.APP_MODE == AppMode.GCP:
            from rubik_core.rag import (
                NoUC1CorporaError,
                RagManager,
                RagOperationError,
                rags_for_user_email,
            )

            try:
                allowed = rags_for_user_email(self.user_email)
                if not allowed:
                    logger.info(
                        "UC-1 RAG fan-out: user %s has no allowed RAGs; falling back",
                        self.user_email,
                    )
                else:
                    mgr = RagManager()
                    hits = mgr.query(query_text=query, rags=allowed, top_k=top_k)
                    if not hits:
                        return {
                            "status": "low_confidence",
                            "report": _RAG_LOW_CONFIDENCE_REPORT,
                        }
                    keep, low_conf = _filter_rag_hits_by_score([h.score for h in hits])
                    strong = [h for h, k in zip(hits, keep) if k]
                    if low_conf or not strong:
                        logger.info(
                            "search_knowledge_base: low-confidence result for %s "
                            "(q=%r, %d hit(s), none strong)",
                            self.user_email,
                            query[:80],
                            len(hits),
                        )
                        return {
                            "status": "low_confidence",
                            "report": _RAG_LOW_CONFIDENCE_REPORT,
                        }
                    results = []
                    for h in strong:
                        filename = (
                            h.source_uri.rsplit("/", 1)[-1]
                            if h.source_uri
                            else "unknown"
                        )
                        results.append(
                            f"**Source ({h.rag}):** {filename}\n"
                            + _clip_with_truncation_notice(
                                h.text,
                                settings.RAG_HIT_EXCERPT_CHARS,
                                label=filename,
                            )
                        )
                    return {"status": "success", "report": "\n\n---\n\n".join(results)}
            except NoUC1CorporaError as e:
                logger.info(
                    "UC-1 RAG fan-out unavailable (%s); falling back to legacy path", e
                )
            except RagOperationError as e:
                logger.warning(
                    "UC-1 RAG fan-out failed (%s); falling back to legacy path", e
                )

        from rubik_core.models import get_embeddings

        # ── Legacy: single-corpus VertexRagAdapter (pre-UC-1) ───────────────
        # Reached only when UC-1 corpora don't exist yet (NoUC1CorporaError).
        # Will be removed once the UC-1 corpora are fully validated.
        if settings.APP_MODE == AppMode.GCP:
            from rubik_core.adapters.vertex_rag import VertexRagAdapter

            adapter = VertexRagAdapter()
            try:
                raw_chunks = adapter.query_rag_corpus(query_text=query, top_k=top_k)
                keep, low_conf = _filter_rag_hits_by_score(
                    [rc.score for rc in raw_chunks]
                )
                strong = [rc for rc, k in zip(raw_chunks, keep) if k]
                if not strong or low_conf:
                    return {
                        "status": "low_confidence",
                        "report": _RAG_LOW_CONFIDENCE_REPORT,
                    }
                results = []
                for rc in strong:
                    source = rc.chunk.metadata.get("source_path") or "General Knowledge"
                    results.append(
                        f"**Source:** {source}\n"
                        + _clip_with_truncation_notice(
                            rc.chunk.text,
                            settings.RAG_HIT_EXCERPT_CHARS,
                            label=source,
                        )
                    )
                return {"status": "success", "report": "\n\n---\n\n".join(results)}
            except Exception as e:
                return {"status": "error", "error_message": str(e)}
        else:
            from rubik_core.adapters.chroma import ChromaAdapter

            adapter = ChromaAdapter()
            try:
                embeddings_model = get_embeddings()
                query_embedding = embeddings_model.embed_query(query)
                raw_chunks = adapter.query(query_embedding, top_k=top_k)
                results = []
                for rc in raw_chunks:
                    source = rc.chunk.metadata or "General Knowledge"
                    results.append(
                        f"**Source:** {source}\n"
                        + _clip_with_truncation_notice(
                            rc.chunk.text,
                            settings.RAG_HIT_EXCERPT_CHARS,
                            label=str(source),
                        )
                    )
                if not results:
                    return {
                        "status": "success",
                        "report": "No relevant documents found.",
                    }
                return {"status": "success", "report": "\n\n---\n\n".join(results)}
            except Exception as e:
                return {"status": "error", "error_message": str(e)}

    async def search_my_files(self, query: str, file_name: str = "") -> dict:
        """Search the files THIS person uploaded to Ask Cubie themselves: their own
        documents, PDFs, spreadsheets, decks and pictures. Only they can see these
        files; they are NOT in the company knowledge base, so search_knowledge_base
        never finds them.

        Use this when the person refers to a file they attached, uploaded, sent or
        shared with Cubie ("my file", "the PDF I uploaded", "that spreadsheet",
        "the screenshot I sent yesterday"), or when the turn lists their private
        files and the question could be about one of them. Files attached to the
        CURRENT message are already in the message: answer from those directly.

        Args:
            query: What to look for, in plain words. Leave it empty together with
                file_name to read the opening of that file.
            file_name: Optional. All or part of one file's name, to look only
                inside that file.

        Returns:
            dict: status and a report with excerpts, each under a **Source:** line.
        """
        from rubik_core.auth.registry import UserRegistry
        from rubik_core.chat_files import service as chat_files
        from rubik_core.database.db import SessionLocal

        profile = UserRegistry().get_user(self.user_email)
        if not chat_files.enabled_for(profile):
            return {
                "status": "error",
                "report": f"Files in Ask {PRODUCT_NAME} aren't switched on for this person.",
            }

        def _run() -> dict:
            with SessionLocal() as db:
                return chat_files.search_my_files(
                    db, self.user_email, query, file_name=file_name
                )

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:  # noqa: BLE001
            logger.warning("search_my_files failed for %s: %s", self.user_email, e)
            return {
                "status": "error",
                "report": "Their files couldn't be searched just now. Say so.",
            }

    def search_products(self, query: str, top_k: int = 5) -> dict:
        """Search the company's own product and service catalog: what it sells, descriptions, list prices and links.

        Use this for questions like "what is the price of <product>?", "what do we offer for <need>?", or "what products are available?".

        Args:
            query: Natural-language search query about products; empty lists the catalog.
            top_k: Number of results to return.

        Returns:
            dict: status and a report with the matching catalog rows, or a note that
            the company has not uploaded a catalog yet.
        """
        from rubik_core.retrieval.product_catalog import get_product_catalog

        catalog = get_product_catalog()
        everything = catalog.get_all_products()
        if not everything:
            return {
                "status": "success",
                "report": (
                    "The company has not uploaded a product catalog yet. An admin "
                    "can add one in Admin → Company setup."
                ),
            }
        rows = catalog.search_products(query) if (query or "").strip() else everything
        if not rows:
            return {
                "status": "success",
                "report": f"No product in the catalog matches {query!r}.",
            }
        lines = []
        for row in rows[: max(1, top_k)]:
            head = f"- **{row['name']}**" + (
                f" ({row['category']})" if row.get("category") else ""
            )
            details = [
                row.get("description") or "",
                f"List price: {row['price']}" if row.get("price") else "",
                f"Link: {row['url']}" if row.get("url") else "",
            ]
            lines.append(" — ".join([head, *(d for d in details if d)]))
        return {"status": "success", "report": "\n".join(lines)}

    @attribute_to("web_search")
    def search_web(self, query: str) -> dict:
        """Search the internet for up-to-date information, news, or laws and regulations.

        Use this tool when the Knowledge Base does not have the answer or when the
        information needs to be extremely current (e.g. today's news or a very recent regulation).

        Args:
            query: The search query to perform on Google.

        Returns:
            dict: status and a report containing the grounded answer from Google Search.
        """
        from rubik_core.ai_providers.registry import resolve_model_route
        from rubik_core.ai_providers.runtime import generate_grounded_text_on_route
        from rubik_core.config import settings

        try:
            route = resolve_model_route(
                "chat_web_search",
                "grounding",
                default_model=settings.VERTEX_LLM_MODEL_FLASH,
            )
            today = datetime.now().strftime("%B %d, %Y")
            response = generate_grounded_text_on_route(
                route,
                f"Today is {today}. Search the web and provide a detailed, up-to-date answer for: {query}",
                temperature=0.0,
            )
            report = response.text
            if response.citation_urls:
                citations = [
                    f"**Source:** {url}\nOnline reference: {url}"
                    for url in response.citation_urls[:10]
                ]
                report += "\n\n---\n\n" + "\n\n---\n\n".join(citations)

            return {
                "status": "success",
                "report": f"ONLINE RESEARCH RESULTS:\n\n{report}",
            }
        except Exception as exc:  # noqa: BLE001
            # A provider SDK error can contain request headers, URLs or prompt
            # text. The ADK tool result is visible in Chat and its trace.
            logger.warning("search_web provider failed (%s)", type(exc).__name__)
            return {
                "status": "error",
                "error_message": (
                    "Workspace web search could not run. Check its AI model "
                    "connection in Admin → Workspace."
                ),
            }

    # ─── Slack Tools ─────────────────────────────────────────────────────────

    def slack_get_channel_messages(
        self,
        channel: str,
        max_results: int = 25,
        hours_back: int = 0,
    ) -> dict:
        """Get recent messages from a Slack channel, optionally time-bounded.

        Use for prompts like "what's happening in #<channel>", "summarize the
        #<channel> channel", "recap what was said in #<channel> today", or
        "what happened yesterday in #<channel>" (use hours_back=24 for "yesterday",
        hours_back=168 for "this week"). Accepts either a channel name with
        or without the leading `#`, or a Slack channel ID (`C…`). Works for
        both public and private channels the user belongs to.

        Args:
            channel: Channel name (e.g. "<channel>") or Slack channel ID.
            max_results: Maximum messages to return (default 25).
            hours_back: If > 0, only include messages from the last N hours.
                Pass 0 for the most recent messages regardless of age.

        Returns:
            dict: status and a report listing messages with sender + timestamp,
            or an instruction to connect Slack if the user hasn't authorized.
        """
        from datetime import datetime, timedelta

        from backend.services.slack_service import (
            SlackAPIError,
            SlackNotConnectedError,
            SlackService,
        )

        since = (
            datetime.now(timezone.utc) - timedelta(hours=hours_back)
            if hours_back and hours_back > 0
            else None
        )
        try:
            svc = SlackService(user_email=self.user_email)
            # Skip the per-message chat.getPermalink round-trip (~400ms
            # each). The report below uses app_link (a locally-built slack://
            # deep link) and only falls back to permalink, so suppressing it is
            # behavior-preserving and keeps the chat turn under the 90s timeout.
            messages = svc.get_channel_messages(
                channel=channel, since=since, limit=max_results, skip_permalink=True
            )
        except SlackNotConnectedError:
            return {
                "status": "not_connected",
                "report": "Slack isn't connected yet. Direct the user to Settings → Slack to authorize.",
            }
        except SlackAPIError as e:
            return {"status": "error", "error_message": f"Slack API error: {e.error}"}
        except Exception as e:
            logger.error("slack_get_channel_messages error: %s", e)
            return {"status": "error", "error_message": str(e)}

        if not messages:
            return {"status": "success", "report": f"No recent messages in #{channel}."}

        lines = []
        for m in messages:
            who = m.user_name or m.user_id or "(unknown)"
            preview = m.text[:300].replace("\n", " ")
            link = (
                f" — {m.app_link or m.permalink}" if (m.app_link or m.permalink) else ""
            )
            lines.append(f"- [{who}] {preview}{link}")
        return {"status": "success", "report": "\n".join(lines)}

    async def slack_summarize_my_unread(
        self,
        hours_back: int = 24,
    ) -> dict:
        """Cross-channel Slack catch-up: summarize unread activity across ALL
        channels the user belongs to.

        Use this whenever the user asks about Slack BROADLY without naming a
        specific channel: "what happened in slack yesterday", "what did I
        miss on slack", "slack catch-up", "anything important in slack
        today", "what's new in slack this week". The tool reads each channel
        the user is in, drops noise, and produces ONE digest covering only
        the channels with worthwhile activity.

        For a SINGLE named channel, use `slack_summarize_channel` instead.

        Args:
            hours_back: Time window in hours (24 = since yesterday, default).
                Use 168 for "this week", 8 for "today" (working hours).

        Returns:
            dict: status and a markdown digest with one ### section per
            channel that had real activity. Pass through to the user
            largely as-is — don't re-summarize.
        """
        from backend.services.slack_service import (
            SlackAPIError,
            SlackNotConnectedError,
            SlackService,
        )

        try:
            svc = SlackService(user_email=self.user_email)
            result = await svc.summarize_unread_across_channels(hours_back=hours_back)
        except SlackNotConnectedError:
            return {
                "status": "not_connected",
                "report": "Slack isn't connected yet. Direct the user to Settings → Slack to authorize.",
            }
        except SlackAPIError as e:
            return {"status": "error", "error_message": f"Slack API error: {e.error}"}
        except Exception as e:
            logger.error("slack_summarize_my_unread error: %s", e)
            return {"status": "error", "error_message": str(e)}

        header = (
            f"**Slack catch-up — last {result['hours_back']}h** "
            f"({result['channel_count']} channels, {result['message_count']} messages)"
        )
        return {
            "status": "success",
            "report": f"{header}\n\n{result['summary_md']}",
        }

    async def slack_summarize_channel(
        self,
        channel: str,
        hours_back: int = 24,
        max_messages: int = 100,
    ) -> dict:
        """Produce an executive summary of recent activity in a Slack channel.

        Use this for ANY prompt where the user wants the SIGNAL from a channel
        rather than the raw transcript: "what happened in #X yesterday",
        "summarize #X this week", "recap #X today", "what did I miss in #X".
        The user already has Slack — they're asking the assistant because they
        don't want to read every message.

        For literal "show me the last N messages" requests where the user
        actually wants the raw list, use `slack_get_channel_messages` instead.

        Args:
            channel: Channel name (e.g. "<channel>") or Slack channel ID.
            hours_back: Time window in hours (24 = yesterday, 168 = this week).
                Defaults to 24.
            max_messages: Cap on messages pulled before summarization (default 100).

        Returns:
            dict: status and a markdown-formatted summary covering key topics,
            decisions, action items, and open questions. The summary is the
            primary output — pass it through to the user largely as-is rather
            than re-summarizing.
        """
        from backend.services.slack_service import (
            SlackAPIError,
            SlackNotConnectedError,
            SlackService,
        )

        try:
            svc = SlackService(user_email=self.user_email)
            result = await svc.summarize_channel_activity(
                channel=channel,
                hours_back=hours_back,
                max_messages=max_messages,
            )
        except SlackNotConnectedError:
            return {
                "status": "not_connected",
                "report": "Slack isn't connected yet. Direct the user to Settings → Slack to authorize.",
            }
        except SlackAPIError as e:
            return {"status": "error", "error_message": f"Slack API error: {e.error}"}
        except Exception as e:
            logger.error("slack_summarize_channel error: %s", e)
            return {"status": "error", "error_message": str(e)}

        header = (
            f"**{result['channel']}** — last {result['hours_back']}h "
            f"({result['message_count']} messages)"
        )
        return {
            "status": "success",
            "report": f"{header}\n\n{result['summary_md']}",
        }

    def slack_get_my_mentions(self, max_results: int = 20) -> dict:
        """Return Slack messages that @-mention the current user.

        Use for prompts like "show me my Slack mentions", "anything I'm
        tagged in on Slack today", or "what Slack messages need my
        attention". Requires the `search:read` user scope to be granted at
        OAuth time (it's in the manifest by default).

        Args:
            max_results: Maximum mentions to return (default 20).

        Returns:
            dict: status and a report listing mentions, or a connect-Slack
            instruction if the user hasn't authorized.
        """
        from backend.services.slack_service import (
            SlackAPIError,
            SlackNotConnectedError,
            SlackService,
        )

        try:
            svc = SlackService(user_email=self.user_email)
            mentions = svc.get_mentions(limit=max_results)
        except SlackNotConnectedError:
            return {
                "status": "not_connected",
                "report": "Slack isn't connected yet. Direct the user to Settings → Slack to authorize.",
            }
        except SlackAPIError as e:
            return {"status": "error", "error_message": f"Slack API error: {e.error}"}
        except Exception as e:
            logger.error("slack_get_my_mentions error: %s", e)
            return {"status": "error", "error_message": str(e)}

        if not mentions:
            return {
                "status": "success",
                "report": "No Slack mentions found in the searched window.",
            }

        lines = []
        for m in mentions:
            channel = f"#{m.channel_name}" if m.channel_name else m.channel_id
            who = m.user_name or m.user_id or "(unknown)"
            preview = m.text[:300].replace("\n", " ")
            link = (
                f" — {m.app_link or m.permalink}" if (m.app_link or m.permalink) else ""
            )
            lines.append(f"- {channel} [{who}] {preview}{link}")
        return {"status": "success", "report": "\n".join(lines)}

    def slack_search_messages(self, query: str, max_results: int = 15) -> dict:
        """Free-text search across the user's accessible Slack content.

        Use for prompts like "find Slack messages about <topic>",
        "search Slack for <topic>", or "what did <person> say about
        <topic> in Slack". For "what's happening in <channel>" prefer
        `slack_get_channel_messages` instead.

        Args:
            query: Slack search query (supports operators like `from:@<person>`
                or `in:#<channel>`).
            max_results: Maximum results to return (default 15).

        Returns:
            dict: status and a report with matched messages, or a
            connect-Slack instruction if the user hasn't authorized.
        """
        from backend.services.slack_service import (
            SlackAPIError,
            SlackNotConnectedError,
            SlackService,
        )

        try:
            svc = SlackService(user_email=self.user_email)
            results = svc.search_messages(query=query, limit=max_results)
        except SlackNotConnectedError:
            return {
                "status": "not_connected",
                "report": "Slack isn't connected yet. Direct the user to Settings → Slack to authorize.",
            }
        except SlackAPIError as e:
            return {"status": "error", "error_message": f"Slack API error: {e.error}"}
        except Exception as e:
            logger.error("slack_search_messages error: %s", e)
            return {"status": "error", "error_message": str(e)}

        if not results:
            return {
                "status": "success",
                "report": f"No Slack messages matched: {query}",
            }

        lines = []
        for m in results:
            channel = f"#{m.channel_name}" if m.channel_name else m.channel_id
            who = m.user_name or m.user_id or "(unknown)"
            preview = m.text[:300].replace("\n", " ")
            link = (
                f" — {m.app_link or m.permalink}" if (m.app_link or m.permalink) else ""
            )
            lines.append(f"- {channel} [{who}] {preview}{link}")
        return {"status": "success", "report": "\n".join(lines)}

    # ─── Open loops — "what am I waiting on?" (Gmail + Slack together) ───────

    async def what_am_i_waiting_on(self, days: int = 7, sources: str = "all") -> dict:
        """What the user is waiting on, across Gmail AND Slack in ONE sweep.

        Use this — and not the single-source tools — whenever the user asks what
        is outstanding for them without meaning one specific inbox:
        "what am I waiting on", "am I waiting on anything", "what's outstanding",
        "who owes me something", "what still needs my reply / input / approval",
        "did anyone get back to me", "what's pending", "open loops",
        "what haven't I closed out", "what follow-ups do I owe or am I owed".
        It applies whether or not they say "email" or "Slack" — and ESPECIALLY
        when they name neither.

        It reads every recent email thread (inbox and sent) and every Slack
        thread the user was tagged in plus their DMs, follows each one to its
        NEWEST message, and returns a report already sorted into: what needs the
        user's action (by priority, with owner and deadline), what they are
        waiting on from others, and what it skipped because the thread was
        already resolved or somebody else has it. That last section is part of
        the answer — keep it.

        NOT for: "summarize my unread" (`gmail_summarize_unread`), "what did I
        miss on Slack" (`slack_summarize_my_unread`), a topic or person lookup
        (`gmail_search` / `slack_search_messages`).

        Args:
            days: Look-back window in days (default 7; "this month" → 30; max 30).
            sources: "all" (default — the user named no source or named both),
                "gmail" when they clearly mean only email, "slack" when they
                clearly mean only Slack.

        Returns:
            dict: status and a markdown `report` with fixed sections and a
            footer naming what was and was not checked. Pass it through to the
            user as-is — do not re-rank it, drop the Skipped section, or add
            items of your own.
        """
        from backend.services.open_loops import run_open_loops_sweep

        try:
            result = await run_open_loops_sweep(
                self.user_email, days=days, sources=sources
            )
        except Exception as e:  # noqa: BLE001 — surface, never 500 the turn
            logger.error("what_am_i_waiting_on error: %s", e)
            return {"status": "error", "error_message": str(e)}
        return {
            "status": "success",
            "report": result.report_md,
            "checked": result.checked,
            "not_checked": result.not_checked,
            "candidate_count": len(result.candidates),
        }

    # ─── Promises — "what did I / my team say we'd send, and is it done?" ────

    def promises_outstanding(
        self,
        whose: str = "me",
        team: str = "",
        days: int = 7,
        include_done: bool = False,
    ) -> dict:
        """Promises the user — or the people they manage — MADE to someone and
        have not yet kept, grouped by the contact who is waiting.

        Cubie hears these overnight in meetings, L10s, sent email and Slack
        ("I'll send you the quote Friday") and checks each one past its date
        for evidence it happened. Use this whenever the question is about
        commitments someone MADE: "who did my team promise a follow-up / quote
        / resource to this week and hasn't sent it", "what did I say I'd do",
        "what have I promised people", "which promises are slipping", "who is
        my sales team behind on", "what follow-ups do my reps owe clients".

        NOT for what OTHER people owe the user or replies they are waiting on
        (`what_am_i_waiting_on`) or EOS team To-Dos.

        Args:
            whose: "me" (default) for the user's own promises; "team" when
                they ask about their team, reps, reports or a department.
            team: A department name when they name one ("sales", "marketing");
                empty for everyone they manage.
            days: Look-back for when the promise was made (default 7; "this
                month" → 30). Promises DUE this week are always included.
            include_done: True only when they also want the ones that look done.

        Returns:
            dict: status and a markdown `report` grouped by contact, each line
            naming who promised it, their words, when and where, the due date
            and whether there is any sign it happened, with a footer saying
            what was checked. Pass it through as-is; do not add promises of
            your own or turn "no sign yet" into "didn't do it".
        """
        from rubik_core.config import settings

        if not getattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", False):
            return {
                "status": "error",
                "report": "Promise follow-through is switched off in this environment.",
            }
        try:
            from backend.services.promise_report import build_report
            from rubik_core.auth.registry import UserRegistry
            from rubik_core.database.db import SessionLocal
            from rubik_core.features import feature_allows

            profile = UserRegistry().get_user(self.user_email)
            if profile is None or not feature_allows("promise_accountability", profile):
                return {
                    "status": "error",
                    "report": "Promise follow-through isn't switched on for your team.",
                }
            with SessionLocal() as db:
                return build_report(
                    db,
                    profile,
                    whose="team" if str(whose).lower() == "team" else "me",
                    team=team or "",
                    days=days,
                    include_done=bool(include_done),
                )
        except Exception as e:  # noqa: BLE001 — surface, never 500 the turn
            logger.error("promises_outstanding error: %s", e)
            return {"status": "error", "error_message": str(e)}

    # ─── Loose Ends — what the weekday-morning scan found ────────────────────

    def loose_ends(self, scope: str = "me", older_than_days: int = 0) -> dict:
        """The user's LOOSE ENDS from Cubie's weekday-morning scan (or, for a
        Sales leader, their team's), read from the list that scan keeps.

        Three kinds: client emails the user sent, or client conversations they
        reported in Slack, that are NOT LOGGED IN HUBSPOT; client emails
        WAITING ON THE USER'S REPLY for 2+ business days; and things they said
        they would do that are past their date, plus overdue HubSpot tasks.

        Use this ONLY when the user talks about loose ends or this scan:
        "what loose ends do I have", "my loose ends", "what isn't logged in
        HubSpot", "which of my emails are not logged in HubSpot", "unlogged
        emails", "which client emails haven't I answered from the morning
        scan", "loose ends over 7 days", "which loose ends on my team are over
        7 days".

        NOT for "what am I waiting on", "who owes me", "open loops" or "what's
        outstanding" — that is `what_am_i_waiting_on`, a live sweep of the
        inbox. NOT for "what did my team promise" or "which promises are
        slipping" — that is `promises_outstanding`.

        Args:
            scope: "me" (default) for the user's own loose ends; "team" when
                they ask about their team, reps or AEs.
            older_than_days: 0 (default) for all of them; 7 when they ask for
                loose ends "over 7 days" (or the number of days they name).

        Returns:
            dict: status and a markdown `report`. Pass it through as-is: keep
            every count together with what it is out of, keep the line saying
            what the scan checked, and never call a source the scan could not
            read "all clear". A team report names people A to Z and never
            ranks them; do not rank them either.
        """
        from rubik_core.config import settings
        from rubik_core.logsafe import safe_error

        if not getattr(settings, "LOOP_CLOSURE_ENABLED", False):
            return {
                "status": "error",
                "report": f"Loose Ends is switched off in {PRODUCT_NAME} right now.",
            }
        team = str(scope or "").strip().lower() == "team"
        try:
            min_days = max(0, int(older_than_days or 0))
        except (TypeError, ValueError):
            min_days = 0
        try:
            from rubik_core.auth.registry import UserRegistry
            from rubik_core.database.db import SessionLocal
            from rubik_core.features import feature_allows
            from rubik_core.loop_closure import constants, views

            profile = UserRegistry().get_user(self.user_email)
            key = "loop_closure_team" if team else "loop_closure"
            if profile is None or not feature_allows(key, profile):
                return {
                    "status": "error",
                    "report": (
                        "The Loose Ends team view is limited to Sales leaders."
                        if team
                        else "Loose Ends isn't switched on for your team yet."
                    ),
                }
            labels = dict(getattr(constants, "KIND_LABELS", {}) or {})
            with SessionLocal() as db:
                if team:
                    body = views.team_view(db)
                    # A cross-person read of what a scan of other people's
                    # mailboxes found: audited like the Team tab's GET.
                    from backend.audit import record_audit

                    record_audit(
                        db,
                        actor_email=(self.user_email or "").strip().lower(),
                        action="LOOSE_ENDS_TEAM_VIEW",
                        target="loose-ends:team",
                        details={
                            "via": "chat",
                            "roster_size": body.get("roster_size"),
                        },
                    )
                    report = self._loose_ends_team_md(
                        body,
                        labels=labels,
                        min_days=min_days,
                        escalation_days=int(
                            getattr(settings, "LOOP_CLOSURE_ESCALATION_DAYS", 7) or 7
                        ),
                    )
                else:
                    body = views.mine_view(
                        db,
                        (self.user_email or "").strip().lower(),
                        has_todos=bool(feature_allows("todos", profile)),
                    )
                    report = self._loose_ends_mine_md(body, min_days=min_days)
            return {"status": "success", "report": report}
        except Exception as e:  # noqa: BLE001 — surface, never 500 the turn
            logger.error("loose_ends error: %s", safe_error(e))
            return {
                "status": "error",
                "report": f"{PRODUCT_NAME} could not read the Loose Ends list just now.",
            }

    @staticmethod
    def _loose_ends_when(value) -> str:
        """``Oct 7, 12:04 UTC`` from a naive-UTC datetime or ISO string."""
        dt = value
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
        if isinstance(dt, datetime):
            return f"{dt:%b} {dt.day}, {dt:%H:%M} UTC"
        return str(value)

    @staticmethod
    def _loose_ends_coverage_line(coverage: dict | None) -> str:
        """What the last scan looked at, with each count's unit — the
        denominators behind "nothing found"."""
        if not coverage:
            return "Your mailbox was not part of the last scan, so nothing of yours was checked."
        if coverage.get("scan") == "failed" or coverage.get("gmail") == "unavailable":
            # No counts: "checked 0 email threads" would read as an empty inbox.
            return (
                "The last scan could not read your mailbox, so nothing from email "
                "was checked or cleared. This is not an all-clear."
            )
        return (
            "The last scan checked "
            f"{int(coverage.get('threads_scanned') or 0)} email threads, "
            f"{int(coverage.get('slack_messages_scanned') or 0)} Slack messages, "
            f"{int(coverage.get('promises_scanned') or 0)} promises and "
            f"{int(coverage.get('tasks_scanned') or 0)} HubSpot tasks."
        )

    @staticmethod
    def _loose_ends_mine_md(body: dict, *, min_days: int = 0) -> str:
        """The AE's own list as markdown — built in code, never by the model."""
        if not body.get("scanned_at"):
            return (
                f"{PRODUCT_NAME} has not scanned your mailbox for loose ends yet, so there "
                "is no list to show. The scan runs on weekday mornings. This is "
                "not an all-clear."
            )
        cap = 10
        totals = body.get("totals") or {}
        esc = int((body.get("thresholds") or {}).get("escalation_days") or 7)
        open_n = int(totals.get("open") or 0)
        over = int(totals.get("over_escalation") or 0)
        lines = [
            f"**Your loose ends** (last scanned "
            f"{AgentTools._loose_ends_when(body['scanned_at'])})",
            f"{open_n} open; {over} of the {open_n} have been open {esc} days "
            f"or more. This week: {int(totals.get('flagged_this_week') or 0)} "
            f"flagged, {int(totals.get('closed_this_week') or 0)} closed.",
        ]
        if min_days:
            lines.append(f"Showing only the ones open {min_days} days or more.")
        for group in body.get("groups") or []:
            items = list(group.get("items") or [])
            count = int(group.get("count") or len(items))
            if min_days:
                items = [i for i in items if int(i.get("days_open") or 0) >= min_days]
                heading = (
                    f"**{group.get('label')}**: {len(items)} of {count} open "
                    f"{min_days}+ days"
                )
            else:
                heading = f"**{group.get('label')}**: {count} of {open_n} open"
            lines += ["", heading]
            if not items:
                if not min_days and group.get("empty_line"):
                    lines.append(str(group["empty_line"]))
                continue
            for item in items[:cap]:
                who = " · ".join(
                    str(x) for x in (item.get("counterparty"), item.get("company")) if x
                )
                age = (item.get("age") or {}).get("label") or (
                    f"{int(item.get('days_open') or 0)} days"
                )
                parts = [item.get("title") or "(no subject)"]
                if who:
                    parts.append(who)
                parts.append(age)
                line = "- " + " — ".join(str(p) for p in parts)
                if item.get("headline"):
                    line += f" — {item['headline']}"
                if item.get("reopened"):
                    line += " (re-opened)"
                href = next(
                    (
                        a.get("href")
                        for a in (
                            item.get("primary_action"),
                            item.get("secondary_action"),
                        )
                        if isinstance(a, dict) and a.get("href")
                    ),
                    None,
                )
                if href:
                    line += f" ([open]({href}))"
                lines.append(line)
            if len(items) > cap:
                lines.append(
                    f"- …and {len(items) - cap} more of {len(items)}, on Sales → "
                    "Loose Ends."
                )
        lines += ["", AgentTools._loose_ends_coverage_line(body.get("coverage"))]
        lines += [f"_{note}_" for note in body.get("notes") or []]
        lines.append(
            "Each row's action (log it in HubSpot, draft the reply) is on Sales → "
            "Loose Ends. A loose end clears on its own once HubSpot or the thread "
            "shows it was done."
        )
        return "\n".join(lines)

    @staticmethod
    def _loose_ends_team_md(
        body: dict,
        *,
        labels: dict,
        min_days: int = 0,
        escalation_days: int = 7,
    ) -> str:
        """The team roll-up as markdown — METADATA ONLY: owner (A–Z), kind,
        company / contact name and days open. No subject, title, headline or
        snippet is read from the body, so none can be rendered."""
        if not body.get("scanned_at"):
            return (
                f"{PRODUCT_NAME} has not run the loose-ends scan for your team yet, so "
                "there is nothing to show. This is not an all-clear."
            )
        cap = 10
        people = sorted(
            body.get("people") or [],
            key=lambda p: str(p.get("name") or p.get("email") or "").casefold(),
        )
        roster = int(body.get("roster_size") or len(people))
        total_open = sum(int(p.get("open") or 0) for p in people)
        total_over = sum(int(p.get("over_escalation") or 0) for p in people)
        with_open = sum(1 for p in people if int(p.get("open") or 0) > 0)
        lines = [
            f"**Loose ends on your team** (last scanned "
            f"{AgentTools._loose_ends_when(body['scanned_at'])})",
            f"{with_open} of {roster} people have open loose ends; {total_over} "
            f"of the {total_open} open loose ends are {escalation_days} days or "
            "older. People are listed A to Z, not ranked.",
        ]
        if min_days:
            lines.append(f"Showing only loose ends open {min_days} days or more.")
        gap_words = {
            ("gmail", "unavailable"): "Gmail could not be read",
            ("gmail", "partial"): "only the newest part of Gmail was read",
            ("slack", "not_connected"): "Slack is not connected",
            ("slack", "unavailable"): "Slack could not be read",
            ("hubspot", "no_owner"): "no HubSpot owner matches this person",
            ("hubspot", "unavailable"): "HubSpot could not be read",
            ("hubspot", "partial"): "HubSpot was only partly read",
            ("ai", "unavailable"): "the AI check did not run",
        }
        for person in people:
            name = person.get("name") or person.get("email") or "Someone"
            open_n = int(person.get("open") or 0)
            items = list(person.get("items") or [])
            if min_days:
                items = [i for i in items if int(i.get("days_open") or 0) >= min_days]
                if not items:
                    continue
            by_kind = person.get("by_kind") or {}
            kinds = ", ".join(
                f"{labels.get(k, k)} {int(n or 0)}" for k, n in by_kind.items() if n
            )
            head = (
                f"**{name}**: {open_n} open "
                f"({int(person.get('over_escalation') or 0)} of {open_n} "
                f"{escalation_days}+ days)"
            )
            if kinds:
                head += f": {kinds}"
            if not open_n:
                head = f"**{name}**: nothing open"
            lines += ["", head]
            for item in items[:cap]:
                age = (item.get("age") or {}).get("label") or (
                    f"{int(item.get('days_open') or 0)} days"
                )
                kind = labels.get(item.get("kind"), item.get("kind") or "")
                lines.append(f"- {kind} · {item.get('who') or 'unknown'} · {age}")
            if len(items) > cap:
                lines.append(f"- …and {len(items) - cap} more of {len(items)}.")
            coverage = person.get("coverage") or {}
            gaps = [
                gap_words.get((source, str(state)), f"{source}: {state}")
                for source, state in coverage.items()
                if source in ("gmail", "slack", "hubspot", "ai")
                and state
                and state != "ok"
            ]
            if gaps:
                lines.append(
                    "- Could not look: " + "; ".join(gaps) + ". Not an all-clear."
                )
        if min_days and not any(
            int(i.get("days_open") or 0) >= min_days
            for p in people
            for i in p.get("items") or []
        ):
            lines += [
                "",
                f"No loose end on the team has been open {min_days} days or more.",
            ]
        health = body.get("health") or {}
        if health.get("flagged"):
            lines += [
                "",
                f"Noise: {int(health.get('dismissed') or 0)} of "
                f"{int(health.get('flagged') or 0)} flags in the last "
                f"{int(health.get('window_days') or 30)} days were marked not a "
                "real miss.",
            ]
        return "\n".join(lines)

    # ─── Product Help ────────────────────────────────────────────────────────

    def _help_allowed_section_ids(self) -> set[str]:
        """Section ids this user may be told about.

        Mirrors the Help page, which hides Customer X-Ray and CRM Sync from
        people who can't reach them. Fails closed: if the profile or the gates
        can't be resolved, only the ungated sections are offered.
        """
        import logging

        from rubik_core.help import HELP_SECTIONS

        logger = logging.getLogger(__name__)

        allowed = {s.id for s in HELP_SECTIONS if not s.requires}
        try:
            from backend.auth import user_can_access_crm_sync, user_can_access_xray
            from rubik_core.auth.registry import UserRegistry
            from rubik_core.config import settings
            from rubik_core.features import feature_allows

            profile = UserRegistry().get_user(self.user_email)
            if profile is not None:
                gates = {
                    "xray": user_can_access_xray,
                    "crm_sync": user_can_access_crm_sync,
                    # Newer features gate straight off the registry rather than
                    # growing a predicate in backend/auth.py per section.
                    "scheduled_tasks": lambda p: feature_allows("scheduled_tasks", p),
                    "todos": lambda p: feature_allows("todos", p),
                    "proposals": lambda p: feature_allows("proposal_generator", p),
                    "voice_commands": lambda p: feature_allows("voice_commands", p),
                    # The dashboards answer 404 with the kill switch off, so
                    # Chat must not point anyone at them then either.
                    "insights": lambda p: (
                        settings.INSIGHTS_ENABLED and feature_allows("insights", p)
                    ),
                    # Same reason: the drawer's feed answers 404 with the
                    # What's New kill switch off.
                    "whats_new": lambda p: (
                        settings.WHATS_NEW_ENABLED and feature_allows("whats_new", p)
                    ),
                    # And Cubie for Chrome: its Connect page and API answer 404
                    # with the switch off.
                    "chrome_extension": lambda p: (
                        settings.CHROME_EXTENSION_ENABLED
                        and feature_allows("chrome_extension", p)
                    ),
                }
                for section in HELP_SECTIONS:
                    gate = gates.get(section.requires or "")
                    if gate is not None and gate(profile):
                        allowed.add(section.id)
        except Exception as e:  # noqa: BLE001 — gated sections stay hidden
            logger.warning("help section gating failed, showing ungated only: %s", e)
        return allowed

    def search_cubie_help(self, query: str, top_k: int = 3) -> dict:
        """Look up how to USE Cubie itself — its features, pages, buttons, and limits.

        This is the product's own Help page. Use it whenever the user asks about
        the app rather than about their data: "how do I add a document to the
        knowledge base?", "where do I connect Slack?", "what does Done do on a
        priority card?", "can Cubie send emails?", "why is my briefing empty?",
        "who can see an L10 meeting?", "what can Chat answer about?".

        Do NOT use it for the user's actual work (deals, emails, meetings,
        documents) — those have their own tools. Never describe how a feature
        behaves from memory; call this and answer from what it returns.

        Args:
            query: The user's question about using Cubie, in their own words.
            top_k: How many Help sections to return (default 3).

        Returns:
            dict: status and a report containing the matching Help sections,
                each ending in a "Help link:" URL to give the user.
        """
        # No LLM call happens here — it's a lexical lookup over a fixed page, so
        # the turn's tokens stay attributed to "chat" rather than a new feature.
        from rubik_core.help import render_help_report, search_help, section_directory

        allowed = self._help_allowed_section_ids()
        try:
            k = max(1, min(int(top_k), 5))
        except (TypeError, ValueError):
            k = 3

        sections = search_help(query, top_k=k, allowed_ids=allowed)
        if not sections:
            return {
                "status": "success",
                "report": (
                    f"No Help section matched that question. {PRODUCT_NAME}'s Help "
                    "page covers:\n"
                    f"{section_directory(allowed)}\n\n"
                    "Tell the user which of these looks closest, or say plainly that "
                    "Help doesn't document it — do not invent product behavior."
                ),
            }
        return {"status": "success", "report": render_help_report(sections)}
