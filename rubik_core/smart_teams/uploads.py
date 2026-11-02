"""ST-5 — an official assessment report becomes a *proposal*, and only then a layer.

Optional enrichment, not a step
-------------------------------
Few people have official assessment results (plan.md Decision 1). The default
state of this feature for everyone else is **no upload at all**, and that state
is not a gap: :func:`list_uploads` returns ``[]``, no profile is missing
anything, no guide is incomplete, and nothing in the product nags. Every
function here is written so that "there is no official data" is the ordinary
answer rather than an error path.

The three states, and why the middle one exists
-----------------------------------------------
1. **Uploaded.** The bytes are stored and an ``st_uploads`` row exists with
   ``review_status='pending'``, ``framework=NULL``.
2. **Parsed.** :func:`parse_upload` — on cubie-worker, one Gemini call — has
   *proposed* a framework and a payload, or has recorded ``framework=NULL`` plus
   a ``parse_error`` because it could not tell. **Nothing has reached a profile.**
3. **Confirmed and merged.** The person read the proposal and said yes, and
   :func:`merge_upload` wrote a new ``st_profiles`` version.

A parse is a proposal because a model reading a PDF is a guess about a document
that is *about a person*. ``st_uploads.framework`` is nullable on purpose: a
parse that cannot tell which instrument it is looking at records NULL and a
refusal code, never a guess. A wrong guess that merged silently would replace a
layer of somebody's profile with numbers from an instrument it is not measuring.

Vendor prose never enters our data. Three mechanisms, in order of strength
-------------------------------------------------------------------------
Parsing a report someone uploads about themselves is fine — those are their
results, given with their consent, for their own profile. The report's *writing*
is the vendor's copyrighted work, and once ST-6 publishes a guide, anything in a
profile payload can reach a document colleagues read and the companywide RAG
corpus. So:

1. **The stored shape has nowhere to put prose.** A merged DISC layer is
   ``DimensionScore`` intensities, a style named by *our* taxonomy
   (``disc.WORK_STYLES``), and *our* authored prose — the same object
   ``score_disc`` produces from an interview. The vendor's payload contributes
   four integers and nothing else.
2. **The parse contract has no text field**, and
   :func:`_coerce_proposal` drops every key
   :data:`~rubik_core.smart_teams.upload_prompts.OUTPUT_CONTRACT` does not name,
   then asserts leaf by leaf that no string survived except a member of a named
   closed vocabulary (:func:`_assert_no_prose`). A model that returns a summary
   loses it here, and the count of what was dropped is reported.
3. **The extracted report text is never persisted.** It exists inside one
   :func:`parse_upload` call. ``parse_error`` carries a code from
   :data:`PARSE_REASONS` and a sentence *we* wrote — never a model string, never
   an excerpt of the document, because an error column is a rendered surface too.

The stored PDF itself is kept (the person may want to see what they uploaded and
an admin may need it to debug a bad parse), under the storage rules below.

Storage, and why this is not an Asset
-------------------------------------
The bytes follow ``backend/services/generated_docs.py``: GCS in GCP mode under
``{GCS_RAG_PREFIX}/`` — the same bucket the DAM uses, so IAM and lifecycle stay
one story, and deliberately NOT under ``rags/<slug>/``, which is the only prefix
:mod:`rubik_core.rag.manager` imports into a corpus — and ``LOCAL_DATA_DIR``
otherwise. What this module does **not** do is register an ``AssetMetadata`` row,
even though ``st_uploads.doc_id``'s column comment calls it "the Cubie asset
id". An Asset is governed by ``has_document_access``, whose rule 3 grants a
department-scoped Exec sight of everything in their department — and an official
psychometric report is exactly the raw material ``authz.can_view_raw_profile``
exists to keep away from execs and managers. So ``doc_id`` here is an internal
storage id (:data:`DOC_ID_PREFIX`), the DAM never learns about the file, and the
only readers are the subject and a support admin.

The merge, stated exactly
-------------------------
:func:`merge_upload` writes ONE new ``st_profiles`` version through
``profile.write_profile_version`` — it does not reimplement version numbering,
``is_current`` retirement, or the collision retry. In that new version:

* the confirmed framework's payload is built from the upload, and its
  ``provenance`` entry becomes ``"official_upload"``;
* **every other framework's payload and provenance entry is carried across byte
  for byte** from the previous current version. Not recomputed, not re-scored,
  not defaulted — copied, and validated on the way through so a malformed
  carried payload refuses the merge instead of being written forward.

``"merged"`` — the third ``PROVENANCE_SOURCES`` value, meaning an upload
reconciled *with* interview answers — is deliberately not produced by this
module. The DISC adapter below uses the vendor's four numbers and none of the
person's interview answers, so calling the result "merged" would claim a
reconciliation that did not happen.

Which frameworks can actually merge
-----------------------------------
:data:`MERGE_ADAPTERS` has one entry: ``disc``. That is the ticket's scope ("ST-5
Official-results upload + parse + merge (ET's DISC first)") and it is also the
honest limit. An adapter has to produce the instrument's own result object,
because that object is what ``contracts.envelope_for`` computes the reportable
gate from — and for four of the other five, building one from a vendor report
would misstate what it is:

* ``big_five`` — ``BigFiveResult.as_dict()`` hardcodes ``"instrument":
  "ipip_50"``. A vendor's Big Five is not the IPIP-50 bank, and a facet
  breakdown we do not have cannot be invented.
* ``type16`` — is *derived* from a scored ``BigFiveResult`` and labelled so
  everywhere. Building it from four letters printed on a report would make the
  "(derived)" qualifier false.
* ``strengths`` — our taxonomy is our own authored themes. Mapping a vendor's
  theme names onto it means holding those names, which plan.md forbids.
* ``motivators`` / ``enneagram`` — plausible future adapters; there is no
  official data for them to read today, and an adapter with nothing to
  parse is an untested one.

Detection still recognizes all six, because telling somebody "this looks like a
strengths report and Cubie cannot merge those yet" is a better answer than "we
could not tell what this is". Those uploads record the framework, a
:data:`REASON_UNSUPPORTED_FRAMEWORK` refusal, and never merge.

Advisory only
-------------
Nothing here is a measure of competence, potential, promotion-readiness, or
fitness for a role. A merged layer informs collaboration and team staffing, and
the raw payload it lands in is visible only to its subject and to the support
admins.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cost.usage import attribute_to
from rubik_core.database.smart_teams import (
    PROFILE_FRAMEWORKS,
    SmartTeamsDocument,
    SmartTeamsProfile,
    SmartTeamsUpload,
    default_provenance,
    document_cache_keys,
    subject_key_for,
)
from rubik_core.smart_teams import upload_prompts as up
from rubik_core.smart_teams.contracts import (
    FRAMEWORKS,
    LayerOutcome,
    PayloadError,
    ScoringOutput,
    envelope_for,
    not_taken_envelope,
    read_verdict,
    validate_envelope,
)
from rubik_core.smart_teams.instruments import disc
from rubik_core.smart_teams.scoring import ScoringReport

logger = logging.getLogger(__name__)

__all__ = [
    "MERGE_ADAPTERS",
    "PARSE_REASONS",
    "REGENERATION_STATES",
    "GuideInvalidation",
    "MergeOutcome",
    "ParseProposal",
    "UploadError",
    "confirm_upload",
    "create_upload",
    "delete_upload_bytes",
    "extract_report_text",
    "get_upload",
    "invalidate_stale_guides",
    "list_uploads",
    "load_upload_bytes",
    "merge_upload",
    "parse_report_text",
    "parse_upload",
    "reject_upload",
    "store_upload_bytes",
]

# ── Storage ───────────────────────────────────────────────────────────────────
#: Prefix for the internal storage id kept in ``st_uploads.doc_id``. Distinct
#: from ``generated_docs``' ``gendoc-`` so a stray object in the bucket is
#: identifiable, and prefixed at all so nothing mistakes it for a DAM doc_id
#: (there is no ``assets`` row behind it — see the module docstring).
DOC_ID_PREFIX = "stupload-"

#: Sub-prefix inside ``{GCS_RAG_PREFIX}/``. NOT ``rags/…``: that is the only
#: prefix ``rubik_core.rag.manager`` imports into a Vertex corpus, and an
#: official psychometric report must never be retrievable by Chat.
GCS_SUBPREFIX = "smart-teams-uploads"

#: The only extension accepted. Official reports are PDFs; every other format
#: would need its own text extraction and its own tests, and an unsupported one
#: must be refused at the door rather than stored and then found unparseable.
ALLOWED_SUFFIXES: frozenset[str] = frozenset({".pdf"})

#: Upload ceiling. A 20-page vendor report is well under a megabyte; ten of them
#: is a generous allowance and keeps a mis-aimed upload from filling the bucket.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: ``st_uploads.original_filename`` is display-only, so it is clipped rather than
#: rejected. It is never used to build a storage path (see :func:`_suffix_of`).
MAX_FILENAME_CHARS = 255

#: Pages read for text. Score tables are at the front of every report format we
#: have seen, and a 100-page appendix is narrative we pay for and discard.
MAX_REPORT_PAGES = 40

#: The AI Cost bucket for this module's one LLM call. Same string as
#: ``scoring.COST_FEATURE`` and the ``_FEATURE_LABELS`` entry in
#: ``backend/routers/admin.py``, spelled here as a literal rather than imported
#: because ``test_smart_teams_conversation`` resolves the ``@attribute_to``
#: argument statically — an imported name reads as unresolvable and the check
#: that every model call is attributed cannot run. The house pattern across
#: ``conversation.py``, ``guide.py`` and ``skill_inference.py`` for the same
#: reason; ``test_smart_teams_uploads`` asserts the two constants agree.
COST_FEATURE = "smart_teams"

# ── Parse model ───────────────────────────────────────────────────────────────
#: Resolved to a concrete Gemini id by ``priorities.agents.base._resolve_model_id``
#: → ``settings.VERTEX_LLM_MODEL_FLASH``. A KEY, never an id: model ids are
#: centralized in ``rubik_core/config.py`` and must not be written at a call site
#: or floated on a ``-latest`` alias.
PARSE_MODEL_KEY = "gemini-flash"

#: Reading a printed number off a page is a transcription job, not a creative
#: one. Same 0.2 as the strengths extraction and the chat paths.
PARSE_TEMPERATURE = 0.2

#: Total tries. A transient Vertex 429 or a truncated JSON parse usually clears
#: on the second call; past that the row records a refusal.
PARSE_ATTEMPTS = 2

# ── Refusal vocabulary ────────────────────────────────────────────────────────
# Closed, like ``contracts.REASON_CODES`` and for the same reason: the UI decides
# what to offer from it and the support read reports it, and free text would make
# both string-match on prose. Each code's sentence is OURS — never a model string
# and never an excerpt of the uploaded document.
REASON_NO_TEXT = "no_text"
REASON_UNREADABLE = "unreadable_file"
REASON_UNRECOGNIZED = "unrecognized"
REASON_UNSUPPORTED_FRAMEWORK = "unsupported_framework"
REASON_AMBIGUOUS_SCALE = "ambiguous_scale"
REASON_NO_SCORES = "no_scores"
REASON_INCOMPLETE_SCORES = "incomplete_scores"
REASON_OUT_OF_RANGE = "out_of_range"
REASON_MODEL_ERROR = "model_error"

PARSE_REASONS: Mapping[str, str] = {
    REASON_NO_TEXT: (
        "There was no readable text in this PDF. If it is a scan or a photo of a "
        f"printed report, {PRODUCT_NAME} cannot read the numbers out of it."
    ),
    REASON_UNREADABLE: (
        f"{PRODUCT_NAME} could not open this file. If it is password-protected or was only "
        "partly uploaded, try saving a fresh copy and uploading that."
    ),
    REASON_UNRECOGNIZED: (
        f"{PRODUCT_NAME} could not tell which assessment this report came from, so it has "
        "not proposed anything for your profile."
    ),
    REASON_UNSUPPORTED_FRAMEWORK: (
        f"{PRODUCT_NAME} recognised this report but cannot merge that kind of assessment "
        "yet. Nothing about your profile has changed."
    ),
    REASON_AMBIGUOUS_SCALE: (
        f"This report's numbers are on a scale {PRODUCT_NAME} cannot line up with its own "
        "without guessing, so it has not proposed anything."
    ),
    REASON_NO_SCORES: (
        f"{PRODUCT_NAME} recognised the report but could not find the scores in it."
    ),
    REASON_INCOMPLETE_SCORES: (
        "Some of this report's scores are missing, and a partial set would leave "
        "your profile describing dimensions nobody measured."
    ),
    REASON_OUT_OF_RANGE: (
        f"One of this report's numbers is outside the range {PRODUCT_NAME} expects, so it "
        "has not proposed anything rather than store a value it cannot place."
    ),
    REASON_MODEL_ERROR: (
        f"{PRODUCT_NAME} could not finish reading this report. Nothing about your profile "
        "has changed, and an admin can look at it."
    ),
}

#: The provenance value a confirmed upload writes for its own framework. The
#: string comes from ``database.smart_teams.PROVENANCE_SOURCES``; spelled as a
#: constant here so the writer and the report of what was written agree.
PROVENANCE_OFFICIAL = "official_upload"

#: Review statuses, from ``database.smart_teams.UPLOAD_REVIEW_STATUSES``.
REVIEW_PENDING = "pending"
REVIEW_CONFIRMED = "confirmed"
REVIEW_REJECTED = "rejected"
REVIEW_MERGED = "merged"

#: ST-6's module (``rubik_core/smart_teams/guide.py``). A merge advances the
#: profile version, which makes every cached guide stale by construction
#: (``document_cache_keys`` — a new version key is a cache miss). Staleness alone
#: un-publishes nothing and removes nothing from the corpus, so
#: :func:`invalidate_stale_guides` does both, and does them through ST-6's own
#: functions rather than writing the transitions a second time:
#:
#: * ``guide.unpublish`` is that module's only writer of ``published -> False``
#:   (it also leaves ``approved_at`` alone, which is what makes re-approval
#:   required rather than the record of the old consent being erased);
#: * ``guide.purge_guide_vectors`` is the only caller that passes
#:   ``delete_document_vectors`` BOTH arguments the DAM routing asymmetry needs,
#:   so it is the only way an unpublished guide actually stops answering in Chat;
#: * ``guide.is_stale`` is that module's own staleness predicate, used here to
#:   report the state rather than assert it.
#:
#: Imported lazily and read by name so a merge that already succeeded is never
#: reported as a failure because this module moved: an ``ImportError`` degrades
#: to the unpublish alone, which this module can do itself.
GUIDE_MODULE = "rubik_core.smart_teams.guide"

#: What ST-6 does NOT have as of this change: any entry point that *dispatches* a
#: regeneration (no endpoint, no cubie-worker job). So nothing here claims to have
#: requested one. :func:`invalidate_stale_guides` reports the state it can
#: actually measure — see :data:`REGENERATION_STATES` — and the person regenerates
#: from ST-6's own surface, where ``guide.is_stale`` is already what tells them to.
REGENERATION_STATES: tuple[str, ...] = (
    "unavailable",  # ST-6's module could not be read; nothing was measured.
    "no_guide",  # This person has no guide row at all. The common case.
    "stale",  # A guide exists and no longer matches the version just written.
    "current",  # A guide already cites the new version. Nothing to redo.
)

#: Audit actions. Readable through ``GET /api/admin/audit?action=…``.
ACTION_PARSE_FAILED = "SMART_TEAMS_UPLOAD_PARSE_FAILED"
ACTION_MERGED = "SMART_TEAMS_UPLOAD_MERGED"
ACTION_STALE_GUIDE_INGESTED = "SMART_TEAMS_STALE_GUIDE_STILL_INGESTED"


class UploadError(RuntimeError):
    """An upload could not be stored, parsed, or merged.

    Raised rather than returned for the same reason ``ProfileError`` is: every
    caller is an HTTP handler or a cubie-worker job, and both need to tell "wrote
    a new version" apart from "wrote nothing". A silent no-op here reads
    downstream as a person who never uploaded anything.
    """


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _suffix_of(filename: str | None) -> str:
    """The validated extension for a stored object. Never the client's path.

    Only the suffix of the client-supplied name is ever used, and only after it
    matches :data:`ALLOWED_SUFFIXES` — the full name goes in a column for
    display and nowhere near a filesystem path or a GCS blob name. A name like
    ``../../etc/passwd.pdf`` therefore contributes the four characters
    ``.pdf`` and nothing else.
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise UploadError(
            f"{PRODUCT_NAME} can read official reports as PDF files. "
            f"{sorted(ALLOWED_SUFFIXES)} is the whole list."
        )
    return suffix


