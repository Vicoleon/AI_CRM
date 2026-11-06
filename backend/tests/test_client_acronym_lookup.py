"""Regression: "Give me the acronyms for Granite Residential from the Client
Acronym List" → "There is no acronym for Granite Residential in the Client
Acronym List."

The acronym was there. The acronym PDF in the Library (407 rows, 27,662
chars) holds "Granite Residential Services Corp → GRSC" at char 21,997.
Three cuts stacked up:

1. Named-document resolution measured coverage of the QUERY's tokens against
   the filename. Asking about a row inside a named file ("granite",
   "residential") drops that ratio to 0.40, so the file never resolved and the
   question fell through to vector search.
2. Vector hits were sliced to the first 600 chars of a ~2,000-char chunk, so
   even the chunk holding the row could arrive without it.
3. The named-document reader capped every file at 8,000 chars — silently. Both
   clients the team tested (Granite Residential at 21,997, Peoples Logistics at
   13,120) sit past the cut, which is exactly why those two "failed" while
   clients near the top of the export answered fine.

Nothing told the model it was reading a fragment, so it read absence-from-the-
fragment as absence-from-the-document — then corroborated it with the Sep 4
"Sales Team Trainings" meeting notes, which had merely recorded that the
acronym lookup was broken. Fixes: match on the document's own name, read a
named document whole, and announce every clip in the text the model reads.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rubik_core.adk_agent.tools import (
    AgentTools,
    _clip_with_truncation_notice,
    _filename_identity_tokens,
    _read_asset_document_text,
    _resolve_named_documents,
)
from rubik_core.config import settings

_ACRONYMS_FILENAME = "Client_Acronyms_2026-09-02.pdf"
_EMERY_QUERY = (
    "Give me the acronyms for Granite Residential from the Client Acronym List"
)


def _fake_user(email="ebrooks@acme.example"):
    u = MagicMock()
    u.email = email
    u.is_admin = False
    u.department = "Executive"
    return u


def _acronyms_asset():
    asset = MagicMock()
    asset.doc_id = "acr1"
    asset.status = "approved"
    asset.source_path = f"gs://cubie-data-acme/cubie/{_ACRONYMS_FILENAME}"
    asset.original_filename = _ACRONYMS_FILENAME
    return asset


def _resolve(query, filename=_ACRONYMS_FILENAME, *, probes=None):
    """Resolve against a Library holding exactly one document.

    ``list_assets(q=…)`` is modelled the way the repository really behaves — a
    case-insensitive substring match on the filename — so a probe word that
    isn't in the filename returns nothing, as it would in production. Passing
    ``probes`` records the words the resolver actually tried.
    """
    asset = _acronyms_asset()
    asset.original_filename = filename

    def fake_list_assets(q=None, limit=None, **kw):
        if probes is not None:
            probes.append(q)
        if q and q.lower() in filename.lower():
            return {
                "assets": {"acr1": {"original_filename": filename, "topic": "Other"}},
                "total": 1,
            }
        return {"assets": {}, "total": 0}

    with (
        patch("rubik_core.auth.registry.UserRegistry") as MockUR,
        patch("rubik_core.dam.registry.DAMRegistry") as MockDAM,
        patch("rubik_core.auth.access.has_document_access", return_value=True),
    ):
        MockUR.return_value.get_user.return_value = _fake_user()
        MockDAM.return_value.list_assets.side_effect = fake_list_assets
        MockDAM.return_value.get_asset.return_value = asset
        return _resolve_named_documents(query, "ebrooks@acme.example")


# ── the filename's identity ─────────────────────────────────────────────────


def test_identity_tokens_ignore_export_date_and_extension():
    """A date-stamped export is still called "the client acronym list"."""
    assert _filename_identity_tokens(_ACRONYMS_FILENAME) == {"client", "acronym"}


def test_identity_tokens_ignore_version_noise():
    assert _filename_identity_tokens("Vendor Roster v2 FINAL (Updated).xlsx") == {
        "vendor",
        "roster",
    }


# ── resolution: the query names the document, then asks about a row ─────────


def test_the_regression_query_resolves_the_acronym_list():
    assert [a.doc_id for a in _resolve(_EMERY_QUERY)] == ["acr1"]


def test_resolution_does_not_hinge_on_the_longest_query_word():
    """The longest word here is "residential", which is in no filename. The
    document must still be found — narrowing on that one word alone is what
    kept the acronym list out of the candidate set entirely."""
    probes: list = []
    result = _resolve(_EMERY_QUERY, probes=probes)
    assert [a.doc_id for a in result] == ["acr1"]
    assert probes[0] == "residential"  # still tried first (longest)
    assert "acronym" in probes  # …but not the only thing tried


def test_probing_stops_once_a_document_matches():
    """Each probe is a query: don't keep asking after a confident match."""
    probes: list = []
    _resolve("the client acronym list", probes=probes)
    assert probes == ["acronym"]  # matched on the first probe, stopped there


def test_singular_phrasing_still_resolves_the_plural_filename():
    """ "the client acronym list" must reach "Client_Acronyms…" — the whole bug
    class is a lookup question missing its file by one letter."""
    q = "what's the acronym for Peoples Logistics in the client acronym list"
    assert [a.doc_id for a in _resolve(q)] == ["acr1"]


