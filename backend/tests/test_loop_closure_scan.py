"""Loose Ends — ``run_scan`` end to end, against fakes and SQLite.

Every outside system arrives through ``ScanDeps``: a fake Gmail (raw
``format=metadata`` thread dicts, exactly what ``GmailClient.search_threads``
returns), a fake Slack service, a fake HubSpot read client that answers the
searches, batch reads and association reads the index makes, and a scripted
judge. Nothing here can reach the network.

What is pinned: the named buckets and per-AE coverage of a real run; a second
morning closing what was done, with ids-only evidence; a dry run that decides
everything and keeps nothing; a failed judge creating no NEW open loops while
existing ones are still re-checked; HubSpot down closing nothing; the run lock;
one AE's failure not costing the others; and promise hearing only for AEs the
priorities batch does not cover.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.config import settings
from rubik_core.database.loop_closure import LoopItem, LoopScanRun
from rubik_core.loop_closure import hubspot_index
from rubik_core.loop_closure import judge as judge_mod
from rubik_core.loop_closure.scan import ScanDeps, run_scan
from rubik_core.retrieval.hubspot_api import (
    CompanyMatch,
    DomainMatch,
    HubSpotAPIError,
)
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

#: Every ``@acme.example`` colleague here is a roster member, so this module
#: opts into the one fixture carve-out (``conftest.roster_admits_acme_example``).
pytestmark = pytest.mark.usefixtures("roster_admits_acme_example")


@pytest.fixture(autouse=True)
def _workspace_hubspot_pipeline(monkeypatch):
    from rubik_core.loop_closure import hubspot_index
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )
    # This fake portal's companies are wholly owned. The production helper
    # uses X-Ray's complete live company/deal association proof.
    monkeypatch.setattr(hubspot_index, "_strict_company_ids", lambda ids: set(ids))


UTC = timezone.utc
NOW = datetime(2026, 10, 8, 12, 10, tzinfo=UTC)  # Thu 07:10 Chicago
MON_10 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
AE = "rep@acme.example"
AE2 = "second@acme.example"
JANE = "jane@acmecorp.example"
BOB = "bob@acmecorp.example"
CARL = "carl@acmecorp.example"


# ── Fakes ────────────────────────────────────────────────────────────────────


def _raw(
    mid,
    tid,
    sender,
    to,
    at,
    subject,
    *,
    cc=(),
    snippet="hello",
    extra=(),
    labels=("INBOX",),
):
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": ", ".join(to)},
        {"name": "Subject", "value": subject},
        *({"name": k, "value": v} for k, v in extra),
    ]
    if cc:
        headers.append({"name": "Cc", "value": ", ".join(cc)})
    return {
        "id": mid,
        "threadId": tid,
        "internalDate": str(int(at.timestamp() * 1000)),
        "snippet": snippet,
        "labelIds": list(labels),
        "payload": {"headers": headers},
    }


class FakeGmail:
    def __init__(self, threads: dict[str, list[dict]], *, fail: bool = False):
        self.threads = threads
        self.fail = fail
        self.queries: list[str] = []
        # Threads outside the lookback window (search misses them) and threads
        # deleted since (a direct read raises, like Gmail's 404).
        self.hidden: set[str] = set()
        self.gone: set[str] = set()

    def _t(self, tid):
        return {"id": tid, "messages": list(self.threads[tid])}

    def search_threads(self, query="", max_results=20):
        if self.fail:
            raise RuntimeError("Gmail service not initialized.")
        self.queries.append(query)
        return [self._t(t) for t in self.threads if t not in self.hidden]

    def _hydrate_threads(self, ids):
        if any(t in self.gone for t in ids):
            raise RuntimeError("HttpError 404")
        return [self._t(t) for t in ids if t in self.threads]


class FakeSlack:
    def __init__(self, messages=(), *, connected=True, uid="U1", scope_error=False):
        self.is_connected = connected
        self.messages = list(messages)
        self.uid = uid
        self.scope_error = scope_error
        self.raise_on_scope: list[bool] = []

    def slack_user_id(self):
        return self.uid

    def search_messages(self, query, limit=20, *, sort="score", raise_on_scope=False):
        self.raise_on_scope.append(raise_on_scope)
        if self.scope_error:
            if raise_on_scope:
                from backend.services.slack_service import SlackAPIError

                raise SlackAPIError("missing_scope")
            return []
        return list(self.messages)


def _hs_ms(value) -> int:
    return int(value)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeHubSpot:
    """Answers the reads ``hubspot_index.build_index`` makes."""

    MAX_RECORDS = 5000

    def __init__(self):
        self.fail = False
        self.owners = {"o1": {"email": AE, "name": "Rita Rep"}}
        self.contacts = {
            JANE: ("c1", "Jane", "Doe"),
            BOB: ("c2", "Bob", "Roe"),
            CARL: ("c3", "Carl", "Coe"),
        }
        self.assoc = {
            ("contacts", "companies"): {"c1": ["co1"], "c2": ["co1"], "c3": ["co1"]},
            ("contacts", "deals"): {"c1": ["d1"], "c2": ["d1"], "c3": ["d1"]},
            ("companies", "deals"): {"co1": ["d1"]},
            ("deals", "companies"): {"d1": ["co1"]},
        }
        self.companies = {"co1": {"name": "Acme Supply", "domain": "acmecorp.example"}}
        self.deals = {
            "d1": {
                "dealname": "Acme Onboarding",
                "pipeline": "default",
                "hubspot_owner_id": "o1",
                "open": True,
            }
        }
        # (type, id, at, {target: [ids]}, incoming)
        self.engagements: list[tuple] = []
        self.tasks = {
            "t9": {
                "hs_task_subject": "Call Jane back",
                "hs_timestamp": _iso(NOW - timedelta(days=5)),
                "hs_task_status": "NOT_STARTED",
                "hubspot_owner_id": "o1",
                "companies": ["co1"],
                "deals": ["d1"],
            }
        }

    def _maybe_fail(self):
        if self.fail:
            raise HubSpotAPIError("HubSpot contacts search failed: HTTP 503: down")

    def owner_directory(self):
        return {} if self.fail else dict(self.owners)

    @staticmethod
    def _in_window(value_iso: str, filters) -> bool:
        at = datetime.fromisoformat(value_iso.replace("Z", "+00:00"))
        ms = int(at.timestamp() * 1000)
        for f in filters:
            if f["propertyName"] != "hs_timestamp":
                continue
            v = _hs_ms(f["value"])
            if f["operator"] == "GTE" and ms < v:
                return False
            if f["operator"] == "LT" and ms >= v:
                return False
        return True

    def search_all(self, object_type, filters, properties, sorts=None):
        self._maybe_fail()
        values = {f["propertyName"]: f.get("values") or f.get("value") for f in filters}
        if object_type == "contacts":
            wanted = values.get("email") or []
            return [
                {"id": cid, "properties": {"email": e, "firstname": f, "lastname": ln}}
                for e, (cid, f, ln) in self.contacts.items()
                if e in wanted
            ]
        if object_type == "deals":
            owners = values.get("hubspot_owner_id") or []
            return [
                {"id": did, "properties": {k: v for k, v in d.items() if k != "open"}}
                for did, d in self.deals.items()
                if d["open"] and d["hubspot_owner_id"] in owners
            ]
        if object_type == "tasks":
            owners = values.get("hubspot_owner_id") or []
            return [
                {
                    "id": tid,
                    "properties": {k: v for k, v in t.items() if k != "companies"},
                }
                for tid, t in self.tasks.items()
                if t["hubspot_owner_id"] in owners
                and t["hs_task_status"] != "COMPLETED"
                and self._in_window(t["hs_timestamp"], filters)
            ]
        rows = []
        for etype, eid, at, _links, incoming in self.engagements:
            if etype != object_type or not self._in_window(_iso(at), filters):
                continue
            props = {"hs_timestamp": _iso(at)}
            if etype == "emails":
                props["hs_email_direction"] = "INCOMING_EMAIL" if incoming else "EMAIL"
            rows.append({"id": eid, "properties": props})
        return rows

    def batch_associations(self, from_type, to_type, ids, *, prefer_type_id=None):
        self._maybe_fail()
        if from_type in ("emails", "calls", "meetings", "notes"):
            out = {i: [] for i in ids}
            for etype, eid, _at, links, _inc in self.engagements:
                if etype == from_type and eid in out:
                    out[eid] = list(links.get(to_type, []))
            return out
        if from_type == "tasks":
            key = {"companies": "companies", "deals": "deals"}.get(to_type)
            return {
                i: list(self.tasks.get(i, {}).get(key, [])) if key else [] for i in ids
            }
        table = self.assoc.get((from_type, to_type), {})
        return {i: list(table.get(i, [])) for i in ids}

    def fully_owned_deal_associations(self, object_type, ids):
        self._maybe_fail()
        if object_type == "tasks":
            raw = {tid: self.tasks.get(tid, {}).get("deals", []) for tid in ids}
        elif object_type in ("emails", "calls", "meetings", "notes"):
            raw = {
                eid: links.get("deals", [])
                for etype, eid, _at, links, _inc in self.engagements
                if etype == object_type and eid in ids
            }
        else:
            raw = self.assoc.get((object_type, "deals"), {})
        allowed = hubspot_index._allowed_pipelines()
        return {
            record_id: list(raw[record_id])
            for record_id in ids
            if raw.get(record_id)
            and all(
                self.deals.get(deal_id, {}).get("pipeline") in allowed
                for deal_id in raw[record_id]
            )
        }

    def batch_read(self, object_type, ids, properties, **kwargs):
        self._maybe_fail()
        if object_type == "companies":
            return [
                {"id": i, "properties": self.companies[i]}
                for i in ids
                if i in self.companies
            ]
        if object_type == "deals":
            return [
                {
                    "id": i,
                    "properties": {
                        k: v for k, v in self.deals[i].items() if k != "open"
                    },
                }
                for i in ids
                if i in self.deals
            ]
        if object_type == "tasks":
            return [
                {
                    "id": i,
                    "properties": {
                        k: self.tasks[i][k]
                        for k in ("hs_task_status", "hs_timestamp", "hubspot_owner_id")
                        if k in properties
                    },
                }
                for i in ids
                if i in self.tasks
            ]
        return []

    def companies_for_domains(self, domains, *, properties=None):
        out = {}
        for d in domains:
            if d == "acmecorp.example":
                out[d] = DomainMatch(
                    domain=d,
                    candidates=(
                        CompanyMatch(
                            hubspot_company_id="co1", name="Acme Supply", domain=d
                        ),
                    ),
                )
            else:
                out[d] = DomainMatch(domain=d)
        return out


def _no_sleep_retry(fn, *args, **kwargs):
    return fn(*args, **kwargs)


class ScriptedJudge:
    """Says yes to every email except Carl's and to every Slack message."""

    def __init__(self, *, fail=False, raise_for=None):
        self.fail = fail
        self.raise_for = raise_for
        self.calls: list[tuple[list, list]] = []

    def __call__(self, emails, slack, *, owner_name="", now=None):
        self.calls.append((emails, slack))
        if self.raise_for and owner_name == self.raise_for:
            raise RuntimeError("boom")
        if self.fail:
            return None
        return judge_mod.JudgeResult(
            emails={
                e.tag: judge_mod.EmailVerdict(
                    tag=e.tag,
                    expects_reply="Carl" not in e.sender_name,
                    business_request=True,
                    headline=f"Asks about {e.subject}",
                )
                for e in emails
            },
            slack={
                s.tag: judge_mod.SlackVerdict(
                    tag=s.tag, reports_client_touch=True, headline="Call with Acme"
                )
                for s in slack
            },
        )


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base, User

    assert "loop_items" in Base.metadata.tables
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    monkeypatch.setattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", False)
    # Loose Ends ships open to everyone. These tests run the configuration an
    # admin sets when it is for the sales team only — the Sales department on
    # the roster — as Feature Access data.
    from backend.tests._rosters import pin_rosters
    from rubik_core.features import FeaturePolicy

    pin_rosters(
        monkeypatch,
        {
            "loop_closure": FeaturePolicy(
                departments=frozenset({"sales"}),
                exec_allowed=False,
                admin_allowed=False,
            )
        },
    )
    # The roster (Feature Access) decides who may be scanned; no admin override
    # cached by another suite may leak in.
    from rubik_core.features.policy import clear_cache

    clear_cache()
    session = Session()
    session.add(User(email=AE, id="rep", name="Rita Rep", department="Sales"))
    session.add(User(email=AE2, id="sec", name="Sam Second", department="Sales"))
    session.commit()
    yield session
    session.close()


