import asyncio
from unittest.mock import MagicMock, patch

from rubik_core.tools.drive import GoogleDriveClient

# transcript_sync refuses to ingest a doc under MIN_TRANSCRIPT_CHARS — that is
# how it skips a "Notes by Gemini" shell Google has not finished writing.
# These fixtures exercise ACL / attendance, not length, so they
# carry a realistic body rather than a 22-character stub.
_TRANSCRIPT_FILLER = "\n" + "\n".join(
    f"Speaker {i}: We walked through the numbers and agreed on next steps."
    for i in range(1, 12)
)


def test_extract_participants_from_transcript():
    client = GoogleDriveClient(user_email="host@acme.example")

    # Mock service creation to avoid API calls
    client.service = MagicMock()

    dummy_transcript = """
    Meeting: Project Kickoff
    Date: 2026-03-09

    Attendees: host@acme.example, Alice (alice@acme.example), bob.smith@external.com

    Transcript:
    host@acme.example: Welcome everyone.
    alice@acme.example: Glad to be here. Do we need to include charlie@acme.example?
    bob.smith@external.com: Not for this phase.
    """

    participants = client.extract_participants_from_transcript(dummy_transcript)

    expected = {
        "host@acme.example",
        "alice@acme.example",
        "bob.smith@external.com",
        "charlie@acme.example",
    }
    assert set(participants) == set(expected)


def test_search_past_meetings_short_circuits_for_non_participants():
    """A user with zero rows in meeting_participants must get nothing back."""
    from rubik_core.adk_agent.tools import AgentTools

    tools = AgentTools(user_email="outsider@acme.example")

    with (
        patch(
            "rubik_core.database.repository.SQLMeetingParticipantsRepository.list_doc_ids_for_user",
            return_value=[],
        ),
        patch("rubik_core.adapters.chroma.ChromaAdapter") as MockAdapter,
    ):
        result = asyncio.run(tools.search_past_meetings("anything"))
        # No vector store call should ever happen — we short-circuit on the SQL ACL.
        MockAdapter.return_value.query.assert_not_called()

    assert result["status"] == "success"
    assert "no past meetings" in result["report"].lower()


def test_search_past_meetings_drops_chunks_outside_participant_set():
    """Even if vectors leak a foreign doc, the SQL ACL re-check must drop it."""
    from rubik_core.adk_agent.tools import AgentTools

    tools = AgentTools(user_email="alice@acme.example")

    leaked_chunk = MagicMock()
    leaked_chunk.chunk.metadata = {
        "source_path": "gs://b/x/forbidden_doc.txt",
        "doc_id": "forbidden_doc",
    }
    leaked_chunk.chunk.text = "secret discussion content"

    allowed_chunk = MagicMock()
    allowed_chunk.chunk.metadata = {
        "source_path": "gs://b/x/ok_doc.txt",
        "doc_id": "ok_doc",
    }
    allowed_chunk.chunk.text = "fine content"

    with (
        patch(
            "rubik_core.database.repository.SQLMeetingParticipantsRepository.list_doc_ids_for_user",
            return_value=["ok_doc"],
        ),
        patch("rubik_core.config.settings") as mock_settings,
        patch("rubik_core.adapters.chroma.ChromaAdapter") as MockAdapter,
        patch("rubik_core.models.get_embeddings") as mock_emb,
        patch("rubik_core.dam.registry.DAMRegistry.get_asset") as mock_get_asset,
    ):
        mock_settings.APP_MODE = "LOCAL"
        from rubik_core.config import AppMode

        mock_settings.APP_MODE = AppMode.LOCAL

        mock_emb.return_value.embed_query.return_value = [0.0]
        MockAdapter.return_value.query.return_value = [leaked_chunk, allowed_chunk]

        ok_asset = MagicMock()
        ok_asset.original_filename = "Budget Sync.txt"
        ok_asset.status = "approved"
        mock_get_asset.side_effect = lambda doc_id: (
            ok_asset if doc_id == "ok_doc" else None
        )

        result = asyncio.run(tools.search_past_meetings("budget"))

    assert "secret discussion" not in result["report"]
    assert "fine content" in result["report"]


def test_recency_query_detection():
    """The recency detector fires on 'last meeting' phrasing, not plain topics."""
    from rubik_core.adk_agent.tools import _is_recency_query

    assert _is_recency_query("Summarize the key takeaways from my last meeting")
    assert _is_recency_query("what happened in my most recent call?")
    assert _is_recency_query("recap my latest meeting")
    assert _is_recency_query("notes from the previous meeting")
    # Plain topical questions must NOT route to the recency path.
    assert not _is_recency_query("what did we decide about the Acme renewal?")
    assert not _is_recency_query("budget meeting notes")
    assert not _is_recency_query("")