def _gcs_blob_name(doc_id: str, suffix: str) -> str:
    from rubik_core.tenancy.storage import object_name

    return object_name(GCS_SUBPREFIX, f"{doc_id}{suffix}")


def store_upload_bytes(doc_id: str, filename: str, data: bytes) -> str:
    """Persist an uploaded report; return the URI recorded in ``gcs_uri``.

    Mirrors ``backend/services/generated_docs.store_doc_bytes`` — same bucket,
    same ``GCS_RAG_PREFIX``, same ``LOCAL_DATA_DIR`` fallback — under
    :data:`GCS_SUBPREFIX` with the file's real content type, and outside the
    ``rags/`` prefix so no corpus can sweep it up.
    """
    from rubik_core.config import AppMode, settings

    suffix = _suffix_of(filename)
    blob_name = _gcs_blob_name(doc_id, suffix)
    if settings.APP_MODE == AppMode.GCP:
        from google.cloud import storage

        client = storage.Client(project=settings.GCP_PROJECT_ID)
        blob = client.bucket(settings.GCS_BUCKET_NAME).blob(blob_name)
        blob.upload_from_string(data, content_type="application/pdf")
        return f"gs://{settings.GCS_BUCKET_NAME}/{blob_name}"

    from rubik_core.tenancy.storage import local_data_dir

    local_dir = local_data_dir() / GCS_SUBPREFIX
    local_dir.mkdir(parents=True, exist_ok=True)
    path = local_dir / blob_name.rsplit("/", 1)[-1]
    path.write_bytes(data)
    return path.as_uri()