def _threads() -> dict[str, list[dict]]:
    return {
        # AE emailed Jane (a contact) Monday; nothing in HubSpot.
        "tU": [_raw("mU1", "tU", AE, [JANE], MON_10, "Onboarding pricing")],
        # AE emailed Bob; a call is logged on Bob that afternoon.
        "tL": [_raw("mL1", "tL", AE, [BOB], MON_10, "Kickoff")],
        # Jane wrote to the AE Monday and waits.
        "tO": [
            _raw("mO1", "tO", f"Jane Doe <{JANE}>", [AE], MON_10, "Pricing question")
        ],
        # Carl wrote too, but the judge says it expects no reply.
        "tF": [_raw("mF1", "tF", f"Carl Coe <{CARL}>", [AE], MON_10, "Thanks!")],
        # A newsletter.
        "tN": [
            _raw(
                "mN1",
                "tN",
                "news@vendor.com",
                [AE],
                MON_10,
                "Weekly digest",
                extra=[("List-Unsubscribe", "<mailto:x@vendor.com>")],
            )
        ],
    }


def _slack_msgs():
    return [
        SimpleNamespace(
            channel_id="C1",
            channel_name="sales",
            ts=f"{MON_10.timestamp():.0f}.000100",
            text="Just got off a call with Acme Supply about the audit",
            permalink="https://slack.example/p1",
            app_link=None,
        )
    ]