def test_search_past_meetings_recency_query_returns_latest_by_date():
    """'My last meeting' resolves by date and never touches the vector store."""
    from datetime import datetime

    from rubik_core.adk_agent.tools import AgentTools

    tools = AgentTools(user_email="alice@acme.example")

    latest = MagicMock()
    latest.original_filename = "Acme Renewal Sync.txt"
    latest.status = "approved"
    latest.source_path = "gs://b/x/recent_doc.txt"
    latest.created_at = datetime(2026, 6, 24, 15, 0, 0)

    with (
        patch(
            "rubik_core.database.repository.SQLMeetingParticipantsRepository.list_doc_ids_for_user",
            return_value=["recent_doc"],
        ),
        patch(
            "rubik_core.database.repository.SQLMeetingParticipantsRepository.list_recent_doc_ids_for_user",
            return_value=["recent_doc"],
        ) as mock_recent,
        patch("rubik_core.dam.registry.DAMRegistry.get_asset", return_value=latest),
        patch(
            "rubik_core.adk_agent.tools._read_transcript_text",
            return_value="We agreed to ship Friday and follow up with Acme.",
        ),
        patch("rubik_core.adapters.chroma.ChromaAdapter") as MockAdapter,
    ):
        result = asyncio.run(
            tools.search_past_meetings(
                "Summarize the key takeaways from my last meeting"
            )
        )
        # Singular phrasing → just the latest one.
        mock_recent.assert_called_once_with("alice@acme.example", limit=1)
        # Recency path must NOT touch the vector store.
        MockAdapter.return_value.query.assert_not_called()

    assert result["status"] == "success"
    assert "Acme Renewal Sync" in result["report"]
    assert "ship Friday" in result["report"]
    assert "most recent meeting" in result["report"].lower()


def test_search_past_meetings_recency_plural_returns_several():
    """Plural recency phrasing ('recent meetings') asks for more than one."""
    from datetime import datetime

    from rubik_core.adk_agent.tools import AgentTools

    tools = AgentTools(user_email="alice@acme.example")

    asset = MagicMock()
    asset.original_filename = "Standup.txt"
    asset.status = "approved"
    asset.source_path = "gs://b/x/d.txt"
    asset.created_at = datetime(2026, 6, 24, 15, 0, 0)

    with (
        patch(
            "rubik_core.database.repository.SQLMeetingParticipantsRepository.list_doc_ids_for_user",
            return_value=["d"],
        ),
        patch(
            "rubik_core.database.repository.SQLMeetingParticipantsRepository.list_recent_doc_ids_for_user",
            return_value=["d"],
        ) as mock_recent,
        patch("rubik_core.dam.registry.DAMRegistry.get_asset", return_value=asset),
        patch(
            "rubik_core.adk_agent.tools._read_transcript_text",
            return_value="content",
        ),
    ):
        asyncio.run(
            tools.search_past_meetings("summarize my recent meetings", max_results=4)
        )
        mock_recent.assert_called_once_with("alice@acme.example", limit=4)


def test_list_recent_doc_ids_orders_by_created_at(tmp_path, monkeypatch):
    """Newest-first ordering, deleted/failed excluded, limit + empty-email honored."""
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Asset, Base
    from rubik_core.database.repository import SQLMeetingParticipantsRepository

    engine = create_engine(f"sqlite:///{tmp_path}/recent.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)

    base = datetime(2026, 6, 1, 12, 0, 0)
    with TestSession() as s:
        for i, doc in enumerate(["old", "mid", "new"]):
            s.add(
                Asset(
                    doc_id=doc,
                    owner_email="host@acme.example",
                    status="approved",
                    topic="Meeting Transcripts",
                    created_at=base + timedelta(days=i),
                    updated_at=base + timedelta(days=i),
                )
            )
        # A deleted transcript must never surface even though it's the newest.
        s.add(
            Asset(
                doc_id="dead",
                owner_email="host@acme.example",
                status="deleted",
                topic="Meeting Transcripts",
                created_at=base + timedelta(days=10),
                updated_at=base + timedelta(days=10),
            )
        )
        s.commit()

    repo = SQLMeetingParticipantsRepository()
    for doc in ["old", "mid", "new", "dead"]:
        repo.upsert_participants(
            doc, meeting_id=f"ext-{doc}", emails=["alice@acme.example"]
        )

    assert repo.list_recent_doc_ids_for_user("alice@acme.example", limit=2) == [
        "new",
        "mid",
    ]
    assert repo.list_recent_doc_ids_for_user("alice@acme.example") == [
        "new",
        "mid",
        "old",
    ]
    assert repo.list_recent_doc_ids_for_user("") == []


def test_meeting_participants_repo_round_trip(tmp_path, monkeypatch):
    """The repo writes/reads junction rows and treats missing inputs as deny."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Asset, Base
    from rubik_core.database.repository import SQLMeetingParticipantsRepository

    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    # Repos imported SessionLocal at module-load time, so patch the bound name there too.
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)

    # Seed an asset (FK target for meeting_participants.doc_id).
    from datetime import datetime

    with TestSession() as s:
        s.add(
            Asset(
                doc_id="doc-1",
                owner_email="host@acme.example",
                status="approved",
                topic="Meeting Transcripts",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        s.commit()

    repo = SQLMeetingParticipantsRepository()
    repo.upsert_participants(
        "doc-1",
        meeting_id="ext-123",
        emails=["Alice@acme.example", "bob@acme.example", "alice@acme.example"],
    )

    assert repo.has_access("doc-1", "alice@acme.example") is True
    assert repo.has_access("doc-1", "BOB@acme.example") is True  # case-insensitive
    assert repo.has_access("doc-1", "stranger@acme.example") is False
    assert repo.has_access("doc-1", "") is False
    assert repo.has_access("", "alice@acme.example") is False

    assert sorted(repo.list_doc_ids_for_user("alice@acme.example")) == ["doc-1"]

    # Re-upsert deduplicates rather than appending.
    repo.upsert_participants(
        "doc-1", meeting_id="ext-123", emails=["alice@acme.example", "bob@acme.example"]
    )
    assert sorted(repo.list_participants_for_doc("doc-1")) == [
        "alice@acme.example",
        "bob@acme.example",
    ]

    repo.delete_for_doc("doc-1")
    assert repo.has_access("doc-1", "alice@acme.example") is False


def _wire_sqlite(tmp_path, monkeypatch, *extra_modules):
    """Create a schema'd throwaway sqlite and rebind SessionLocal everywhere a
    repo/script captured it at import time. Returns (TestSession, doc_id helper)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/acl.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)
    for mod in extra_modules:
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", TestSession)
    return TestSession