def load_upload_bytes(source_path: str) -> bytes:
    """Read a stored report back from a ``gs://`` URI or a local path."""
    from rubik_core.config import settings
    from rubik_core.tenancy.storage import assert_own_uri

    checked = assert_own_uri(source_path)
    if source_path.startswith("gs://"):
        from google.cloud import storage

        bucket_name, _, blob_name = checked[len("gs://") :].partition("/")
        client = storage.Client(project=settings.GCP_PROJECT_ID)
        return client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    return Path(checked).read_bytes()


def delete_upload_bytes(source_path: str | None) -> None:
    """Best-effort removal of a stored report. Never blocks the operation."""
    if not source_path:
        return
    from rubik_core.tenancy.storage import assert_own_uri

    checked = assert_own_uri(source_path)
    try:
        if source_path.startswith("gs://"):
            from google.cloud import storage

            from rubik_core.config import settings

            bucket_name, _, blob_name = checked[len("gs://") :].partition("/")
            client = storage.Client(project=settings.GCP_PROJECT_ID)
            client.bucket(bucket_name).blob(blob_name).delete()
        else:
            Path(checked).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — cleanup must never block the operation
        logger.warning(
            "[smart-teams] could not delete upload object %s",
            source_path,
            exc_info=True,
        )


# ── Text extraction ───────────────────────────────────────────────────────────


def extract_report_text(data: bytes) -> tuple[str, bool]:
    """PDF bytes -> ``(text, truncated)``. The text is NEVER persisted.

    PyMuPDF, the same reader ``rubik_core/ingestion/loader.py`` uses for PDFs, so
    there is one PDF text path in the repo. Bounded twice — :data:`MAX_REPORT_PAGES`
    and ``upload_prompts.MAX_REPORT_CHARS`` — and the second bound is *reported*
    rather than swallowed, because a score table that fell off the end must reach
    the model as "not in what you were shown" instead of silently not existing.

    Returns ``("", False)`` for a PDF with no text layer (a scan). That is a
    refusal, not a failure: :func:`parse_upload` records
    :data:`REASON_NO_TEXT`.
    """
    import fitz  # PyMuPDF

    chunks: list[str] = []
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            for index, page in enumerate(doc):
                if index >= MAX_REPORT_PAGES:
                    break
                chunks.append(page.get_text())
    except Exception as exc:  # noqa: BLE001 — a bad PDF is a refusal, not a 500
        raise UploadError(f"{PRODUCT_NAME} could not open this file as a PDF.") from exc
    text = "\n".join(chunks).strip()
    if len(text) > up.MAX_REPORT_CHARS:
        return text[: up.MAX_REPORT_CHARS], True
    return text, False


# ── The parse proposal ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParseProposal:
    """What one parse proposed, and nothing it should not have.

    Two framework fields, because they answer two different questions and
    conflating them is how a report Cubie cannot map reaches a merge.

    * ``detected`` — which instrument this report is, as far as we can tell.
      Recorded in ``st_uploads.framework`` so the person is told *what* they
      uploaded even when nothing can be done with it, and ``None`` when we
      genuinely could not tell.
    * ``framework`` — the layer this proposal may be merged into. Non-``None``
      only when ``payload`` is too, so "a framework with no payload" and "a
      payload for no framework" are both unrepresentable rather than states every
      caller has to remember to check.

    ``reason`` is a key of :data:`PARSE_REASONS` when this proposal cannot merge,
    and ``None`` when it can. ``dropped_keys`` counts what
    :func:`_coerce_proposal` threw away — the honest measure of how far off
    contract the model's answer was, and the number a test asserts on to prove
    vendor prose did not land in a column.
    """

    framework: str | None
    payload: Mapping[str, Any] | None
    detected: str | None = None
    reason: str | None = None
    dropped_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.framework is None) != (self.payload is None):
            raise UploadError(
                "ParseProposal: framework and payload are set together or not at "
                "all — a half-set proposal is what a merge would act on"
            )
        if self.reason is not None and self.reason not in PARSE_REASONS:
            raise UploadError(
                f"{self.reason!r} is not in PARSE_REASONS; add it there in the "
                "same change that emits it, so the UI and the support read can "
                "read it"
            )

    @property
    def mergeable(self) -> bool:
        return self.framework is not None and self.payload is not None

    @property
    def message(self) -> str | None:
        """Our own sentence for ``reason``, or ``None``. Never a model string."""
        return None if self.reason is None else PARSE_REASONS[self.reason]


