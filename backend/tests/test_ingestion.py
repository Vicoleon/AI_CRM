"""Document loader and chunker: deterministic ids, filename metadata, overlap.

The loader's keyword tagging is industry-neutral: Cubie ships to any company,
so it never tags a document with the old company's regulators or audit lines.
"""

import re

from rubik_core.ingestion.chunker import TokenAwareChunker
from rubik_core.ingestion.loader import DocumentLoader

#: The old company's industry (mortgage QC and compliance): its regulators,
#: agencies and audit lines, as whole words in any casing.
OLD_INDUSTRY = re.compile(
    r"(?i)\b(?:mortgages?|lending|loans?|servicing|(?:sub)?servicers?|MERS|HMDA"
    r"|CFPB|HUD|Fannie Mae|Freddie Mac|Ginnie Mae)\b"
)


def test_deterministic_doc_id(tmp_path):
    loader = DocumentLoader()
    f1 = tmp_path / "test.txt"
    f1.write_text("hello world")

    id1 = loader.generate_doc_id(f1)
    id2 = loader.generate_doc_id(f1)
    assert id1 == id2


def test_filename_parsing():
    loader = DocumentLoader()
    meta = loader._parse_filename("ClientA__Compliance__Texas__2023-01-01__Audit")
    assert meta["client_name"] == "ClientA"
    assert meta["service_line"] == "Compliance"
    assert meta["region"] == "Texas"
    assert meta["engagement_date"] == "2023-01-01"


def test_chunking_overlap():
    chunker = TokenAwareChunker(chunk_size=10, chunk_overlap=2)
    text = "This is a long string that should be split into multiple chunks."
    # Wrap in RawDocument
    from rubik_core.ingestion.loader import RawDocument

    doc = RawDocument(doc_id="test", text=text, metadata={})
    chunks = chunker.chunk_document(doc)

    assert len(chunks) > 1
    # Check for non-empty chunks
    for c in chunks:
        assert len(c.text) > 0


def test_heuristic_metadata_reads_the_date():
    loader = DocumentLoader()
    meta = loader._heuristic_metadata("Quarterly review, issued 2026-03-31.")
    assert meta["engagement_date"] == "2026-03-31"
    assert isinstance(meta["regulator_tags"], list)


def test_keyword_tagging_is_industry_neutral():
    loader = DocumentLoader()
    keywords = [*loader.regulator_keywords, *loader.service_line_keywords]
    assert [kw for kw in keywords if OLD_INDUSTRY.search(kw)] == []


def test_a_document_from_the_old_industry_gets_no_industry_tags():
    loader = DocumentLoader()
    text = "This report discusses CFPB mock audits and MERS requirements."
    meta = loader._heuristic_metadata(text)
    assert [tag for tag in meta["regulator_tags"] if OLD_INDUSTRY.search(tag)] == []
    assert not OLD_INDUSTRY.search(meta.get("service_line") or "")