def test_meet_sync_populates_participant_acl(tmp_path, monkeypatch):
    """Regression: the live Meet-sync path must write meeting_participants, not
    just Asset.allowed_emails — otherwise chat Q&A short-circuits to "no past
    meetings" even for the host."""
    import hashlib

    from rubik_core.config import AppMode
    from rubik_core.database.repository import SQLMeetingParticipantsRepository
    from rubik_core.scripts import sync_meet_transcripts as sync_mod

    _wire_sqlite(tmp_path, monkeypatch, sync_mod)
    monkeypatch.setattr(sync_mod.settings, "APP_MODE", AppMode.LOCAL)

    fake_processor = MagicMock()
    fake_processor.process_file.return_value = {"result": MagicMock(success=True)}
    monkeypatch.setattr(sync_mod, "get_processor", lambda: fake_processor)

    drive = MagicMock()
    drive.service = MagicMock()
    drive.get_meet_recordings_folder_id.return_value = "folder-1"
    drive.list_transcripts.return_value = [
        {
            "id": "file-1",
            "name": "Acme Sync - Notes by Gemini",
            "mimeType": "text/plain",
        }
    ]
    drive.search_transcripts_by_pattern.return_value = []
    drive.export_document_text.return_value = (
        "host@acme.example: hi\nalice@acme.example: hello" + _TRANSCRIPT_FILLER
    )
    drive.extract_participants_from_transcript.return_value = ["alice@acme.example"]
    monkeypatch.setattr(sync_mod, "GoogleDriveClient", lambda user_email: drive)

    sync_mod.run_sync(user_emails=["host@acme.example"], days_ago=7)

    doc_id = hashlib.sha256(b"file-1").hexdigest()
    repo = SQLMeetingParticipantsRepository()
    assert repo.has_access(doc_id, "host@acme.example") is True
    assert repo.has_access(doc_id, "alice@acme.example") is True


def test_fathom_ingest_populates_participant_acl(tmp_path, monkeypatch):
    """Regression: the Fathom (sales-locked) ingest path must write
    meeting_participants so its transcripts are queryable in chat — but the ACL
    is scoped to the meeting's OWN participants, NOT the batch-triggering rep
    (list_meetings is account-wide, so blanket-adding the trigger would leak one
    rep's access into other reps' calls)."""
    import hashlib

    import backend.routers.fathom as fathom_mod
    from rubik_core.config import AppMode
    from rubik_core.database.repository import SQLMeetingParticipantsRepository

    _wire_sqlite(tmp_path, monkeypatch, fathom_mod)
    monkeypatch.setattr(fathom_mod.settings, "APP_MODE", AppMode.LOCAL)

    fc = MagicMock()
    fc.api_key = "x"
    fc.list_meetings.return_value = [{"id": "m1", "title": "Acme Call"}]
    # Router ingests transcript + Fathom action items/summary via build_ingest_text.
    fc.build_ingest_text.return_value = "transcript body"
    # The meeting's real attendees/host — does NOT include the triggering rep.
    fc.extract_participants_from_meeting.return_value = [
        "alice@acme.example",
        "bob@acme.example",
    ]
    monkeypatch.setattr(fathom_mod, "FathomClient", lambda *a, **k: fc)

    fake_processor = MagicMock()
    fake_processor.process_file.return_value = {"result": MagicMock(success=True)}

    # A rep who only TRIGGERED the batch pull but did not attend this meeting.
    fathom_mod.process_fathom_transcripts_background(
        "trigger@acme.example", fake_processor
    )

    doc_id = hashlib.sha256(b"fathom_m1").hexdigest()
    repo = SQLMeetingParticipantsRepository()
    assert repo.has_access(doc_id, "alice@acme.example") is True
    assert repo.has_access(doc_id, "bob@acme.example") is True
    # The triggering rep is NOT a participant → must NOT gain query access.
    assert repo.has_access(doc_id, "trigger@acme.example") is False


def test_title_hint_from_filename_strips_boilerplate():
    """The calendar-match title hint drops the Gemini suffix and trailing date."""
    from rubik_core.scripts.transcript_sync import _title_hint_from_filename

    assert (
        _title_hint_from_filename(
            "Sales / HubSpot Weekly Open Office - 2026/06/26 - Notes by Gemini"
        )
        == "Sales / HubSpot Weekly Open Office"
    )
    assert _title_hint_from_filename("Acme Sync - Notes by Gemini.txt") == "Acme Sync"
    assert _title_hint_from_filename("") == ""