def _refusal(
    reason: str,
    dropped: tuple[str, ...] = (),
    *,
    detected: str | None = None,
) -> ParseProposal:
    return ParseProposal(
        framework=None,
        payload=None,
        detected=detected,
        reason=reason,
        dropped_keys=dropped,
    )


def _assert_no_prose(value: object, *, path: str = "payload") -> None:
    """Refuse anything string-shaped that is not a closed-vocabulary member.

    The control that makes the "no vendor prose" claim in the module docstring
    true, rather than the prompt that asks for it. Walks the coerced payload and
    raises on the first string leaf outside :data:`_CLOSED_VOCABULARY`, so a
    future adapter that adds a free-text field to the contract fails a test
    instead of shipping a channel for somebody else's copy.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_no_prose(key, path=f"{path}[key]")
            _assert_no_prose(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_prose(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if value not in _CLOSED_VOCABULARY:
            raise UploadError(
                f"{path}: {value[:40]!r} is not a closed-vocabulary value. The "
                "parse contract has no free-text field, and a stored payload "
                "must not be able to carry a vendor's writing."
            )
        return
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return
    raise UploadError(f"{path}: {type(value).__name__} is not a storable leaf")


#: Every string a coerced payload may contain. Assembled from the vocabularies
#: the contract actually names, so adding a value to one of those tuples cannot
#: forget to widen this.
_CLOSED_VOCABULARY: frozenset[str] = frozenset(
    {
        *PROFILE_FRAMEWORKS,
        *up.SCALES,
        *disc.DIMENSIONS,
        "framework",
        "scale",
        "disc",
    }
)

#: The only top-level keys :data:`~rubik_core.smart_teams.upload_prompts.OUTPUT_CONTRACT`
#: names. Everything else in a model response is dropped and counted.
_CONTRACT_KEYS: frozenset[str] = frozenset({"framework", "scale", "disc"})

#: Scales whose numbers are directly comparable to our own 0-100 intensity (a
#: share of the answerable range). ``percentile`` is a rank against a vendor norm
#: group — a different quantity — and ``unknown`` is not an answer, so both are
#: refused rather than rescaled on a guess.
_ACCEPTED_SCALES: frozenset[str] = frozenset({"zero_to_100", "percent"})


def _number(value: object) -> float | None:
    """A numeric leaf, or ``None``. Booleans are not numbers here.

    ``isinstance(True, int)`` is ``True`` in Python, and a model that answers
    ``true`` for a dimension would otherwise store an intensity of 1.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce_proposal(raw: object) -> ParseProposal:
    """Model output -> a :class:`ParseProposal`, dropping everything off contract.

    The whole of the type and range checking, in one place, so a caller never
    sees a shape the contract does not describe. Nothing raises for bad model
    output: an off-contract answer is a *refusal* with a code, because a person
    uploading their own report should be told what happened rather than shown a
    500.
    """
    if not isinstance(raw, Mapping):
        return _refusal(REASON_MODEL_ERROR)

    dropped = tuple(sorted(str(key) for key in raw if key not in _CONTRACT_KEYS))

    raw_framework = raw.get("framework")
    detected = (
        (raw_framework or "").strip().lower() if isinstance(raw_framework, str) else ""
    ) or None
    if detected is None or detected not in PROFILE_FRAMEWORKS:
        # A name we do not have is the same fact as no name at all: we do not
        # know what this report is. NULL framework, and nothing merges.
        return _refusal(REASON_UNRECOGNIZED, dropped)
    if detected not in MERGE_ADAPTERS:
        # Recognized, so the person is told which instrument it is — but there is
        # no adapter that can turn it into one of our layers, so ``framework``
        # (the mergeable one) stays None. See the module docstring for why each
        # of the other five is absent rather than pending.
        return _refusal(REASON_UNSUPPORTED_FRAMEWORK, dropped, detected=detected)

    scale = raw.get("scale")
    scale = (scale if isinstance(scale, str) else "").strip().lower()
    if scale not in _ACCEPTED_SCALES:
        return _refusal(REASON_AMBIGUOUS_SCALE, dropped, detected=detected)

    scores_raw = raw.get(detected)
    if not isinstance(scores_raw, Mapping):
        return _refusal(REASON_NO_SCORES, dropped, detected=detected)

    numbers: dict[str, float] = {}
    for key in disc.DIMENSIONS:
        value = _number(scores_raw.get(key))
        if value is None:
            continue
        if not 0.0 <= value <= 100.0:
            return _refusal(REASON_OUT_OF_RANGE, dropped, detected=detected)
        numbers[key] = value
    if not numbers:
        return _refusal(REASON_NO_SCORES, dropped, detected=detected)
    if len(numbers) < len(disc.DIMENSIONS):
        # A partial DISC set produces ``intensity=None`` dimensions, which
        # ``contracts._disc_verdict`` correctly refuses to publish — so merging
        # it would replace a reportable interview layer with one that says
        # nothing. Refuse here, where we can say why.
        return _refusal(REASON_INCOMPLETE_SCORES, dropped, detected=detected)

    payload = {"framework": detected, "scale": scale, detected: numbers}
    _assert_no_prose(payload)
    return ParseProposal(
        framework=detected,
        payload=payload,
        detected=detected,
        reason=None,
        dropped_keys=dropped,
    )


@attribute_to(COST_FEATURE)
def parse_report_text(
    report_text: str,
    *,
    truncated: bool = False,
    model_key: str = PARSE_MODEL_KEY,
    temperature: float = PARSE_TEMPERATURE,
    attempts: int = PARSE_ATTEMPTS,
    client: Any | None = None,
) -> ParseProposal:
    """One Gemini call: report text -> a coerced proposal. The LLM worker.

    ``@attribute_to`` sits HERE, on the function that makes the call, not on
    :func:`parse_upload`: the AI Cost tag is a contextvar and a thread hop loses
    a tag set at the entry point — the exact way the priorities batch's spend
    ended up in the "Unattributed" row.

    Never raises for a bad answer. A Vertex failure or unparseable JSON comes
    back as :data:`REASON_MODEL_ERROR`, because the person on the other end
    uploaded their own report and is owed a sentence, not a stack trace.

    ``client`` is an injection point for tests, matching
    ``scoring.extract_strengths``' ``extractor`` parameter: pass anything with
    ``invoke_json(prompt, fallback=..., attempts=...)``.
    """
    if not report_text.strip():
        return _refusal(REASON_NO_TEXT)

    llm = client if client is not None else _json_client(model_key, temperature)
    prompt = up.parse_prompt(report_text, truncated=truncated)
    try:
        # fallback=None so the client re-raises rather than handing back a shape
        # that coerces to "no framework". A Vertex outage and a report we cannot
        # identify must not look the same on somebody's upload.
        raw = llm.invoke_json(prompt, fallback=None, attempts=attempts)
    except Exception as exc:  # noqa: BLE001 — a model failure is a refusal
        logger.warning("[smart-teams] official-report parse call failed: %s", exc)
        return _refusal(REASON_MODEL_ERROR)
    return _coerce_proposal(raw)


def _json_client(model_key: str, temperature: float):
    """The house JSON-over-Gemini client, imported lazily.

    Lazy for the same reason ``scoring._json_client`` is: importing this module
    must not pull in the priorities package, which builds a genai client on
    first use.
    """
    from rubik_core.priorities.agents.base import LLMJsonClient

    return LLMJsonClient(model_key=model_key, temperature=temperature)