def _deps(gmail=None, hubspot=None, judge=None, slack=None, **kw) -> ScanDeps:
    gmail = gmail or FakeGmail(_threads())
    slack = slack if slack is not None else FakeSlack(_slack_msgs())
    hubspot = hubspot or FakeHubSpot()
    if not any(e[1] == "k1" for e in hubspot.engagements):
        hubspot.engagements.append(
            (
                "calls",
                "k1",
                MON_10 + timedelta(hours=2),
                {"contacts": ["c2"], "deals": ["d1"]},
                False,
            )
        )
    return ScanDeps(
        gmail_factory=lambda email: (
            gmail.get(email) if isinstance(gmail, dict) else gmail
        ),
        slack_factory=lambda email: slack,
        hubspot_client=hubspot,
        judge=judge or ScriptedJudge(),
        hubspot_retry=_no_sleep_retry,
        **kw,
    )


def _seed_promise(db, *, status="suggested"):
    from rubik_core.database.promises import Promise

    p = Promise(
        id="p1",
        owner_email=AE,
        title="Send Jane the revised SOW",
        status=status,
        timezone="America/Chicago",
        source="heard_email",
        confidence=0.9,
        due_local=date(2026, 10, 3),  # 5 days past its date on NOW
        source_ref={"at": "2026-10-01T15:00:00", "link": "https://mail.google.com/x"},
        created_at=datetime(2026, 10, 1, 15, 0),
    )
    db.add(p)
    db.commit()
    return p


def _items(db, **filters):
    q = db.query(LoopItem)
    for k, v in filters.items():
        q = q.filter(getattr(LoopItem, k) == v)
    return q.all()


# ── A real morning ───────────────────────────────────────────────────────────


