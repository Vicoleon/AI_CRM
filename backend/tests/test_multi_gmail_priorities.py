"""Connected mailboxes in owner priorities stay distinct and private in team views."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.agents import slack_delivery
from rubik_core.offboarding import sweep
from rubik_core.priorities import ChannelKind, EntityKind, PrioritiesConfig
from rubik_core.priorities.agents import GmailChannelAgent
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelSignal,
    PrioritiesBundle,
    PriorityCandidate,
)
from rubik_core.priorities.orchestrator import (
    _private_gmail_context_from_digests,
    _stamp_gmail_provenance,
    bundle_from_metadata,
    bundle_to_metadata,
)
from rubik_core.priorities.privacy import is_private_gmail_bundle
from rubik_core.tenancy.context import use_workspace

WORK = "owner@company.example"
PERSONAL = "owner@gmail.example"
ACCOUNT_ID = "b056f252-a20d-4b1d-9b76-316b7bd6769a"


def _thread(tid: str, mailbox: str, *, minutes_ago: int = 1) -> dict:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "id": tid,
        "messages": [
            {
                "id": "message-1",
                "internalDate": str(int(when.timestamp() * 1000)),
                "snippet": "Please review this today.",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "sender@external.example"},
                        {"name": "To", "value": mailbox},
                        {"name": "Subject", "value": "Needs review"},
                    ]
                },
            }
        ],
    }


def _connected_accounts(monkeypatch) -> None:
    monkeypatch.setattr(
        "rubik_core.tenancy.gmail_accounts.multi_gmail_enabled", lambda: True
    )
    monkeypatch.setattr(
        "rubik_core.tenancy.gmail_accounts.list_accounts",
        lambda owner: [
            {
                "id": ACCOUNT_ID,
                "email": PERSONAL,
                "status": "connected",
                "ai_provider_fingerprint": "a" * 64,
            }
        ],
    )


def _agent() -> GmailChannelAgent:
    return GmailChannelAgent(user_email=WORK, config=PrioritiesConfig.default())


def test_same_raw_thread_in_two_mailboxes_keeps_distinct_signals(monkeypatch):
    _connected_accounts(monkeypatch)

    class GmailClient:
        def __init__(self, *, user_email, account_id=None):
            assert user_email == WORK
            self.account_id = account_id

        def search_threads(self, *, query, max_results):
            if "in:inbox" not in query:
                return []
            mailbox = PERSONAL if self.account_id else WORK
            return [_thread("shared-thread", mailbox)]

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", GmailClient)
    agent = _agent()
    with use_workspace("test"):
        signals = agent.parse_signals(agent.fetch_signals())
    assert {sig.id for sig in signals} == {
        "shared-thread",
        f"{ACCOUNT_ID}:shared-thread",
    }
    work = next(sig for sig in signals if not sig.attrs["personal_gmail"])
    personal = next(sig for sig in signals if sig.attrs["personal_gmail"])
    assert work.entity_id == "shared-thread"
    assert personal.entity_id == f"{ACCOUNT_ID}:shared-thread"
    assert personal.attrs["gmail_thread_id"] == "shared-thread"
    assert personal.attrs["gmail_mailbox"] == PERSONAL
    assert personal.attrs["addressed_to_reader"] == "direct"
    assert personal.link == (
        "https://mail.google.com/mail/?authuser=owner%40gmail.example"
        "#inbox/shared-thread"
    )
    prompt = agent.build_prompt(signals)
    assert f"mailbox: {WORK}" in prompt
    assert f"mailbox: {PERSONAL}" in prompt


def test_offboarding_reads_only_delegated_work_mail(monkeypatch):
    _connected_accounts(monkeypatch)
    mailbox_ids = []

    class GmailClient:
        def __init__(self, *, user_email, account_id=None):
            assert user_email == WORK
            mailbox_ids.append(account_id)

        def search_threads(self, *, query, max_results):
            if "in:inbox" not in query:
                return []
            return [_thread("work-handoff", WORK)]

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", GmailClient)
    monkeypatch.setattr(
        "rubik_core.priorities.agents.calendar_agent.CalendarChannelAgent.fetch_signals",
        lambda self: [],
    )
    monkeypatch.setattr(
        "rubik_core.priorities.agents.drive_agent.DriveChannelAgent.fetch_signals",
        lambda self: [],
    )
    with use_workspace("test"):
        signals, ok = sweep._google_last_pass(WORK, [])
    assert ok is True
    assert mailbox_ids == [None]
    assert all(PERSONAL not in str(signal) for signal in signals)


def test_busy_work_inbox_keeps_personal_mailbox_in_signal_budget():
    agent = _agent()
    raw = [_thread(f"work-{i}", WORK, minutes_ago=i + 1) for i in range(25)]
    raw.append(
        {
            **_thread("personal-old", PERSONAL, minutes_ago=120),
            "_gmail_account_id": ACCOUNT_ID,
            "_gmail_mailbox": PERSONAL,
        }
    )
    signals = agent.parse_signals(raw)
    assert len(signals) == agent.MAX_SIGNALS_OUT
    assert any(sig.attrs["personal_gmail"] for sig in signals)
    assert any(not sig.attrs["personal_gmail"] for sig in signals)


def test_card_mailbox_label_and_private_run_flag_survive_serialization():
    agent = _agent()
    raw = [
        _thread("work-thread", WORK),
        {
            **_thread("personal-thread", PERSONAL),
            "_gmail_account_id": ACCOUNT_ID,
            "_gmail_mailbox": PERSONAL,
        },
    ]
    signals = agent.parse_signals(raw)
    by_id = {signal.id: signal for signal in signals}
    personal_id = f"{ACCOUNT_ID}:personal-thread"
    cards = [
        PriorityCandidate(
            title="Personal request",
            entity_kind=EntityKind.THREAD,
            entity_id="personal-thread",
            source_channels=[ChannelKind.GMAIL],
            source_links=[by_id[personal_id].link],
            signal_ids=[personal_id],
        ),
        PriorityCandidate(
            title="Work request",
            entity_kind=EntityKind.THREAD,
            entity_id="work-thread",
            source_channels=[ChannelKind.GMAIL],
            source_links=[by_id["work-thread"].link],
            signal_ids=["work-thread"],
        ),
        PriorityCandidate(
            title="Reach out to personal contact",
            entity_kind=EntityKind.PERSON,
            entity_id="contact@example.com",
            source_channels=[ChannelKind.GMAIL],
            source_links=[by_id[personal_id].link],
            signal_ids=[personal_id],
        ),
        PriorityCandidate(title="Uncited model wording"),
    ]
    _stamp_gmail_provenance(cards, by_id)
    assert cards[0].entity_id == personal_id
    assert cards[0].attrs["gmail_mailbox"] == PERSONAL
    assert cards[0].attrs["gmail_account_id"] == ACCOUNT_ID
    assert cards[0].attrs["personal_gmail"] is True
    assert cards[1].attrs["gmail_mailbox"] == WORK
    assert "personal_gmail" not in cards[1].attrs
    assert cards[2].attrs["gmail_account_id"] == ACCOUNT_ID
    assert cards[2].attrs["gmail_mailbox"] == PERSONAL
    assert cards[2].entity_id == "contact@example.com"
    assert "gmail_mailbox" not in cards[3].attrs
    assert "gmail_account_id" not in cards[3].attrs
    assert "personal_gmail" not in cards[3].attrs
    assert all(card.attrs["private_gmail_context"] is True for card in cards)

    with use_workspace("test-workspace"):
        bundle = PrioritiesBundle(
            user_email=WORK,
            crm_provenance={
                "workspace": "test-workspace",
                "deal_ids": [],
                "unprovable": False,
            },
            priorities=cards,
        )
        restored = bundle_from_metadata(WORK, bundle_to_metadata(bundle))
    assert restored.priorities[0].attrs["gmail_mailbox"] == PERSONAL
    assert restored.priorities[0].attrs["gmail_account_id"] == ACCOUNT_ID
    assert restored.priorities[0].attrs["personal_gmail"] is True
    assert restored.priorities[1].attrs["private_gmail_context"] is True
    assert restored.priorities[2].attrs["gmail_account_id"] == ACCOUNT_ID
    assert "gmail_mailbox" not in restored.priorities[3].attrs


def test_empty_run_keeps_personal_mail_privacy_at_bundle_level():
    personal = ChannelSignal(
        id="personal-thread",
        channel=ChannelKind.GMAIL,
        title="Private message",
        attrs={"personal_gmail": True, "gmail_mailbox": PERSONAL},
    )
    digest = ChannelDigest(channel=ChannelKind.GMAIL, signals=[personal])
    assert _private_gmail_context_from_digests([digest]) is True

    with use_workspace("test-workspace"):
        bundle = PrioritiesBundle(
            user_email=WORK,
            crm_provenance={
                "workspace": "test-workspace",
                "deal_ids": [],
                "unprovable": False,
            },
            private_gmail_context=_private_gmail_context_from_digests([digest]),
            priorities=[],
        )
        metadata = bundle_to_metadata(bundle)
        restored = bundle_from_metadata(WORK, metadata)
    assert metadata["priorities"] == []
    assert metadata["private_gmail_context"] is True
    assert is_private_gmail_bundle(metadata) is True
    assert restored.private_gmail_context is True


def test_manager_digest_rejects_top_level_private_bundle_without_card_flags(
    monkeypatch,
):
    from backend.routers.home import _bundle_dict_from_metadata

    with use_workspace("test-workspace"):
        metadata = bundle_to_metadata(
            PrioritiesBundle(
                user_email=WORK,
                crm_provenance={
                    "workspace": "test-workspace",
                    "deal_ids": [],
                    "unprovable": False,
                },
                private_gmail_context=True,
                priorities=[
                    PriorityCandidate(
                        title="Work-looking card",
                        source_channels=[ChannelKind.GMAIL],
                        entity_kind=EntityKind.THREAD,
                        entity_id="work-thread",
                    )
                ],
            )
        )
        projected = _bundle_dict_from_metadata(metadata)
    assert projected is not None
    assert projected["private_gmail_context"] is True
    assert not projected["priorities"][0]["attrs"]

    monkeypatch.setattr(
        "backend.routers.home._load_today_priorities", lambda email: projected
    )
    monkeypatch.setattr(slack_delivery, "_department_members", lambda db, dept: [WORK])
    assert len(slack_delivery._load_bundle_priority_items(WORK)) == 1
    assert slack_delivery._team_priorities_for_department(object(), "sales") == []


def test_channel_exceptions_never_log_or_persist_private_mail_text(monkeypatch, caplog):
    from rubik_core.priorities import PrioritiesOrchestrator, PrioritiesRequest
    from rubik_core.priorities.orchestrator import CHANNEL_AGENT_REGISTRY

    secret = "PRIVATE-MAIL-SUBJECT"
    agent = _agent()

    def failed_fetch():
        raise RuntimeError(secret)

    monkeypatch.setattr(agent, "fetch_signals", failed_fetch)
    with caplog.at_level("WARNING"):
        digest = agent.run()
    assert digest.error == "fetch_failed: RuntimeError"
    assert secret not in caplog.text
    assert secret not in digest.error

    class CrashedAgent:
        channel = ChannelKind.GMAIL

        def __init__(self, **kwargs):
            pass

        def run(self):
            raise RuntimeError(secret)

    monkeypatch.setitem(CHANNEL_AGENT_REGISTRY, ChannelKind.GMAIL, CrashedAgent)
    orchestrator = PrioritiesOrchestrator(config=PrioritiesConfig.default())
    with caplog.at_level("WARNING"):
        [crashed] = orchestrator._run_channels(
            PrioritiesRequest(user_email=WORK, channels=[ChannelKind.GMAIL]),
            [],
            None,
        )
    assert crashed.error == "agent_crashed: RuntimeError"
    assert secret not in caplog.text
    assert secret not in crashed.error


def test_owner_dm_labels_personal_mail_while_manager_digest_excludes_it(monkeypatch):
    cards = [
        {
            "title": "Personal request",
            "why_it_matters": "A reply is due.",
            "attrs": {
                "gmail_mailbox": PERSONAL,
                "personal_gmail": True,
                "private_gmail_context": True,
            },
        },
        {
            "title": "Work request in mixed run",
            "attrs": {"gmail_mailbox": WORK, "private_gmail_context": True},
        },
        {
            "title": "Uncited wording from mixed run",
            "attrs": {"private_gmail_context": True},
        },
        {"title": "Clean work request", "attrs": {"gmail_mailbox": WORK}},
    ]
    monkeypatch.setattr(
        "backend.routers.home._load_today_priorities",
        lambda email: {"priorities": cards},
    )
    monkeypatch.setattr(
        "backend.routers.home.load_next_week_priorities",
        lambda email: {"priorities": cards[:1]},
    )
    owner_items = slack_delivery._load_bundle_priority_items(WORK)
    personal, work_from_mixed_run, uncited_from_mixed_run, clean_work = owner_items
    assert [item.title for item in owner_items] == [card["title"] for card in cards]
    assert "*Mailbox:* owner@gmail.example" in personal.body
    assert (
        "*Mailbox:* owner@gmail.example"
        in slack_delivery._load_bundle_priority_items(
            WORK, report=slack_delivery.REPORT_KIND_NEXT_WEEK
        )[0].body
    )
    assert not work_from_mixed_run.personal_gmail
    assert "*Mailbox:*" not in work_from_mixed_run.body

    monkeypatch.setattr(slack_delivery, "_department_members", lambda db, dept: [WORK])
    team = slack_delivery._team_priorities_for_department(object(), "sales")
    # A mixed run is private as a whole: even a work-looking card may contain
    # personal context from synthesis.
    assert team == []


def test_reauth_notice_keeps_work_signals_without_logging_mail_subject(monkeypatch):
    _connected_accounts(monkeypatch)

    class GmailClient:
        def __init__(self, *, user_email, account_id=None):
            if account_id:
                raise PermissionError("refresh denied")

        def search_threads(self, *, query, max_results):
            return [_thread("work-thread", WORK)] if "in:inbox" in query else []

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", GmailClient)
    agent = _agent()
    monkeypatch.setattr(
        agent.client,
        "invoke_json",
        lambda prompt, fallback: {"summary": "One work thread", "thread_attrs": {}},
    )
    digest = agent.run()
    assert len(digest.signals) == 1
    assert digest.signals[0].id == "work-thread"
    assert digest.error and "reconnect additional Gmail mailbox" in digest.error
    assert PERSONAL not in digest.error
    assert "Needs review" not in digest.error
    assert digest.private_gmail_context is True
    assert _private_gmail_context_from_digests([digest]) is True


def test_one_failed_connected_mail_lane_marks_the_run_partial(monkeypatch):
    _connected_accounts(monkeypatch)

    class GmailClient:
        def __init__(self, *, user_email, account_id=None):
            self.account_id = account_id

        def search_threads(self, *, query, max_results):
            if self.account_id and "in:inbox" in query:
                raise TimeoutError("PRIVATE MAIL QUERY")
            if not self.account_id and "in:inbox" in query:
                return [_thread("work-thread", WORK)]
            return []

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", GmailClient)
    agent = _agent()
    raw = agent.fetch_signals()
    assert len(raw) == 1
    assert agent._additional_mailbox_errors == [ACCOUNT_ID]