def test_find_meeting_attendee_emails_matches_by_title():
    """The calendar lookup returns the invite's attendees + organizer, by title,
    and excludes room/equipment resources."""
    from datetime import datetime, timezone

    from rubik_core.tools.calendar import GoogleCalendarClient

    cal = GoogleCalendarClient(user_email="host@acme.example")
    cal.service = MagicMock()
    event = {
        "summary": "Sales / HubSpot Weekly Open Office - weekly",
        "organizer": {"email": "Host@acme.example"},
        "attendees": [
            {"email": "mingram@acme.example"},  # attended, never spoke
            {"email": "host@acme.example"},
            {"email": "room@resource.calendar.google.com", "resource": True},
        ],
    }
    cal.service.events.return_value.list.return_value.execute.return_value = {
        "items": [event]
    }

    emails = cal.find_meeting_attendee_emails(
        "Sales / HubSpot Weekly Open Office",
        around_time=datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc),
    )
    assert "mingram@acme.example" in emails
    assert "host@acme.example" in emails
    # Room resource is not a person — must not be granted access.
    assert "room@resource.calendar.google.com" not in emails


def test_find_meeting_attendee_emails_no_title_match_returns_empty():
    """Fail-closed: an unrelated event must not widen access."""
    from datetime import datetime, timezone

    from rubik_core.tools.calendar import GoogleCalendarClient

    cal = GoogleCalendarClient(user_email="host@acme.example")
    cal.service = MagicMock()
    cal.service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "summary": "Completely Unrelated 1:1",
                "attendees": [{"email": "someone@acme.example"}],
            }
        ]
    }
    emails = cal.find_meeting_attendee_emails(
        "Sales / HubSpot Weekly Open Office",
        around_time=datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc),
    )
    assert emails == []


def test_meet_sync_enriches_acl_from_calendar(tmp_path, monkeypatch):
    """Fallback path: when Meet attendance is unavailable, the ACL falls back to
    the calendar invite, so a silent attendee (mingram) still gets access rather
    than being dropped. (When Meet attendance IS available, attendance is
    authoritative — see test_meet_sync_gates_on_actual_attendance.)"""
    import hashlib

    from rubik_core.config import AppMode
    from rubik_core.database.repository import SQLMeetingParticipantsRepository
    from rubik_core.scripts import transcript_sync as ts

    _wire_sqlite(tmp_path, monkeypatch, ts)
    monkeypatch.setattr(ts.settings, "APP_MODE", AppMode.LOCAL)

    fake_processor = MagicMock()
    fake_processor.process_file.return_value = {"result": MagicMock(success=True)}
    monkeypatch.setattr(ts, "_build_processor", lambda: fake_processor)

    drive = MagicMock()
    drive.service = MagicMock()
    drive.get_meet_recordings_folder_id.return_value = "folder-1"
    drive.list_transcripts.return_value = [
        {
            "id": "file-9",
            "name": "Sales / HubSpot Weekly Open Office - Notes by Gemini",
            "mimeType": "text/plain",
            "createdTime": "2026-06-26T18:05:00Z",
        }
    ]
    drive.search_transcripts_by_pattern.return_value = []
    # mingram never speaks → that email is NOT in the text and NOT name-matched.
    drive.export_document_text.return_value = (
        "host@acme.example: kicking off" + _TRANSCRIPT_FILLER
    )
    drive.extract_participants_from_transcript.return_value = ["host@acme.example"]
    monkeypatch.setattr(ts, "GoogleDriveClient", lambda user_email: drive)

    # Calendar invite is the authoritative attendee source — includes mingram.
    cal = MagicMock()
    cal.service = MagicMock()
    cal.find_meeting_attendee_emails.return_value = [
        "host@acme.example",
        "mingram@acme.example",
    ]
    monkeypatch.setattr(ts, "GoogleCalendarClient", lambda user_email: cal)
    # No Meet client in this scenario → falls back to the calendar invite list.
    monkeypatch.setattr(ts, "_meet_client_for", lambda user_email: None)

    ts.run_sync(user_emails=["host@acme.example"], days_ago=7)

    doc_id = hashlib.sha256(b"file-9").hexdigest()
    repo = SQLMeetingParticipantsRepository()
    assert repo.has_access(doc_id, "host@acme.example") is True
    # The fix: the silent attendee can now see/query the meeting.
    assert repo.has_access(doc_id, "mingram@acme.example") is True


def test_meet_client_lists_signed_in_participants():
    """Filters conferenceRecords by meeting code, picks the occurrence nearest the
    transcript time (recurring meetings reuse a space), and returns only signed-in
    participants (user_id + name) — anonymous/phone attendees are dropped."""
    from datetime import datetime, timezone

    from rubik_core.tools.meet import GoogleMeetClient

    m = GoogleMeetClient.__new__(GoogleMeetClient)  # skip DWD build
    m.user_email = "host@acme.example"
    m.service = MagicMock()

    # Two occurrences of the same recurring space; transcript ≈ the 2nd.
    m.service.conferenceRecords.return_value.list.return_value.execute.return_value = {
        "conferenceRecords": [
            {"name": "conferenceRecords/old", "endTime": "2026-07-01T18:00:00Z"},
            {"name": "conferenceRecords/new", "endTime": "2026-07-08T18:00:00Z"},
        ]
    }
    parts = m.service.conferenceRecords.return_value.participants.return_value.list
    parts.return_value.execute.return_value = {
        "participants": [
            {"signedinUser": {"user": "users/HOST", "displayName": "Host User"}},
            {"signedinUser": {"user": "users/PQ", "displayName": "Marlo Ingram"}},
            {"anonymousUser": {"displayName": "Guest"}},  # no id → dropped
        ]
    }

    people = m.list_signed_in_participants(
        "abc-defg-hij", around_time=datetime(2026, 7, 8, 18, 5, tzinfo=timezone.utc)
    )
    assert {p["user_id"] for p in people} == {"users/HOST", "users/PQ"}
    # Picked the occurrence nearest the transcript time.
    assert parts.call_args.kwargs["parent"] == "conferenceRecords/new"