def test_a_run_files_every_kind_and_records_coverage(db):
    _seed_promise(db)
    judge = ScriptedJudge()
    report = run_scan(db, emails=[AE], now=NOW, deps=_deps(judge=judge))

    assert report["status"] == "completed"
    assert report["new"] == {"unlogged": 2, "open_loop": 1, "todo": 2}
    assert report["judge"]["calls"] == 1 and report["judge"]["declined"] == 1
    assert report["hubspot"] == "ok"
    assert report["failed"] == []

    keys = {(i.kind, i.source_key) for i in _items(db)}
    assert ("unlogged", "gmail:tU") in keys
    assert ("open_loop", "gmail:tO:mO1") in keys
    assert ("todo", "promise:p1") in keys
    assert ("todo", "hstask:t9") in keys
    assert not any(k[1].startswith("gmail:tL") for k in keys)  # logged
    assert not any(k[1].startswith("gmail:tF") for k in keys)  # judge said no
    assert not any(k[1].startswith("gmail:tN") for k in keys)  # newsletter

    [loop] = _items(db, kind="open_loop")
    assert loop.headline == "Asks about Pricing question"
    assert loop.counterparty_label == "Jane Doe" and loop.company_label == "Acme Supply"
    assert loop.detail["judge"] == "expects_reply"
    [slack_item] = _items(db, source="slack")
    assert slack_item.company_label == "Acme Supply"

    run = db.query(LoopScanRun).one()
    assert run.status == "completed" and run.finished_at is not None
    assert run.coverage[AE] == {
        "gmail": "ok",
        "slack": "ok",
        "hubspot": "ok",
        "ai": "ok",
        "threads_scanned": 5,
        "slack_messages_scanned": 1,
        "promises_scanned": 1,
        "tasks_scanned": 1,
        "new": 5,
        "closed": 0,
    }
    # The daily post ships off until an admin turns it on (Admin → Loose Ends).
    assert report["digest"] == {"status": "off"}
    assert report["scorecard"]["metrics_found"] == 0


def test_a_second_morning_closes_what_was_done(db):
    promise = _seed_promise(db)
    gmail = FakeGmail(_threads())
    hubspot = FakeHubSpot()
    run_scan(db, emails=[AE], now=NOW, deps=_deps(gmail=gmail, hubspot=hubspot))

    # Overnight: the AE answered Jane, a note was logged on Jane, the promise
    # was kept and the task completed.
    gmail.threads["tO"].append(
        _raw("mO2", "tO", AE, [JANE], NOW + timedelta(hours=2), "Re: Pricing question")
    )
    hubspot.engagements.append(
        (
            "notes",
            "n5",
            NOW + timedelta(hours=3),
            {"contacts": ["c1"], "deals": ["d1"]},
            False,
        )
    )
    promise.status = "kept"
    db.commit()
    hubspot.tasks["t9"]["hs_task_status"] = "COMPLETED"

    tomorrow = NOW + timedelta(days=1)
    report = run_scan(
        db, emails=[AE], now=tomorrow, deps=_deps(gmail=gmail, hubspot=hubspot)
    )

    assert report["closed"] == {
        "replied": 1,
        "logged_in_hubspot": 1,
        "promise_kept": 1,
        "task_completed": 1,
    }
    [loop] = _items(db, kind="open_loop")
    assert loop.status == "closed" and loop.close_reason == "replied"
    assert loop.close_evidence == {
        "type": "gmail_message",
        "id": "mO2",
        "at": "2026-10-08T14:10:00",
    }
    [unlogged] = _items(db, source_key="gmail:tU")
    assert unlogged.close_reason == "logged_in_hubspot"
    assert unlogged.close_evidence["id"] == "n5"
    # Evidence is ids and a time only — no subject, no snippet.
    assert set(unlogged.close_evidence) == {"type", "id", "at"}
    # The Slack item's company has nothing logged: still open.
    [slack_item] = _items(db, source="slack")
    assert slack_item.status == "open"


def test_a_dry_run_decides_everything_and_keeps_nothing(db):
    _seed_promise(db)
    judge = ScriptedJudge()
    report = run_scan(db, emails=[AE], now=NOW, dry_run=True, deps=_deps(judge=judge))
    assert report["status"] == "completed" and report["dry_run"] is True
    assert report["new"]["open_loop"] == 1
    assert report["coverage"][AE]["gmail"] == "ok"
    assert "digest" not in report and "scorecard" not in report
    assert judge.calls  # it really asked
    assert _items(db) == []
    assert db.query(LoopScanRun).count() == 0


def test_a_dry_run_ignores_the_kill_switch(db, monkeypatch):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    assert run_scan(db, emails=[AE], now=NOW, deps=_deps())["status"] == "disabled"
    report = run_scan(db, emails=[AE], now=NOW, dry_run=True, deps=_deps())
    assert report["status"] == "completed"


def test_eos_off_skips_scorecard_write_but_keeps_loose_ends_scan(db, monkeypatch):
    from rubik_core.control import repository
    from rubik_core.loop_closure import scorecard

    monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(
        scorecard,
        "write_weekly",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("EOS scorecard write")),
    )
    report = run_scan(
        db,
        emails=[AE],
        now=NOW,
        deps=_deps(),
        post_digest=False,
        write_scorecard=True,
    )
    assert report["status"] == "completed"
    assert "scorecard" not in report
    assert report["roster"] == 1


def test_a_failed_judge_creates_no_new_open_loops_but_still_rechecks(db):
    gmail = FakeGmail(_threads())
    run_scan(db, emails=[AE], now=NOW, deps=_deps(gmail=gmail))
    assert len(_items(db, kind="open_loop")) == 1

    gmail.threads["tO"].append(
        _raw("mO2", "tO", AE, [JANE], NOW + timedelta(hours=2), "Re: Pricing question")
    )
    gmail.threads["tO2"] = [
        _raw("mP1", "tO2", f"Jane Doe <{JANE}>", [AE], MON_10, "Another question")
    ]
    report = run_scan(
        db,
        emails=[AE],
        now=NOW + timedelta(days=1),
        deps=_deps(gmail=gmail, judge=ScriptedJudge(fail=True)),
    )
    assert report["judge"]["failed"] == [AE]
    assert report["coverage_problems"]["ai_unavailable"] == [AE]
    assert report["new"]["open_loop"] == 0
    assert not _items(db, source_key="gmail:tO2:mP1")
    # The existing loop was still re-checked, and closed.
    [loop] = _items(db, source_key="gmail:tO:mO1")
    assert loop.close_reason == "replied"
    run = db.query(LoopScanRun).order_by(LoopScanRun.started_at.desc()).first()
    assert run.coverage[AE]["ai"] == "unavailable"