# ── Framework adapters ────────────────────────────────────────────────────────


def _disc_from_upload(payload: Mapping[str, Any]) -> disc.DiscResult:
    """Four vendor intensities -> the same ``DiscResult`` an interview produces.

    Everything except the four numbers is OURS. ``style_for_scores`` is the pure
    function ``score_disc`` uses, so the style name, the blend decision, the
    margins and the confidence are computed by our taxonomy from the vendor's
    numbers — which is the whole reason a vendor's style name is never needed and
    never stored.

    ``ResponseQuality`` is constructed with zero answered items and no computed
    statistics, and that is a statement of fact rather than a clean bill of
    health: this sitting happened somewhere else, so none of our response-pattern
    indices *can* be computed, and every field says so. ``flags`` is empty
    because there were no responses of ours to flag — a reader who asks "why is
    my C a 71" is shown ``likert_answered: 0`` and the ``official_upload``
    provenance, which is the honest answer. What this does NOT do is invent a
    flag, or claim a check ran.
    """
    numbers = payload["disc"]
    scores: dict[str, disc.DimensionScore] = {}
    for dimension in disc.DIMENSIONS:
        intensity = max(0, min(100, int(round(float(numbers[dimension])))))
        scores[dimension] = disc.DimensionScore(
            dimension=dimension,
            intensity=intensity,
            band=disc.intensity_band(intensity),
            # No items of ours were answered. Reported, not hidden.
            likert_answered=0,
            likert_raw=None,
            likert_percent=None,
            choice_answered=0,
            choice_wins=0,
            choice_percent=None,
        )
    assignment = disc.style_for_scores({d: s.intensity for d, s in scores.items()})
    quality = disc.ResponseQuality(
        likert_answered=0,
        choice_answered=0,
        longest_run=0,
        response_variability=None,
        response_range=None,
        mean_pair_discrepancy=None,
        severe_pairs=0,
        position_share=None,
        seconds_per_item=None,
        flags=[],
    )
    return disc.DiscResult(scores=scores, assignment=assignment, quality=quality)


#: Framework -> the function that turns a coerced payload into that instrument's
#: own result object. Membership in this registry is what "mergeable" means, and
#: :func:`_coerce_proposal` consults it, so a framework we can detect but not
#: merge can never reach :func:`merge_upload`. One entry today; the module
#: docstring says why each of the other five is absent rather than pending.
MERGE_ADAPTERS: Mapping[str, Any] = {"disc": _disc_from_upload}


# ── Row lifecycle ─────────────────────────────────────────────────────────────


def create_upload(
    db: Session, user_email: str, *, filename: str, data: bytes
) -> SmartTeamsUpload:
    """Store the bytes and insert the ``pending`` row. Parses nothing.

    The subject is ``user_email`` and there is no parameter for anybody else:
    an upload is a person handing us a report about themselves, and a
    ``subject_email`` argument here would be an impersonation primitive over the
    most private data in the app.
    """
    email = _fold(user_email)
    if not email:
        raise UploadError("An upload needs an identified owner.")
    if not data:
        raise UploadError("That file was empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"That file is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
            "limit for an official report."
        )
    # Validates the extension BEFORE anything is written or stored.
    _suffix_of(filename)

    doc_id = f"{DOC_ID_PREFIX}{uuid.uuid4().hex[:16]}"
    uri = store_upload_bytes(doc_id, filename, data)
    row = SmartTeamsUpload(
        id=uuid.uuid4().hex,
        user_email=email,
        doc_id=doc_id,
        gcs_uri=uri,
        original_filename=(filename or "")[:MAX_FILENAME_CHARS],
        framework=None,
        parsed=None,
        parse_error=None,
        review_status=REVIEW_PENDING,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "[smart-teams] upload %s stored for %s (%d bytes)", row.id[:8], email, len(data)
    )
    return row


def get_upload(
    db: Session, upload_id: str, owner_email: str
) -> SmartTeamsUpload | None:
    """One upload, **only** if ``owner_email`` owns it.

    The owner predicate is part of the query rather than a check after the read,
    so there is no shape of this function that returns somebody else's row. A
    caller that holds an id it did not create gets ``None`` and therefore a 404,
    which also refuses to confirm that the id exists.
    """
    email = _fold(owner_email)
    if not email or not upload_id:
        return None
    return (
        db.query(SmartTeamsUpload)
        .filter(
            SmartTeamsUpload.id == upload_id,
            func.lower(SmartTeamsUpload.user_email) == email,
        )
        .one_or_none()
    )


def list_uploads(db: Session, user_email: str) -> list[SmartTeamsUpload]:
    """Every upload this person made, newest first. ``[]`` is the normal answer."""
    email = _fold(user_email)
    if not email:
        return []
    return (
        db.query(SmartTeamsUpload)
        .filter(func.lower(SmartTeamsUpload.user_email) == email)
        .order_by(SmartTeamsUpload.uploaded_at.desc())
        .all()
    )


def parse_upload(
    db: Session,
    upload_id: str,
    *,
    parser: Any | None = None,
) -> ParseProposal:
    """Read one stored report and record what it *proposes*. Merges nothing.

    Runs on cubie-worker (``backend/jobs/smart_teams_parse_job.py``). Not in the
    web process: Cloud Run throttles the service's CPU to near-zero once the
    response returns, so a ``BackgroundTask`` doing a PDF read plus a Vertex call
    is starved silently and the upload sits ``pending`` forever with nothing
    saying why.

    Idempotent on ``review_status``: a row the person has already decided on
    (``confirmed`` / ``rejected`` / ``merged``) is left exactly as it is, because
    a Cloud Run retry arriving after a confirmation must not re-propose over a
    decision. Re-parsing a still-``pending`` row is safe and overwrites the
    proposal.

    ``parser`` is the injection point for tests — anything with the signature of
    :func:`parse_report_text`.
    """
    row = db.get(SmartTeamsUpload, upload_id)
    if row is None:
        raise UploadError(f"no upload {upload_id!r}")
    if row.review_status != REVIEW_PENDING:
        logger.info(
            "[smart-teams] upload %s is already %s; leaving the proposal alone",
            upload_id[:8],
            row.review_status,
        )
        stored = row.parsed if isinstance(row.parsed, Mapping) else None
        mergeable = (row.framework or "").lower() in MERGE_ADAPTERS and stored
        return ParseProposal(
            framework=row.framework if mergeable else None,
            payload=dict(stored) if mergeable else None,
            detected=row.framework,
            reason=None if mergeable else REASON_UNRECOGNIZED,
        )

    run = parser or parse_report_text
    try:
        data = load_upload_bytes(row.gcs_uri or "")
        text, truncated = extract_report_text(data)
    except UploadError as exc:
        # A file we cannot open or read is a refusal with a code, not a crash.
        logger.warning(
            "[smart-teams] upload %s could not be read: %s", upload_id[:8], exc
        )
        proposal = _refusal(REASON_UNREADABLE)
    else:
        proposal = run(text, truncated=truncated)

    # ``framework`` records what we KNOW the report is, which is deliberately not
    # the same question as what we can merge: a recognized-but-unsupported
    # instrument keeps its name so the person is told which one it is, while
    # ``parsed`` stays NULL and the row can never merge. Everything we could not
    # identify records NULL — the honest value, and the reason the column is
    # nullable.
    row.framework = proposal.detected
    row.parsed = dict(proposal.payload) if proposal.payload is not None else None
    row.parse_error = proposal.message
    row.parsed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)

    logger.info(
        "[smart-teams] upload %s parsed: framework=%s mergeable=%s reason=%s "
        "dropped=%d",
        upload_id[:8],
        row.framework or "-",
        proposal.mergeable,
        proposal.reason or "-",
        len(proposal.dropped_keys),
    )
    if proposal.dropped_keys:
        # Not an error — the coercion did its job. Logged because a model that
        # keeps adding fields is a prompt that has drifted.
        logger.info(
            "[smart-teams] upload %s: dropped %s off-contract keys from the "
            "parse response",
            upload_id[:8],
            len(proposal.dropped_keys),
        )
    if proposal.reason in _OPERATIONAL_REASONS:
        # An operational failure, as distinct from a report we read correctly and
        # cannot use. Only these get an audit row, so the admin view answers
        # "which parses BROKE" rather than "which uploads were unusable".
        _record_audit(
            db,
            actor_email=_fold(row.user_email),
            action=ACTION_PARSE_FAILED,
            target=upload_id,
            details={"reason": proposal.reason},
        )
    return proposal