def test_meet_client_ignores_records_outside_correlation_window():
    """If the only records for a code are far from the transcript time, don't
    guess — return [] so the caller falls back to the invite list."""
    from datetime import datetime, timezone

    from rubik_core.tools.meet import GoogleMeetClient

    m = GoogleMeetClient.__new__(GoogleMeetClient)
    m.user_email = "host@acme.example"
    m.service = MagicMock()
    m.service.conferenceRecords.return_value.list.return_value.execute.return_value = {
        "conferenceRecords": [
            {"name": "conferenceRecords/x", "endTime": "2026-07-01T18:00:00Z"}
        ]
    }
    people = m.list_signed_in_participants(
        "abc-defg-hij", around_time=datetime(2026, 7, 8, 18, 5, tzinfo=timezone.utc)
    )
    assert people == []


def test_resolve_joiner_emails_by_id_then_name():
    """Meet participants resolve to app-user emails by Google id first, then by
    display name; external/unknown participants don't resolve."""
    from rubik_core.scripts import transcript_sync as ts

    class U:
        def __init__(self, id, email, name):
            self.id, self.email, self.name = id, email, name

    users = [
        U("HOST", "host@acme.example", "Host User"),
        U("PQ", "mingram@acme.example", "Marlo Ingram"),
        U("CD", "ifoster@acme.example", "Indigo Foster"),
    ]
    joiners = [
        {"user_id": "users/HOST", "display_name": "Host User"},  # id match
        {"user_id": "users/UNKNOWN", "display_name": "Marlo Ingram"},  # name match
        {"user_id": "users/EXT", "display_name": "External Client"},  # unresolved
    ]
    assert ts._resolve_joiner_emails(joiners, users) == {
        "host@acme.example",
        "mingram@acme.example",
    }


def test_meet_sync_gates_on_actual_attendance(tmp_path, monkeypatch):
    """Forward gating: the ACL is who ACTUALLY JOINED (Meet), not the calendar
    invite. A joiner (mingram) gets access; an invited-but-absent person (ifoster,
    not in the Meet participant list) does NOT."""
    import hashlib

    from rubik_core.config import AppMode
    from rubik_core.database.repository import (
        SQLMeetingParticipantsRepository,
        SQLUserRegistry,
    )
    from rubik_core.scripts import transcript_sync as ts

    _wire_sqlite(tmp_path, monkeypatch, ts)
    monkeypatch.setattr(ts.settings, "APP_MODE", AppMode.LOCAL)

    reg = SQLUserRegistry()
    reg.register_user(email="host@acme.example", name="Host User", id="HOST")
    reg.register_user(email="mingram@acme.example", name="Marlo Ingram", id="PQ")
    reg.register_user(email="ifoster@acme.example", name="Indigo Foster", id="CD")

    fake_processor = MagicMock()
    fake_processor.process_file.return_value = {"result": MagicMock(success=True)}
    monkeypatch.setattr(ts, "_build_processor", lambda: fake_processor)

    drive = MagicMock()
    drive.service = MagicMock()
    drive.get_meet_recordings_folder_id.return_value = "folder-1"
    drive.list_transcripts.return_value = [
        {
            "id": "file-9",
            "name": "Team L10 - Notes by Gemini",
            "mimeType": "text/plain",
            "createdTime": "2026-07-08T18:05:00Z",
        }
    ]
    drive.search_transcripts_by_pattern.return_value = []
    drive.export_document_text.return_value = (
        "Host User: kicking off" + _TRANSCRIPT_FILLER
    )
    drive.extract_participants_from_transcript.return_value = []
    monkeypatch.setattr(ts, "GoogleDriveClient", lambda user_email: drive)

    # Calendar: the matched event carries a Meet code. (Patch the factory, not the
    # class, so the real extract_meeting_code still runs.)
    cal = MagicMock()
    cal.find_meeting_event.return_value = {
        "hangoutLink": "https://meet.google.com/abc-defg-hij"
    }
    monkeypatch.setattr(ts, "_calendar_client_for", lambda user_email: cal)

    # Meet: actual joiners = host + mingram (ifoster was invited but never joined).
    meet = MagicMock()
    meet.list_signed_in_participants.return_value = [
        {"user_id": "users/HOST", "display_name": "Host User"},
        {"user_id": "users/PQ", "display_name": "Marlo Ingram"},
    ]
    monkeypatch.setattr(ts, "_meet_client_for", lambda user_email: meet)

    ts.run_sync(user_emails=["host@acme.example"], days_ago=7)

    doc_id = hashlib.sha256(b"file-9").hexdigest()
    repo = SQLMeetingParticipantsRepository()
    assert repo.has_access(doc_id, "host@acme.example") is True
    assert repo.has_access(doc_id, "mingram@acme.example") is True  # joined
    # Invited but absent (not a Meet participant) → correctly denied.
    assert repo.has_access(doc_id, "ifoster@acme.example") is False


