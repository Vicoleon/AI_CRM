"""RAG ingestion for the EOS subdomain.

The five EOS sources (VTO, Rocks, To-Dos+Issues, L10 session notes,
Processes) each get extracted from their `eos_*` tables into a list
of `EosDoc` objects, then uploaded to GCS and imported into Cubie's
existing Vertex AI RAG corpus. There is no separate corpus — EOS
chunks live alongside transcripts and knowledge base docs and surface
naturally through Cubie's standard retrieval path.

Triggers:
- **Event-driven** (in the routers that mutate the data):
   * VTO section save → `ingest_vto(version_id, db)`
   * L10 session end → `ingest_l10_session(session_id, db)`
   * Issue → status=solved → `ingest_issue(issue_id, db)`
   * Process create → `ingest_process(process_id, db)`
- **Weekly cron** (`POST /api/admin/ingest/eos`, called by Cloud Scheduler):
   re-ingests all five sources as a backstop for any events the system
   missed.

Per-chunk metadata is encoded in the file content's header (Vertex's
`rag.import_files` does not accept arbitrary metadata fields), so the
LLM sees "EOS · Rocks · …" at the top of every retrieved chunk and can
cite accordingly.
"""

from rubik_core.ingestion.eos.extractor import (  # noqa: F401
    EosDoc,
    extract_all,
    extract_issues,
    extract_l10,
    extract_processes,
    extract_rocks,
    extract_vto,
)
from rubik_core.ingestion.eos.pipeline import (  # noqa: F401
    IngestResult,
    ingest_all_sources,
    ingest_docs,
)