#: Refusals that mean something went wrong on our side rather than something
#: being wrong with the report. These are worth an operator's attention; the
#: rest are ordinary outcomes of reading a document we cannot use.
_OPERATIONAL_REASONS: frozenset[str] = frozenset(
    {REASON_MODEL_ERROR, REASON_UNREADABLE}
)


# ── Confirm / reject ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MergeOutcome:
    """What a confirmation actually did.

    ``profile_version`` is the number of the version that was written.
    ``guides`` is what happened to the documents generated from the version this
    superseded — the consequence the person has to be told about, not a detail.
    """

    upload_id: str
    framework: str
    profile_id: str
    profile_version: int
    provenance: Mapping[str, str]
    guides: GuideInvalidation
    #: Frameworks whose payload and provenance this merge did not touch.
    untouched_frameworks: tuple[str, ...] = ()

    @property
    def unpublished_guides(self) -> tuple[str, ...]:
        """The guides this merge took out of company view."""
        return self.guides.unpublished

    @property
    def regeneration(self) -> str:
        """One of :data:`REGENERATION_STATES`. Measured, never a claim of dispatch."""
        return self.guides.regeneration


def _claim(db: Session, row: SmartTeamsUpload, new_status: str) -> bool:
    """Move ``pending`` -> ``new_status`` with a conditional UPDATE.

    The decision is the claim ticket, evaluated by the database rather than by a
    check-then-act read that two clicks would sail through together: whoever's
    UPDATE matches zero rows lost, and the caller reports the decision that
    already stands instead of merging twice. On Postgres the loser blocks on the
    winner's row lock and re-evaluates the predicate after the commit.
    """
    claimed = (
        db.query(SmartTeamsUpload)
        .filter(
            SmartTeamsUpload.id == row.id,
            SmartTeamsUpload.review_status == REVIEW_PENDING,
        )
        .update(
            {"review_status": new_status, "reviewed_at": datetime.utcnow()},
            synchronize_session=False,
        )
    )
    db.commit()
    return bool(claimed)


def reject_upload(db: Session, upload_id: str, owner_email: str) -> SmartTeamsUpload:
    """The person says no. The profile is not touched, and the row is kept.

    Kept rather than deleted for the same reason an abandoned interview is: they
    uploaded something, an admin may be asked what happened to it, and a delete
    would take that record along with the answer. The stored file stays too —
    it is theirs, and a rejected parse is the case a support request is most
    likely to be about.
    """
    row = get_upload(db, upload_id, owner_email)
    if row is None:
        raise UploadError(f"no upload {upload_id!r} for this person")
    if row.review_status == REVIEW_REJECTED:
        return row
    if not _claim(db, row, REVIEW_REJECTED):
        db.refresh(row)
        raise UploadError(
            f"This upload was already {row.review_status}, so it cannot be "
            "rejected now."
        )
    db.refresh(row)
    logger.info("[smart-teams] upload %s rejected by its subject", upload_id[:8])
    return row


def confirm_upload(
    db: Session,
    upload_id: str,
    owner_email: str,
    *,
    framework: str | None = None,
) -> MergeOutcome:
    """The person confirms the mapping, and only then does anything merge.

    ``framework`` is an *echo*, not a choice: when supplied it must equal the
    framework the parse proposed, and a mismatch is refused. That makes a
    confirmation a statement about the proposal the person actually read — if a
    re-parse changed it under them, they confirm the new one deliberately. It is
    never an identity and never selects a target layer: pointing a vendor's
    numbers at a framework they did not come from is exactly what the review step
    exists to prevent.

    The claim (``pending`` -> ``confirmed``) commits first, so a second click
    cannot start a second merge: the loser's conditional UPDATE matches zero rows
    and it reports the decision that already stands. Retrying a row that is
    already ``confirmed`` is deliberately allowed, because that is what a
    half-finished merge looks like from outside.

    Two windows exist between the claim and ``review_status='merged'``, and they
    are not the same:

    * a failure **before** ``write_profile_version`` leaves the row ``confirmed``
      with no profile. Visible, retryable, and the retry does exactly the right
      thing.
    * a failure **after** its commit but before this function's own commit leaves
      the row ``confirmed`` next to a profile version that was written. A retry
      then appends a *second* version from the same report. That is not a
      corrupted profile — the numbers are identical, ``is_current`` still names
      exactly one row, and the guide is invalidated again — but the history shows
      two versions for one report, and closing that window properly needs the
      row update to share a transaction with the insert, which means a change
      inside ``profile.write_profile_version`` (it commits its own).
    """
    row = get_upload(db, upload_id, owner_email)
    if row is None:
        raise UploadError(f"no upload {upload_id!r} for this person")

    if row.review_status == REVIEW_MERGED:
        raise UploadError("This report has already been merged into your profile.")
    if row.review_status == REVIEW_REJECTED:
        raise UploadError(
            "This report was rejected. Upload it again if you want it merged."
        )

    proposed = (row.framework or "").strip().lower() or None
    if proposed is None or proposed not in MERGE_ADAPTERS or not row.parsed:
        raise UploadError(row.parse_error or PARSE_REASONS[REASON_UNRECOGNIZED])
    if framework is not None and _fold(framework) != proposed:
        raise UploadError(
            "The proposal for this report has changed since you looked at it. "
            "Open it again before confirming."
        )

    if row.review_status == REVIEW_PENDING and not _claim(db, row, REVIEW_CONFIRMED):
        db.refresh(row)
        raise UploadError(
            f"This upload was already {row.review_status}; nothing was merged twice."
        )
    db.refresh(row)

    return merge_upload(db, row)


# ── The merge ─────────────────────────────────────────────────────────────────


def _carried_envelope(
    profile: SmartTeamsProfile | None, name: str, scored_at: datetime
) -> dict[str, Any]:
    """The payload the previous version holds for ``name``, verbatim.

    Validated on the way through, and a malformed one **refuses the merge**
    rather than being written forward: a version is append-only and permanent, so
    copying a broken payload into a new row multiplies the problem instead of
    ending it. A column that is genuinely NULL — no profile at all, or an
    ``admin_seed`` row — becomes ``not_taken_envelope``, which is the shape every
    reader already handles and is not the same claim as NULL.
    """
    stored = getattr(profile, name, None) if profile is not None else None
    if stored is None:
        return not_taken_envelope(name, scored_at=scored_at)
    try:
        validate_envelope(stored)
    except PayloadError as exc:
        raise UploadError(
            f"the current profile's {name} payload is off-contract ({exc}); "
            "refusing to carry it into a new version"
        ) from None
    return dict(stored)


