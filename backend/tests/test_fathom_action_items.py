"""Tests for capturing Fathom's action items + AI summary at ingest.

Drafted follow-ups were dropping call to-do's because only the raw transcript
dialogue was ingested — Fathom's own action-item and summary fields were
discarded. ``build_ingest_text`` now appends them so a "draft a follow-up" /
"what did we cover" query can surface every next step. Extraction is defensive
(multiple key shapes, no-op when absent) so a Fathom schema change can't break
ingestion.
"""

from rubik_core.integrations.fathom import FathomClient


def _client() -> FathomClient:
    return FathomClient(api_key="test-key")


def test_extract_action_items_text_with_assignees():
    meeting = {
        "action_items": [
            {
                "description": "Connect with a labor-law attorney",
                "assignee": {"name": "Arden"},
            },
            {
                "text": "Schedule an IT audit",
                "assignee": {"email": "arden@example.com"},
            },
            {"description": "Meet after the holiday"},
        ]
    }
    out = _client().extract_action_items_text(meeting)
    assert "- Connect with a labor-law attorney (owner: Arden)" in out
    assert "- Schedule an IT audit (owner: arden@example.com)" in out
    assert "- Meet after the holiday" in out


def test_extract_action_items_text_tolerates_strings_and_missing():
    c = _client()
    assert c.extract_action_items_text({"action_items": ["Send the proposal"]}) == (
        "- Send the proposal"
    )
    assert c.extract_action_items_text({"transcript": []}) == ""
    # A wrong shape (not a list) must no-op, never raise.
    assert c.extract_action_items_text({"action_items": "nope"}) == ""


def test_extract_summary_text_variants():
    c = _client()
    assert c.extract_summary_text(
        {"default_summary": "We discussed invoice holdbacks"}
    ) == ("We discussed invoice holdbacks")
    assert c.extract_summary_text({"summary": {"markdown": "## Notes"}}) == "## Notes"
    assert c.extract_summary_text({"ai_summary": "Recap"}) == "Recap"
    assert c.extract_summary_text({"transcript": []}) == ""


def test_build_ingest_text_appends_action_items_and_summary():
    meeting = {
        "transcript": [{"speaker_name": "Emery", "text": "Hello Arden"}],
        "action_items": [{"description": "Send proposal"}],
        "default_summary": "Great call about vendor oversight.",
    }
    out = _client().build_ingest_text(meeting)
    assert "[Emery]: Hello Arden" in out
    assert "## Action Items & Next Steps (from Fathom)" in out
    assert "- Send proposal" in out
    assert "## Meeting Summary (from Fathom)" in out
    assert "Great call about vendor oversight." in out
    # Action items appear before the summary section.
    assert out.index("Action Items") < out.index("Meeting Summary")


def test_build_ingest_text_transcript_only_is_unchanged():
    meeting = {"transcript": [{"speaker_name": "A", "text": "hi"}]}
    # When Fathom carries no summary/action items, ingest text == transcript only.
    assert _client().build_ingest_text(meeting) == "[A]: hi"