def test_extract_meeting_code():
    from rubik_core.tools.calendar import GoogleCalendarClient as C

    assert (
        C.extract_meeting_code({"hangoutLink": "https://meet.google.com/abc-defg-hij"})
        == "abc-defg-hij"
    )
    assert (
        C.extract_meeting_code({"conferenceData": {"conferenceId": "pso-mudg-myg"}})
        == "pso-mudg-myg"
    )
    assert (
        C.extract_meeting_code(
            {
                "conferenceData": {
                    "entryPoints": [
                        {
                            "entryPointType": "video",
                            "uri": "https://meet.google.com/xyz-uvwx-yz?pin=1",
                        }
                    ]
                }
            }
        )
        == "xyz-uvwx-yz"
    )
    assert C.extract_meeting_code({}) is None


def _seed_backfill_case(tmp_path, monkeypatch, ts, *, joiners):
    """Seed users + one already-ingested transcript whose ACL over-grants an
    invited-but-absent exec (ifoster) and is missing a real joiner (mingram).
    ``joiners`` is what the (mocked) Meet client reports for the meeting.
    Returns (TestSession, repo, doc_id)."""
    from datetime import datetime

    from rubik_core.database.models import Asset
    from rubik_core.database.repository import (
        SQLMeetingParticipantsRepository,
        SQLUserRegistry,
    )

    TestSession = _wire_sqlite(tmp_path, monkeypatch, ts)
    reg = SQLUserRegistry()
    reg.register_user(email="host@acme.example", name="Host User", id="HOST")
    reg.register_user(email="mingram@acme.example", name="Marlo Ingram", id="PQ")
    reg.register_user(email="ifoster@acme.example", name="Indigo Foster", id="CD")

    doc_id = "tx-1"
    with TestSession() as s:
        s.add(
            Asset(
                doc_id=doc_id,
                owner_email="host@acme.example",
                status="approved",
                topic="Meeting Transcripts",
                original_filename="Sales Team L10 - Notes by Gemini",
                allowed_emails=["host@acme.example", "ifoster@acme.example"],
                created_at=datetime(2026, 7, 8, 18, 5, 0),
                updated_at=datetime(2026, 7, 8, 18, 5, 0),
            )
        )
        s.commit()

    repo = SQLMeetingParticipantsRepository()
    repo.upsert_participants(
        doc_id,
        meeting_id="file-1",
        emails=["host@acme.example", "ifoster@acme.example"],
    )

    cal = MagicMock()
    cal.find_meeting_event.return_value = {
        "hangoutLink": "https://meet.google.com/abc-defg-hij"
    }
    monkeypatch.setattr(ts, "_calendar_client_for", lambda user_email: cal)

    meet = MagicMock()
    meet.list_signed_in_participants.return_value = joiners
    monkeypatch.setattr(ts, "_meet_client_for", lambda user_email: meet)
    return TestSession, repo, doc_id


def test_backfill_revokes_absentee_and_adds_joiner(tmp_path, monkeypatch):
    """Attendance-based backfill: an invited-but-absent APP USER (ifoster) is
    revoked; an actual joiner missing from the ACL (mingram) is added; the owner
    is kept. Idempotent."""
    from rubik_core.scripts import transcript_sync as ts

    _TS, repo, doc_id = _seed_backfill_case(
        tmp_path,
        monkeypatch,
        ts,
        joiners=[
            {"user_id": "users/HOST", "display_name": "Host User"},
            {"user_id": "users/PQ", "display_name": "Marlo Ingram"},
        ],
    )

    result = ts.backfill_participants()

    assert result["updated"] == 1
    assert repo.has_access(doc_id, "host@acme.example") is True  # owner kept
    assert repo.has_access(doc_id, "mingram@acme.example") is True  # joiner added
    # Invited but never joined → revoked.
    assert repo.has_access(doc_id, "ifoster@acme.example") is False
    # Idempotent.
    assert ts.backfill_participants()["updated"] == 0


def test_backfill_dry_run_previews_without_writing(tmp_path, monkeypatch):
    """dry_run reports the exact revoke/add plan but changes nothing on disk."""
    from rubik_core.scripts import transcript_sync as ts

    _TS, repo, doc_id = _seed_backfill_case(
        tmp_path,
        monkeypatch,
        ts,
        joiners=[
            {"user_id": "users/HOST", "display_name": "Host User"},
            {"user_id": "users/PQ", "display_name": "Marlo Ingram"},
        ],
    )

    result = ts.backfill_participants(dry_run=True)

    assert result["dry_run"] is True
    change = next(c for c in result["changes"] if c["doc_id"] == doc_id)
    assert change["revoke"] == ["ifoster@acme.example"]
    assert change["add"] == ["mingram@acme.example"]
    # Nothing actually written.
    assert repo.has_access(doc_id, "ifoster@acme.example") is True
    assert repo.has_access(doc_id, "mingram@acme.example") is False