def build_merge_report(
    profile: SmartTeamsProfile | None,
    framework: str,
    result: object,
    *,
    user_email: str,
    scored_at: datetime | None = None,
) -> ScoringReport:
    """The ``ScoringReport`` a merge hands ``profile.write_profile_version``.

    Constructed rather than scored: ``failures`` is empty because no instrument
    ran and nothing failed, and every framework other than ``framework`` carries
    the previous version's envelope and provenance entry unchanged.

    Provenance for the untouched five is taken from the stored map **verbatim**,
    including any value this build does not recognise — "untouched" has to mean
    untouched, and normalizing somebody else's value here would quietly rewrite
    history. Only a framework the stored map has no entry for at all falls back
    to ``default_provenance()``, whose docstring explains why "interview" is the
    honest default.
    """
    email = _fold(user_email)
    now = scored_at or datetime.utcnow()
    if framework not in FRAMEWORKS:
        raise UploadError(f"{framework!r} is not one of {FRAMEWORKS}")

    envelope = envelope_for(framework, result, scored_at=now)
    stored_provenance = dict(getattr(profile, "provenance", None) or {})
    defaults = default_provenance()

    outcomes: dict[str, LayerOutcome] = {}
    provenance: dict[str, str] = {}
    for name in FRAMEWORKS:
        if name == framework:
            outcomes[name] = LayerOutcome(
                framework=name,
                envelope=envelope,
                verdict=read_verdict(envelope),
                result=result,
            )
            provenance[name] = PROVENANCE_OFFICIAL
            continue
        carried = _carried_envelope(profile, name, now)
        outcomes[name] = LayerOutcome(
            framework=name,
            envelope=carried,
            verdict=read_verdict(carried),
            result=None,
        )
        provenance[name] = str(stored_provenance.get(name) or defaults[name])

    return ScoringReport(
        output=ScoringOutput(
            user_email=email, outcomes=outcomes, provenance=provenance
        ),
        failures={},
        scored_at=now,
    )


def merge_upload(db: Session, row: SmartTeamsUpload) -> MergeOutcome:
    """Write the new profile version this confirmed upload earns. One version.

    Delegates every hard part of the write to ``profile.write_profile_version``:
    ``max(version) + 1`` under ``uq_st_profile_user_version``, retirement of the
    predecessor through instance assignment so ``is_current`` becomes NULL and
    never ``False``, and the collision retry. This module does not renumber
    versions and must never learn how.

    Then, and only then, the consequence: the person's cached guide was generated
    from the version this just superseded, so :func:`invalidate_stale_guides`
    takes it out of company view.
    """
    from rubik_core.smart_teams.profile import (
        ProfileError,
        current_profile,
        write_profile_version,
    )

    email = _fold(row.user_email)
    framework = (row.framework or "").strip().lower()
    adapter = MERGE_ADAPTERS.get(framework)
    if adapter is None:
        raise UploadError(
            f"there is no merge adapter for {framework!r}; a report {PRODUCT_NAME} cannot "
            "map must not reach a profile"
        )
    payload = row.parsed
    if not isinstance(payload, Mapping):
        raise UploadError("this upload has no parsed payload to merge")

    result = adapter(payload)
    previous = current_profile(db, email)
    report = build_merge_report(
        previous, framework, result, user_email=email, scored_at=datetime.utcnow()
    )
    try:
        # ``origin`` is PROFILE_ORIGINS and ``provenance`` is PROVENANCE_SOURCES —
        # two vocabularies that happen to share this spelling. Passed as the same
        # constant because they mean the same thing here (this whole version
        # exists because of an official upload), not because the vocabularies are
        # interchangeable.
        write = write_profile_version(
            db, report, origin=PROVENANCE_OFFICIAL, status="current"
        )
    except ProfileError as exc:
        raise UploadError(f"could not write a profile version: {exc}") from None

    row.review_status = REVIEW_MERGED
    row.merged_profile_id = write.profile.id
    row.reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)

    guides = invalidate_stale_guides(db, email, current_version=write.version)
    untouched = tuple(name for name in FRAMEWORKS if name != framework)

    _record_audit(
        db,
        actor_email=email,
        action=ACTION_MERGED,
        target=row.id,
        status="success",
        details={
            "framework": framework,
            "profile_id": write.profile.id,
            "version": write.version,
            "provenance": dict(report.output.provenance),
            "unpublished_guides": list(guides.unpublished),
            "guide_chunks_still_ingested": list(guides.purge_failed),
        },
    )
    logger.info(
        "[smart-teams] upload %s merged into profile %s v%d for %s "
        "(framework=%s untouched=%s unpublished_guides=%d regeneration=%s)",
        row.id[:8],
        write.profile.id[:8],
        write.version,
        email,
        framework,
        ",".join(untouched),
        len(guides.unpublished),
        guides.regeneration,
    )
    return MergeOutcome(
        upload_id=row.id,
        framework=framework,
        profile_id=write.profile.id,
        profile_version=write.version,
        provenance=dict(report.output.provenance),
        guides=guides,
        untouched_frameworks=untouched,
    )


# ── The consequence: a merge invalidates the guide ────────────────────────────


@dataclass(frozen=True)
class GuideInvalidation:
    """What a merge did to this person's guides. Four separate facts.

    ``unpublished`` and ``purged`` are deliberately not one number. A guide can
    be out of company view and still be answerable in Chat — its chunks are in
    the companywide corpus until the purge proves otherwise — and reporting one
    count would let the second failure hide behind the first success.
    ``purge_failed`` is the case an operator has to act on, and it carries an
    audit row as well as a place in this tuple.
    """

    unpublished: tuple[str, ...] = ()
    purged: tuple[str, ...] = ()
    purge_failed: tuple[str, ...] = ()
    regeneration: str = "unavailable"