def test_hubspot_down_closes_nothing_and_flags_nothing_unlogged(db):
    run_scan(db, emails=[AE], now=NOW, deps=_deps())
    [unlogged] = _items(db, source_key="gmail:tU")
    assert unlogged.status == "open"

    hubspot = FakeHubSpot()
    hubspot.fail = True
    gmail = FakeGmail(_threads())
    gmail.threads["tU2"] = [_raw("mV1", "tU2", AE, [JANE], MON_10, "Second thread")]
    report = run_scan(
        db,
        emails=[AE],
        now=NOW + timedelta(days=1),
        deps=_deps(gmail=gmail, hubspot=hubspot),
    )
    assert report["hubspot"] == "unavailable"
    assert report["closed"] == {}
    assert report["new"]["unlogged"] == 0
    db.refresh(unlogged)
    assert unlogged.status == "open"
    assert not _items(db, source_key="gmail:tU2")
    run = db.query(LoopScanRun).order_by(LoopScanRun.started_at.desc()).first()
    assert run.coverage[AE]["hubspot"] == "unavailable"


def test_an_unreadable_mailbox_is_a_coverage_state_not_an_all_clear(db):
    run_scan(db, emails=[AE], now=NOW, deps=_deps())
    report = run_scan(
        db,
        emails=[AE],
        now=NOW + timedelta(days=1),
        deps=_deps(gmail=FakeGmail({}, fail=True)),
    )
    assert report["coverage_problems"]["gmail_unavailable"] == [AE]
    [loop] = _items(db, kind="open_loop")
    assert loop.status == "open"  # nothing closed for want of a mailbox
    run = db.query(LoopScanRun).order_by(LoopScanRun.started_at.desc()).first()
    assert run.coverage[AE]["gmail"] == "unavailable"
    assert run.coverage[AE]["threads_scanned"] == 0


def test_slack_not_connected_is_named(db):
    report = run_scan(
        db, emails=[AE], now=NOW, deps=_deps(slack=FakeSlack(connected=False))
    )
    assert report["coverage_problems"]["slack_not_connected"] == [AE]
    assert not _items(db, source="slack")


def test_an_ae_with_no_hubspot_owner_skips_only_the_task_leg(db):
    report = run_scan(db, emails=[AE2], now=NOW, deps=_deps())
    assert report["coverage_problems"]["hubspot_no_owner"] == [AE2]
    run = db.query(LoopScanRun).one()
    assert run.coverage[AE2]["hubspot"] == "no_owner"
    assert run.coverage[AE2]["tasks_scanned"] == 0


def test_the_run_lock(db):
    db.add(
        LoopScanRun(
            started_at=(NOW - timedelta(minutes=10)).replace(tzinfo=None),
            status="running",
            trigger="scheduler",
        )
    )
    db.commit()
    report = run_scan(db, emails=[AE], now=NOW, deps=_deps())
    assert report["status"] == "locked" and report["blocking_run_id"]
    assert _items(db) == []


def test_a_stale_lock_is_released_and_marked_failed(db):
    stale = LoopScanRun(
        started_at=(NOW - timedelta(hours=3)).replace(tzinfo=None),
        status="running",
        trigger="scheduler",
    )
    db.add(stale)
    db.commit()
    report = run_scan(db, emails=[AE], now=NOW, deps=_deps())
    assert report["status"] == "completed"
    db.refresh(stale)
    assert stale.status == "failed"


def test_one_ae_failing_never_costs_the_others(db):
    hubspot = FakeHubSpot()
    hubspot.owners["o2"] = {"email": AE2, "name": "Sam Second"}
    judge = ScriptedJudge(raise_for="Rita Rep")
    mailboxes = {
        AE: FakeGmail(_threads()),
        AE2: FakeGmail(
            {"tS": [_raw("mS1", "tS", f"Jane Doe <{JANE}>", [AE2], MON_10, "Hello")]}
        ),
    }
    report = run_scan(
        db,
        emails=[AE, AE2],
        now=NOW,
        deps=_deps(gmail=mailboxes, hubspot=hubspot, judge=judge),
    )
    assert report["status"] == "completed"
    assert report["failed"] == [AE]
    assert report["scanned"] == 1
    assert not _items(db, owner_email=AE)
    assert _items(db, owner_email=AE2)  # the second AE's morning happened
    # The crashed AE's coverage claims nothing was read — no "0 email threads"
    # — and the page says the scan did not finish, not that HubSpot was down.
    from rubik_core.loop_closure import constants

    run = db.query(LoopScanRun).one()
    assert run.coverage[AE] == {
        "gmail": "unavailable",
        "slack": "unavailable",
        "hubspot": "unavailable",
        "ai": "unavailable",
        "scan": "failed",
    }
    [note] = constants.coverage_notes(run.coverage[AE])
    assert "could not finish your scan" in note


def test_promises_are_heard_only_for_aes_the_batch_does_not_cover(db, monkeypatch):
    monkeypatch.setattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", True)
    heard: list[str] = []
    deps = _deps(
        hear_promises=heard.append,
        batch_roster=lambda: [AE],
    )
    hubspot = deps.hubspot_client
    hubspot.owners["o2"] = {"email": AE2, "name": "Sam Second"}
    report = run_scan(db, emails=[AE, AE2], now=NOW, deps=deps)
    assert heard == [AE2] and report["heard_for"] == 1
    # Never in a dry run: hearing writes the promises ledger.
    heard.clear()
    run_scan(db, emails=[AE, AE2], now=NOW, dry_run=True, deps=deps)
    assert heard == []