def test_backfill_undetermined_leaves_acl_untouched(tmp_path, monkeypatch):
    """When attendance can't be determined (Meet returns nothing), backfill must
    NOT revoke — real attendees are never dropped on a lookup miss."""
    from rubik_core.scripts import transcript_sync as ts

    _TS, repo, doc_id = _seed_backfill_case(
        tmp_path, monkeypatch, ts, joiners=[]
    )  # no Meet attendance → undetermined

    result = ts.backfill_participants()

    assert result["undetermined"] >= 1
    # ACL preserved as-is — the over-grant is NOT revoked when we can't confirm.
    assert repo.has_access(doc_id, "ifoster@acme.example") is True
    assert repo.has_access(doc_id, "host@acme.example") is True


def test_upsert_participants_drops_placeholder_emails(tmp_path, monkeypatch):
    """Reserved/example addresses scraped from transcript text must never enter
    the authoritative ACL."""
    from datetime import datetime

    from rubik_core.database.models import Asset
    from rubik_core.database.repository import (
        SQLMeetingParticipantsRepository,
        is_placeholder_email,
    )

    assert is_placeholder_email("user@example.com") is True
    assert is_placeholder_email("x@example.org") is True
    assert is_placeholder_email("y@host.invalid") is True
    assert is_placeholder_email("z@server.localhost") is True
    assert is_placeholder_email("real@acme.example") is False  # the Workspace domain
    assert is_placeholder_email("vendor@outside.example") is True
    assert is_placeholder_email("client@acmecorp.alt") is False

    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    with TestSession() as s:
        s.add(
            Asset(
                doc_id="d1",
                owner_email="host@acme.example",
                status="approved",
                topic="Meeting Transcripts",
                created_at=datetime(2026, 6, 26, 1, 0, 0),
                updated_at=datetime(2026, 6, 26, 1, 0, 0),
            )
        )
        s.commit()

    repo = SQLMeetingParticipantsRepository()
    repo.upsert_participants(
        "d1",
        meeting_id="m1",
        emails=["host@acme.example", "user@example.com", "nobody@example.com"],
    )
    assert sorted(repo.list_participants_for_doc("d1")) == ["host@acme.example"]
    assert repo.has_access("d1", "user@example.com") is False


def test_backfill_removes_placeholder_emails_and_cleans_cache(tmp_path, monkeypatch):
    """Backfill scrubs existing junk from both meeting_participants and the
    allowed_emails cache, while keeping real attendees."""
    from datetime import datetime

    from rubik_core.database.models import Asset
    from rubik_core.database.repository import SQLMeetingParticipantsRepository
    from rubik_core.scripts import transcript_sync as ts

    TestSession = _wire_sqlite(tmp_path, monkeypatch, ts)
    # No calendar/Meet available → attendance undetermined, so backfill only
    # strips placeholder junk (never revokes real attendees).
    monkeypatch.setattr(ts, "GoogleCalendarClient", lambda user_email: None)
    monkeypatch.setattr(ts, "_meet_client_for", lambda user_email: None)

    # The owner must be a real Cubie user, else backfill fast-skips the transcript.
    from rubik_core.database.repository import SQLUserRegistry

    SQLUserRegistry().register_user(
        email="host@acme.example", name="Host User", id="HOST"
    )

    doc_id = "tx-junk"
    with TestSession() as s:
        s.add(
            Asset(
                doc_id=doc_id,
                owner_email="host@acme.example",
                status="approved",
                topic="Meeting Transcripts",
                original_filename="Some Meeting - Notes by Gemini",
                allowed_emails=[
                    "host@acme.example",
                    "real@acme.example",
                    "user@example.com",
                ],
                created_at=datetime(2026, 6, 26, 1, 0, 0),
                updated_at=datetime(2026, 6, 26, 1, 0, 0),
            )
        )
        s.commit()

    repo = SQLMeetingParticipantsRepository()
    # Seed the bad state directly (bypass the now-filtering upsert) to mimic
    # rows written before the fix.
    from rubik_core.database.models import MeetingParticipant

    with TestSession() as s:
        for e in ["host@acme.example", "real@acme.example", "user@example.com"]:
            s.add(
                MeetingParticipant(doc_id=doc_id, participant_email=e, meeting_id="m1")
            )
        s.commit()
    assert (
        repo.has_access(doc_id, "user@example.com") is True
    )  # junk present pre-backfill

    result = ts.backfill_participants()

    assert result["participants_removed"] == 1
    assert repo.has_access(doc_id, "user@example.com") is False
    # Real attendees survive.
    assert sorted(repo.list_participants_for_doc(doc_id)) == [
        "host@acme.example",
        "real@acme.example",
    ]
    # The denormalized cache is scrubbed too.
    with TestSession() as s:
        cleaned = s.get(Asset, doc_id).allowed_emails
    assert "user@example.com" not in cleaned
    assert sorted(cleaned) == ["host@acme.example", "real@acme.example"]