def test_one_word_filename_is_not_pulled_in_by_a_passing_mention():
    """Two identity tokens minimum: "acronyms" alone can't claim a question."""
    assert _resolve("what is the acronym for Granite Residential", "Acronyms.pdf") == []


def test_unrelated_question_does_not_resolve_the_acronym_list():
    assert _resolve("what is our remote work policy for new hires") == []


# ── reading a named document: whole, and honest when it isn't ───────────────


def _long_table(tmp_path, marker_at=21_997, total=27_662):
    """A file shaped like the acronym export: the answer sits deep inside it."""
    row = "Some Trading Company Inc STC Mar 15, 2024 Kuh Cunco\n"
    body = (row * (total // len(row) + 2))[:marker_at]
    doc = tmp_path / "Client_Acronyms_2026-09-02.txt"
    doc.write_text(body + "Granite Residential Services Corp GRSC\n" + row * 100)
    return doc


def test_named_document_is_read_past_the_old_8000_char_cap(tmp_path, monkeypatch):
    from rubik_core.tenancy import storage

    monkeypatch.setattr(storage, "local_data_dir", lambda: tmp_path)
    doc = _long_table(tmp_path)
    text = _read_asset_document_text(doc.as_uri(), doc.name)
    assert "Granite Residential Services Corp GRSC" in text
    assert "[TRUNCATED" not in text  # 27k chars is well inside the budget


def test_a_clip_that_does_happen_is_announced(tmp_path, monkeypatch):
    from rubik_core.tenancy import storage

    monkeypatch.setattr(storage, "local_data_dir", lambda: tmp_path)
    doc = _long_table(tmp_path)
    text = _read_asset_document_text(doc.as_uri(), doc.name, max_chars=8000)
    assert "Granite Residential Services Corp GRSC" not in text  # the old behaviour…
    assert "[TRUNCATED" in text  # …but no longer a silent one
    assert "missing" in text  # names the wrong conclusion explicitly
    assert doc.name in text


def test_clip_notice_reports_the_real_lengths():
    notice = _clip_with_truncation_notice("x" * 100, 10, label="f.pdf")
    assert notice.startswith("x" * 10)
    assert "first 10 of 100 characters" in notice


def test_text_under_the_limit_is_returned_untouched():
    assert _clip_with_truncation_notice("short", 1000, label="f.pdf") == "short"


# ── vector hits keep the row the question was about ─────────────────────────


def _search_kb(query, hits, monkeypatch):
    from rubik_core.config import AppMode

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    tools = AgentTools(user_email="ebrooks@acme.example")
    with (
        patch("rubik_core.adk_agent.tools._resolve_named_documents", return_value=[]),
        patch("rubik_core.rag.rags_for_user_email", return_value=["companywide"]),
        patch("rubik_core.rag.RagManager") as MgrCls,
    ):
        MgrCls.return_value.query.return_value = hits
        return asyncio.run(tools.search_knowledge_base(query))


def test_a_retrieved_chunk_is_not_sliced_to_600_chars(monkeypatch):
    """A 512-token chunk (~2,000 chars) of a lookup table must arrive whole:
    the row is as likely to be at the end of the chunk as the start."""
    chunk = (
        "Task ID Task Name Acronym\n"
        + "Filler Trading Co FTC Mar 15, 2024\n" * 40
        + "Granite Residential Services Corp GRSC Mar 15, 2024\n"
    )
    assert len(chunk) > 600  # the slice the old code took
    hit = SimpleNamespace(
        text=chunk,
        rag="companywide",
        source_uri=f"gs://b/{_ACRONYMS_FILENAME}",
        score=0.2,
    )
    result = _search_kb("acronym for Granite Residential", [hit], monkeypatch)

    assert result["status"] == "success"
    assert "GRSC" in result["report"]
    assert f"**Source (companywide):** {_ACRONYMS_FILENAME}" in result["report"]


def test_an_oversized_chunk_is_clipped_with_a_notice_and_still_cites(monkeypatch):
    """Even when a hit is too big to show whole, the Sources panel must still
    resolve it — the citation parser reads the **Source:** line."""
    monkeypatch.setattr(settings, "RAG_HIT_EXCERPT_CHARS", 500)
    hit = SimpleNamespace(
        text="row\n" * 400,
        rag="companywide",
        source_uri="gs://b/handbook.md",
        score=0.2,
    )
    result = _search_kb("handbook", [hit], monkeypatch)
    assert "[TRUNCATED" in result["report"]

    from rubik_core.orchestration.adk_orchestrator import ADKOrchestrator

    citations: list = []
    ADKOrchestrator._extract_citations_from_report(
        ADKOrchestrator, result["report"], citations
    )
    assert [c["source_path"] for c in citations] == ["handbook.md"]


# ── the low-confidence note must not read as "the entry isn't there" ────────


def test_low_confidence_note_separates_retrieval_miss_from_absence(monkeypatch):
    result = _search_kb("something we have no document for", [], monkeypatch)
    assert result["status"] == "low_confidence"
    report = result["report"].lower()
    assert "don't have" in report  # unchanged contract with the model
    assert "retrieval miss" in report
    assert "never tell them the entry they asked about is absent" in report