def test_a_dismissed_item_stays_dismissed_on_the_next_run(db):
    from rubik_core.loop_closure import store

    run_scan(db, emails=[AE], now=NOW, deps=_deps())
    [loop] = _items(db, kind="open_loop")
    store.dismiss(db, loop, reason="handled_elsewhere", actor=AE, now=NOW)
    db.commit()
    report = run_scan(db, emails=[AE], now=NOW + timedelta(days=1), deps=_deps())
    db.refresh(loop)
    assert loop.status == "dismissed"
    assert report["new"]["open_loop"] == 0


def test_an_old_thread_is_reread_to_see_the_reply(db):
    gmail = FakeGmail(_threads())
    run_scan(db, emails=[AE], now=NOW, deps=_deps(gmail=gmail))
    gmail.threads["tO"].append(
        _raw("mO2", "tO", AE, [JANE], NOW + timedelta(hours=2), "Re: Pricing question")
    )
    gmail.hidden.add("tO")  # out of the 14-day lanes now
    run_scan(db, emails=[AE], now=NOW + timedelta(days=1), deps=_deps(gmail=gmail))
    [loop] = _items(db, kind="open_loop")
    assert loop.close_reason == "replied"


def test_a_deleted_thread_leaves_its_loop_undecided_not_the_mailbox(db):
    gmail = FakeGmail(_threads())
    run_scan(db, emails=[AE], now=NOW, deps=_deps(gmail=gmail))
    gmail.hidden.add("tO")
    gmail.gone.add("tO")
    report = run_scan(
        db, emails=[AE], now=NOW + timedelta(days=1), deps=_deps(gmail=gmail)
    )
    assert report["coverage_problems"]["gmail_unavailable"] == []
    [loop] = _items(db, kind="open_loop")
    assert loop.status == "open"


# ── Review fixes ─────────────────────────────────────────────────────────────


def _post_turned_on(db, channel_id="C0LOOSE01"):
    """What Admin → Loose Ends leaves behind once an admin switches the post on."""
    from rubik_core.database.models import WorkspaceSetting
    from rubik_core.loop_closure.digest_settings import SETTINGS_KEY

    db.add(
        WorkspaceSetting(
            key=SETTINGS_KEY,
            value={
                "enabled": True,
                "channel_id": channel_id,
                "channel_name": "#sales",
                "bot_in_channel": True,
            },
        )
    )
    db.commit()


class FakeBot:
    def __init__(self):
        self.is_available = True
        self.posts: list[tuple[str, str]] = []

    def post_message(self, channel, *, text=None, blocks=None, thread_ts=None):
        self.posts.append((channel, text))
        return {"ok": True, "ts": "1.0"}


def test_an_unsent_draft_is_neither_a_reply_nor_a_sent_email(db):
    gmail = FakeGmail(_threads())
    run_scan(db, emails=[AE], now=NOW, deps=_deps(gmail=gmail))
    [loop] = _items(db, kind="open_loop")
    # The AE starts a reply; Gmail autosaves it as a DRAFT (From: the AE) and
    # it is never sent. And an unsent draft to a contact, in its own thread.
    gmail.threads["tO"].append(
        _raw(
            "mO2",
            "tO",
            AE,
            [JANE],
            NOW + timedelta(hours=2),
            "Re: Pricing question",
            labels=("DRAFT",),
        )
    )
    gmail.threads["tD"] = [
        _raw("mD1", "tD", AE, [CARL], MON_10, "Only a draft", labels=("DRAFT",))
    ]
    report = run_scan(
        db, emails=[AE], now=NOW + timedelta(days=1), deps=_deps(gmail=gmail)
    )
    db.refresh(loop)
    assert loop.status == "open"  # still waiting: nothing was sent
    assert "replied" not in report["closed"]
    assert not _items(db, source_key="gmail:tD")  # never "Not in HubSpot"


class PagedGmail(FakeGmail):
    """A mailbox that pages ``threads.list`` like Gmail does."""

    def __init__(self, threads):
        super().__init__(threads)
        self.pages: list[tuple[str, int, str | None]] = []

    def list_thread_page(self, query="", max_results=100, page_token=None):
        self.pages.append((query, max_results, page_token))
        ids = [t for t in self.threads if t not in self.hidden]
        start = int(page_token or 0)
        end = start + max_results
        return ids[start:end], (str(end) if end < len(ids) else None)


def test_a_mailbox_with_more_threads_than_one_morning_reads_is_partial(db, monkeypatch):
    from rubik_core.loop_closure import gmail_reader, views

    monkeypatch.setattr(gmail_reader, "MAX_THREADS_PER_LANE", 3)
    gmail = PagedGmail(_threads())  # five threads in the window
    report = run_scan(db, emails=[AE], now=NOW, deps=_deps(gmail=gmail))
    assert report["coverage_problems"]["gmail_partial"] == [AE]
    assert report["coverage_problems"]["gmail_unavailable"] == []
    run = db.query(LoopScanRun).one()
    assert run.coverage[AE]["gmail"] == "partial"
    assert run.coverage[AE]["threads_scanned"] == 3
    # What was read still counts, and the page says what was not.
    assert _items(db, kind="unlogged")
    notes = views.mine_view(db, AE, now=NOW)["notes"]
    assert any("oldest threads were not checked" in n for n in notes)


def test_paging_reads_every_page_up_to_the_cap_and_no_further():
    from rubik_core.loop_closure import gmail_reader

    gmail = PagedGmail({f"t{n}": [] for n in range(7)})
    gmail.threads = {
        tid: [_raw(f"m{tid}", tid, f"Jane <{JANE}>", [AE], MON_10, "Hi")]
        for tid in gmail.threads
    }
    threads, capped = gmail_reader.gather_all(gmail, lookback_days=14, max_per_lane=10)
    assert len(threads) == 7 and capped == []
    _, capped = gmail_reader.gather_all(gmail, lookback_days=14, max_per_lane=4)
    assert len(capped) == 2  # both lanes had more than 4
    assert all(n <= 500 for _q, n, _t in gmail.pages)


def test_a_declined_candidate_is_not_asked_about_again(db):
    gmail = FakeGmail(_threads())
    judge = ScriptedJudge()
    run_scan(db, emails=[AE], now=NOW, deps=_deps(gmail=gmail, judge=judge))
    assert any("Carl" in e.sender_name for e in judge.calls[0][0])

    again = ScriptedJudge()
    report = run_scan(
        db,
        emails=[AE],
        now=NOW + timedelta(days=1),
        deps=_deps(gmail=gmail, judge=again),
    )
    # Carl's "Thanks!" was declined yesterday: remembered, never re-sent, so
    # it cannot keep a newer email out of the call's 40 slots.
    assert report["judge"]["remembered"] == 1
    assert report["judge"]["calls"] == 0 and again.calls == []

    # A NEW message from Carl is a new candidate and is asked about afresh.
    wed = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)
    gmail.threads["tF"].append(
        _raw("mF2", "tF", f"Carl Coe <{CARL}>", [AE], wed, "Re: Thanks!")
    )
    later = ScriptedJudge()
    run_scan(
        db,
        emails=[AE],
        now=datetime(2026, 10, 12, 12, 10, tzinfo=UTC),
        deps=_deps(gmail=gmail, judge=later),
    )
    assert any("Carl" in e.sender_name for emails, _ in later.calls for e in emails)


def test_candidates_over_the_judge_cap_wait_and_are_counted(db, monkeypatch):
    from rubik_core.loop_closure import views

    monkeypatch.setattr(judge_mod, "MAX_EMAILS", 1)
    report = run_scan(db, emails=[AE], now=NOW, deps=_deps())
    # Jane's and Carl's unanswered emails are both fresh; one fits the call.
    assert report["judge"]["over_cap"] == 1
    run = db.query(LoopScanRun).one()
    assert run.coverage[AE]["judge_deferred"] == 1
    notes = views.mine_view(db, AE, now=NOW)["notes"]
    assert any("checked on the next scans" in n for n in notes)


def test_a_snoozed_promise_leaves_as_rescheduled_and_comes_back_when_late_again(db):
    from rubik_core.loop_closure import views

    promise = _seed_promise(db)
    run_scan(db, emails=[AE], now=NOW, deps=_deps())
    [todo] = _items(db, source_key="promise:p1")
    assert todo.status == "open"

    promise.due_local = date(2026, 10, 20)  # snoozed to a later date
    db.commit()
    day2 = NOW + timedelta(days=1)
    report = run_scan(db, emails=[AE], now=day2, deps=_deps())
    db.refresh(todo)
    assert todo.status == "closed" and todo.close_reason == "rescheduled"
    assert todo.close_evidence["due"] == "2026-10-20"
    assert report["closed"].get("rescheduled") == 1
    # Listed in the reason breakdown, never counted as fixed.
    health = views.health(db, [AE], now=day2)
    assert health["closed_by_reason"].get("rescheduled") == 1
    assert health["closed"] == 0 and health["median_days_to_close"] is None
    assert views.mine_view(db, AE, now=day2)["totals"]["closed_this_week"] == 0

    # The new date passes too: the same row comes back, aged from it.
    late = datetime(2026, 10, 25, 12, 10, tzinfo=UTC)
    run_scan(db, emails=[AE], now=late, deps=_deps())
    db.refresh(todo)
    assert todo.status == "open" and todo.reopened_count == 1
    assert todo.opened_at == datetime(2026, 10, 20, 5, 0)  # local midnight, UTC


def test_a_task_moved_later_leaves_as_rescheduled(db):
    hubspot = FakeHubSpot()
    run_scan(db, emails=[AE], now=NOW, deps=_deps(hubspot=hubspot))
    [task] = _items(db, source_key="hstask:t9")
    hubspot.tasks["t9"]["hs_timestamp"] = _iso(NOW + timedelta(days=5))
    report = run_scan(
        db, emails=[AE], now=NOW + timedelta(days=1), deps=_deps(hubspot=hubspot)
    )
    db.refresh(task)
    assert task.close_reason == "rescheduled"
    assert report["closed"].get("rescheduled") == 1


def test_a_task_handed_to_someone_else_leaves_the_original_aes_list(db):
    hubspot = FakeHubSpot()
    hubspot.owners["o2"] = {"email": AE2, "name": "Sam Second"}
    run_scan(db, emails=[AE], now=NOW, deps=_deps(hubspot=hubspot))
    [task] = _items(db, source_key="hstask:t9")
    hubspot.tasks["t9"]["hubspot_owner_id"] = "o2"
    run_scan(db, emails=[AE], now=NOW + timedelta(days=1), deps=_deps(hubspot=hubspot))
    db.refresh(task)
    assert task.close_reason == "task_gone"
    assert task.close_evidence["reassigned"] is True


@pytest.mark.parametrize(
    "slack",
    [lambda: FakeSlack(uid=""), lambda: FakeSlack(scope_error=True)],
    ids=["no-member-id", "missing-search-scope"],
)
def test_slack_that_cannot_be_searched_is_unavailable_not_empty(db, slack):
    fake = slack()
    report = run_scan(db, emails=[AE], now=NOW, deps=_deps(slack=fake))
    assert report["coverage_problems"]["slack_unavailable"] == [AE]
    run = db.query(LoopScanRun).one()
    assert run.coverage[AE]["slack"] == "unavailable"
    assert all(fake.raise_on_scope)  # the scan always asks to hear the error


def test_a_named_address_off_the_roster_is_never_read(db):
    from rubik_core.database.models import User

    db.add(
        User(
            email="ceo@acme.example",
            id="ceo",
            name="Chief",
            department="Exec",
            is_exec=True,
        )
    )
    db.commit()
    opened: list[str] = []
    deps = _deps()
    real = deps.gmail_factory
    deps.gmail_factory = lambda email: opened.append(email) or real(email)
    report = run_scan(
        db,
        emails=[AE, "ceo@acme.example"],
        now=NOW,
        dry_run=True,
        deps=deps,
    )
    assert report["not_on_roster"] == ["ceo@acme.example"]
    assert report["roster"] == 1 and opened == [AE]


def test_an_unchecked_ae_is_never_a_scorecard_zero_or_counted_as_scanned(
    db, monkeypatch
):
    from rubik_core.database.eos import Person, ScorecardEntry, ScorecardMetric

    metric_name = "Loose ends over 7 days"
    _post_turned_on(db)
    from rubik_core.company.operations import OperationsConfig

    monkeypatch.setattr(
        "rubik_core.company.operations.get_operations_config",
        lambda: OperationsConfig(loop_closure_scorecard_metric=metric_name),
    )
    rita = Person(first_name="Rita", last_name="Rep", email=AE)
    db.add(rita)
    db.commit()
    metric = ScorecardMetric(
        owner_id=rita.id, department_name="Sales", name=metric_name, is_exec=False
    )
    db.add(metric)
    db.commit()
    hubspot = FakeHubSpot()
    hubspot.owners["o2"] = {"email": AE2, "name": "Sam Second"}
    mailboxes = {
        AE: FakeGmail({}, fail=True),  # Rita's mailbox cannot be read
        AE2: FakeGmail(
            {"tS": [_raw("mS1", "tS", f"Jane Doe <{JANE}>", [AE2], MON_10, "Hello")]}
        ),
    }
    bot = FakeBot()
    report = run_scan(
        db,
        now=NOW,
        deps=_deps(
            gmail=mailboxes,
            hubspot=hubspot,
            bot=bot,
            deactivated_lookup=lambda emails: set(),
        ),
    )
    assert report["status"] == "completed" and report["not_checked"] == 1
    assert report["scorecard"]["skipped_not_checked"] == 1
    assert (
        db.query(ScorecardEntry).filter(ScorecardEntry.metric_id == metric.id).count()
        == 0
    )
    [(_channel, text)] = bot.posts
    assert "across 1 of 1 people scanned" in text
    assert "Not checked this morning (1 of 2 on the roster" in text
    assert "Rita Rep" in text


def test_a_run_where_every_ae_failed_is_a_failed_run(db, monkeypatch):
    _post_turned_on(db)
    bot = FakeBot()
    report = run_scan(
        db,
        emails=[AE],
        now=NOW,
        deps=_deps(judge=ScriptedJudge(raise_for="Rita Rep"), bot=bot),
    )
    assert report["status"] == "failed" and report["error"] == "every_ae_failed"
    assert "digest" not in report and "scorecard" not in report
    assert bot.posts == []
    run = db.query(LoopScanRun).one()
    assert run.status == "failed" and run.coverage[AE]["scan"] == "failed"


def test_a_retry_of_the_same_execution_takes_over_its_orphaned_lock(db, monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "loop-closure-abc")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    orphan = LoopScanRun(
        started_at=(NOW - timedelta(minutes=3)).replace(tzinfo=None),
        status="running",
        trigger="scheduler",
        report={"execution": "loop-closure-abc"},
    )
    db.add(orphan)
    db.commit()
    report = run_scan(db, emails=[AE], now=NOW, deps=_deps())
    assert report["status"] == "completed"
    assert report["execution"] == "loop-closure-abc"
    db.refresh(orphan)
    assert orphan.status == "failed" and orphan.report["reason"] == "task_retried"


@pytest.mark.parametrize(
    "execution,attempt",
    [("loop-closure-other", "1"), ("loop-closure-abc", "0")],
    ids=["another-execution", "first-attempt"],
)
def test_a_live_lock_still_blocks(db, monkeypatch, execution, attempt):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", execution)
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", attempt)
    db.add(
        LoopScanRun(
            started_at=(NOW - timedelta(minutes=3)).replace(tzinfo=None),
            status="running",
            trigger="scheduler",
            report={"execution": "loop-closure-abc"},
        )
    )
    db.commit()
    assert run_scan(db, emails=[AE], now=NOW, deps=_deps())["status"] == "locked"


def test_the_engagement_window_keeps_the_full_day_of_slack():
    from rubik_core.loop_closure.scan import _window_start

    limits = {"lookback_days": 14, "max_age_days": 60}
    oldest = NOW - timedelta(days=20, hours=14)  # 20.6 days ago
    assert _window_start(NOW, oldest, limits) == oldest - timedelta(days=1)
    # Nothing older than the lookback: lookback + 1 day.
    assert _window_start(NOW, NOW, limits) == NOW - timedelta(days=15)
    # Never past the max age (+1 day).
    ancient = NOW - timedelta(days=90)
    assert _window_start(NOW, ancient, limits) == NOW - timedelta(days=61)