def test_list_transcripts_endpoint_is_admin_only_not_exec(tmp_path, monkeypatch):
    """Execs are scoped to their own/attended transcripts like any normal user;
    only admins see everything."""
    from datetime import datetime
    from types import SimpleNamespace

    import backend.routers.transcripts as tx_router
    from rubik_core.database.models import Asset
    from rubik_core.database.repository import SQLMeetingParticipantsRepository

    TestSession = _wire_sqlite(tmp_path, monkeypatch, tx_router)

    with TestSession() as s:
        # One meeting the exec attended, one they did not.
        s.add(
            Asset(
                doc_id="mine",
                owner_email="someone@acme.example",
                status="approved",
                topic="Meeting Transcripts",
                original_filename="My Meeting",
                created_at=datetime(2026, 6, 26, 1, 0, 0),
                updated_at=datetime(2026, 6, 26, 1, 0, 0),
            )
        )
        s.add(
            Asset(
                doc_id="theirs",
                owner_email="someone@acme.example",
                status="approved",
                topic="Meeting Transcripts",
                original_filename="Not My Meeting",
                created_at=datetime(2026, 6, 26, 2, 0, 0),
                updated_at=datetime(2026, 6, 26, 2, 0, 0),
            )
        )
        s.commit()

    repo = SQLMeetingParticipantsRepository()
    repo.upsert_participants("mine", meeting_id="m1", emails=["exec@acme.example"])
    repo.upsert_participants("theirs", meeting_id="m2", emails=["other@acme.example"])

    # An exec WITH the org-wide flag must still be scoped to their own meeting.
    exec_user = SimpleNamespace(
        email="exec@acme.example", is_admin=False, sees_all_departments=True
    )
    resp = tx_router.list_transcripts(user=exec_user)
    doc_ids = {r["doc_id"] for r in resp["transcripts"]}
    assert doc_ids == {"mine"}

    # An admin sees everything.
    admin_user = SimpleNamespace(
        email="admin@acme.example", is_admin=True, sees_all_departments=False
    )
    resp_admin = tx_router.list_transcripts(user=admin_user)
    assert {r["doc_id"] for r in resp_admin["transcripts"]} == {"mine", "theirs"}


def test_fathom_extract_participants_real_schema():
    """Regression: extract_participants_from_meeting must read the REAL Fathom
    fields (recorded_by + calendar_invitees). The legacy attendees/host_email
    fields it used don't exist in the API response, so it returned [] for every
    meeting — leaving the ACL empty and transcripts unqueryable."""
    from rubik_core.integrations.fathom import FathomClient

    c = FathomClient(api_key="x")

    meeting = {
        "recorded_by": {"email": "Rep@acme.example", "name": "Rep", "team": "Sales"},
        "calendar_invitees": [
            {"email": "Internal@acme.example", "is_external": False},
            {"email": "client@acmecorp.alt", "is_external": True},
            {"name": "no-email person"},
        ],
    }
    assert set(c.extract_participants_from_meeting(meeting)) == {
        "rep@acme.example",
        "internal@acme.example",
        "client@acmecorp.alt",
    }

    # Legacy shape still tolerated (webhook payloads / future API versions).
    legacy = {"attendees": [{"email": "A@x.alt"}], "host_email": "H@x.alt"}
    assert set(c.extract_participants_from_meeting(legacy)) == {"a@x.alt", "h@x.alt"}

    # Missing/empty fields must not crash.
    assert c.extract_participants_from_meeting({}) == []


def test_fathom_participants_gated_on_transcript_attendance():
    """Attendance gate: an invited-but-absent person (never a transcript speaker)
    must NOT get transcript access, while people who actually spoke — and the
    recorder — do. Guards the "I shouldn't have access to the sales L10" feedback."""
    from rubik_core.integrations.fathom import FathomClient

    c = FathomClient(api_key="x")
    meeting = {
        "recorded_by": {"email": "emery@acme.example", "name": "Emerson Brooks"},
        "calendar_invitees": [
            {"email": "emery@acme.example", "name": "Emerson Brooks"},
            {"email": "parker@acme.example", "name": "Parker Jensen"},
            # invited but never spoke → must be dropped
            {"email": "ifoster@acme.example", "name": "Indigo Foster"},
        ],
        "transcript": [
            {"speaker_name": "Emerson Brooks", "text": "Let's start."},
            {"speaker_name": "Parker Jensen", "text": "Pipeline is clean."},
        ],
    }
    allowed = set(c.extract_participants_from_meeting(meeting))
    assert allowed == {"emery@acme.example", "parker@acme.example"}
    assert "ifoster@acme.example" not in allowed


def test_fathom_participants_family_name_match_tolerates_nickname():
    """A first-name nickname still matches on the family name (invitee 'Emery
    Brooks' vs transcript speaker 'Emerson Brooks')."""
    from rubik_core.integrations.fathom import FathomClient

    c = FathomClient(api_key="x")
    meeting = {
        "calendar_invitees": [
            {"email": "emery@acme.example", "name": "Emery Brooks"},
            {"email": "absent@acmecorp.alt", "name": "Jane Absent"},
        ],
        "transcript": [{"speaker_name": "Emerson Brooks", "text": "Hi."}],
    }
    allowed = set(c.extract_participants_from_meeting(meeting))
    assert allowed == {"emery@acme.example"}


def test_fathom_participants_fail_open_when_no_invitee_matches():
    """If NObody on the invite matches the speakers (unexpected speaker-name
    format), fail open to the full invite list rather than lock attendees out."""
    from rubik_core.integrations.fathom import FathomClient

    c = FathomClient(api_key="x")
    meeting = {
        "calendar_invitees": [
            {"email": "a@x.alt", "name": "Alice Anderson"},
            {"email": "b@x.alt", "name": "Bob Brown"},
        ],
        # Opaque speaker labels — no family-name overlap with any invitee.
        "transcript": [{"speaker_name": "Speaker 1", "text": "..."}],
    }
    allowed = set(c.extract_participants_from_meeting(meeting))
    assert allowed == {"a@x.alt", "b@x.alt"}