def invalidate_stale_guides(
    db: Session, user_email: str, *, current_version: int
) -> GuideInvalidation:
    """Take every guide generated from an older profile version out of company view.

    **Why unpublishing rather than only letting the cache miss.**
    ``document_cache_keys`` already makes a new profile version a cache miss, so
    nothing will *serve* a stale guide from the cache path. But the stale row is
    still sitting there with ``published=True``, and ``authz.can_view_guide``
    grants any employee a published guide. So a person whose DISC layer just
    changed would keep a company-visible document describing the numbers they
    replaced, until somebody regenerated it. Clearing ``published`` (and stamping
    ``unpublished_at``, never deleting — unpublishing is a first-class state) is
    what makes "a merge invalidates the guide" true rather than aspirational. It
    also means re-approval is required, which is the point: the document changed,
    so the consent has to be given again.

    Pair and team guides are included. They cite this person's profile version in
    their own ``profile_version_key``, and a pair guide written against the
    version this merge superseded is describing a person who has since told us
    something different.

    ``approved_at`` is left alone. The CHECK constraint is "NOT published OR
    approved_at IS NOT NULL", so clearing ``published`` is always legal, and the
    record that they once approved *that* version is worth keeping.

    **And the corpus copy.** A published guide is RAG-ingested, so unpublishing
    alone would leave Chat quoting the wording this merge invalidated. Every row
    taken out of view here is handed to ``guide.purge_guide_vectors``, which is
    the only function that passes ``delete_document_vectors`` both the
    ``source_path`` and the ``is_company_wide`` flag the DAM routing asymmetry
    needs. A purge that returns ``False`` leaves ``rag_ingested`` true — the
    honest value — and gets an audit row, because "unpublished but still
    answerable" is a state an operator has to close by hand.

    Regeneration itself belongs to ST-6, which as of this change has no entry
    point that dispatches one. So nothing here claims to have requested a
    regeneration; the returned ``regeneration`` is a state measured through that
    module's own ``is_stale`` predicate, one of :data:`REGENERATION_STATES`.
    """
    email = _fold(user_email)
    if not email:
        return GuideInvalidation()

    guide = _guide_module()

    # Built through the canonicalizer rather than by string concatenation: it is
    # the only definition of these keys, and a second one would drift.
    _, fresh_segment = document_cache_keys(
        "personal_guide", [email], {email: current_version}
    )
    own_key = subject_key_for("personal_guide", [email])

    # Prefilter in SQL — the pipe-delimited key contains the address — then match
    # an exact segment in Python, because a LIKE alone also matches a colleague
    # whose address is a superstring of this one.
    rows = (
        db.query(SmartTeamsDocument)
        .filter(SmartTeamsDocument.subject_key.like(f"%{own_key}%"))
        .all()
    )
    now = datetime.utcnow()
    unpublished: list[str] = []
    purged: list[str] = []
    purge_failed: list[str] = []
    for row in rows:
        if email not in (row.subject_key or "").split("|"):
            continue
        if not row.published:
            continue
        if fresh_segment in (row.profile_version_key or "").split("|"):
            # This document already cites the version we just wrote — for a
            # personal guide that means it is not stale at all, and for a shared
            # one it means this person's half of it is current.
            continue

        # ST-6's ``unpublish`` when it is readable, so there is one writer of
        # this transition; the same two assignments here when it is not, because
        # the merge has already happened and this must not be skipped.
        unpublish = getattr(guide, "unpublish", None) if guide else None
        if unpublish is not None:
            unpublish(db, row, now=now)
        else:
            row.published = False
            row.unpublished_at = now
        unpublished.append(row.id)

        if row.rag_ingested:
            if not _purge_guide_chunks(db, guide, row, email=email):
                purge_failed.append(row.id)
            else:
                purged.append(row.id)
    if unpublished:
        db.commit()
        logger.info(
            "[smart-teams] merge for %s unpublished %d stale guide(s) "
            "(purged=%d purge_failed=%d): %s",
            email,
            len(unpublished),
            len(purged),
            len(purge_failed),
            ", ".join(unpublished),
        )

    return GuideInvalidation(
        unpublished=tuple(unpublished),
        purged=tuple(purged),
        purge_failed=tuple(purge_failed),
        regeneration=_regeneration_state(db, guide, email, current_version),
    )


def _guide_module():
    """ST-6's module, or ``None``. Never raises.

    Lazy and by name (:data:`GUIDE_MODULE`) for the reason
    :func:`invalidate_stale_guides` gives: a merge that already wrote a profile
    version must not be reported as a failure because a follow-on module moved,
    and the durable half of the invalidation is something this module can do on
    its own.
    """
    try:
        import importlib

        return importlib.import_module(GUIDE_MODULE)
    except ImportError:
        logger.warning(
            "[smart-teams] %s is not importable; a stale guide will be "
            "unpublished but its corpus copy cannot be purged from here",
            GUIDE_MODULE,
        )
        return None


def _purge_guide_chunks(
    db: Session, guide, row: SmartTeamsDocument, *, email: str
) -> bool:
    """Remove one unpublished guide's chunks from the corpus. True only if gone.

    Delegates to ``guide.purge_guide_vectors``, which is the only caller that
    hands ``delete_document_vectors`` both the ``source_path`` and the
    ``is_company_wide`` flag the DAM corpus-routing asymmetry needs — a purge
    missing either one silently leaves the chunks live in the other corpus.

    A ``False`` return is not swallowed: ``rag_ingested`` stays true, which is
    the truth, and the row gets an :data:`ACTION_STALE_GUIDE_INGESTED` audit
    entry because "out of company view but still answerable in Chat" is a state
    somebody has to close by hand.
    """
    purge = getattr(guide, "purge_guide_vectors", None) if guide else None
    ok = False
    if purge is not None:
        try:
            ok = bool(purge(db, row))
        except Exception:  # noqa: BLE001 — the merge already succeeded
            logger.exception(
                "[smart-teams] purging the corpus copy of guide %s failed", row.id
            )
            ok = False
    if ok:
        return True
    logger.error(
        "[smart-teams] guide %s was unpublished by a profile merge but is still "
        "RAG-ingested; Chat can still quote it until its chunks are removed",
        row.id,
    )
    _record_audit(
        db,
        actor_email=email,
        action=ACTION_STALE_GUIDE_INGESTED,
        target=row.id,
        details={"kind": row.kind, "reason": "profile_merged"},
    )
    return False


def _regeneration_state(db: Session, guide, email: str, current_version: int) -> str:
    """Which of :data:`REGENERATION_STATES` this person's guide is in.

    A *measurement*, not a dispatch. ST-6 has no entry point that queues a
    regeneration, so claiming to have requested one would be the kind of
    confident sentence about nothing this feature exists to avoid. The staleness
    call is that module's own ``is_stale`` — the same predicate its own page
    uses, so the two cannot disagree.
    """
    newest = getattr(guide, "current_guide", None) if guide else None
    stale = getattr(guide, "is_stale", None) if guide else None
    if newest is None or stale is None:
        return "unavailable"
    try:
        row = newest(db, email)
        if row is None:
            return "no_guide"
        return "stale" if stale(row, current_version) else "current"
    except Exception:  # noqa: BLE001 — the merge already succeeded
        logger.exception(
            "[smart-teams] could not read %s's guide state after a merge", email
        )
        return "unavailable"


# ── Audit ─────────────────────────────────────────────────────────────────────


def _record_audit(
    db: Session,
    *,
    actor_email: str,
    action: str,
    target: str,
    status: str = "failure",
    details: dict | None = None,
) -> None:
    """Write the durable trace, swallowing its own failures.

    ``record_audit`` already logs at ERROR and rolls back if the insert fails, so
    the only thing left to guard is the import — and an audit row must never be
    the reason a merge or a parse reports failure.

    ``actor_email`` names the person whose upload this is, even on the rows a
    job wrote: it is the only field ``GET /api/admin/audit`` can filter on, and
    burying the one identifying detail in ``details`` makes the row unfindable
    for the question anybody actually asks.
    """
    try:
        from backend.audit import record_audit

        record_audit(
            db,
            actor_email=actor_email,
            action=action,
            status=status,
            target=target,
            details={"job": "smart-teams-uploads", **(details or {})},
        )
    except Exception:  # noqa: BLE001 — the trace must not become the failure
        logger.exception(
            "[smart-teams] could not record %s for %s — the log line above is "
            "the only record of it",
            action,
            target,
        )


# ── Serialization for the API ─────────────────────────────────────────────────


def upload_dict(row: SmartTeamsUpload) -> dict[str, Any]:
    """One upload, as the API renders it. No file bytes, no vendor text.

    ``parsed`` is safe to return: it is the coerced payload, which
    :func:`_assert_no_prose` has already proved carries nothing but numbers and
    closed-vocabulary labels. ``parse_error`` is our own sentence from
    :data:`PARSE_REASONS`.
    """
    return {
        "id": row.id,
        "original_filename": row.original_filename,
        "framework": row.framework,
        "parsed": row.parsed,
        "parse_error": row.parse_error,
        "review_status": row.review_status,
        "mergeable": bool(
            row.parsed and (row.framework or "").lower() in MERGE_ADAPTERS
        ),
        "merged_profile_id": row.merged_profile_id,
        "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
        "parsed_at": row.parsed_at.isoformat() if row.parsed_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }
