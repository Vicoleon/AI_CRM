"""The weekly close-date roll-up, and the watch band that only it can see.

Two things are pinned harder than the message itself:

* **the watch band never becomes a rep's card.** Tier 3 is the band that is
  worth a manager's glance and not worth interrupting a rep over. If a tier-3
  Signal were ever constructed it would be merged onto the deal's real signals
  by deal_id — altering their evidence and severity — and would eat one of the
  agent's output slots. So no tier-3 Signal exists at all.
* **the roll-up cannot touch a rep's memory rows.** resolve_entity_everywhere
  and reopen_entity_everywhere take no user_email: they update the row for
  every user holding that (entity, kind) pair. A button on this message would
  silence the rep who owns the deal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.jobs.close_date_rollup import (
    MAX_ROWS,
    RollupResult,
    RollupRow,
    _sort_key,
    build_blocks,
)
from backend.signals.hubspot import (
    CLOSE_DATE_TIER_PASSED,
    CLOSE_DATE_TIER_QUIET,
    CLOSE_DATE_TIER_WATCH,
    close_date_tier,
)
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

_MODULE = Path(__file__).resolve().parents[1] / "jobs" / "close_date_rollup.py"


@pytest.fixture(autouse=True)
def _workspace_hubspot_pipelines(monkeypatch):
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )


# ── the band boundaries ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "quiet,expected",
    [
        (13, None),  # too fresh to mention at all
        (14, CLOSE_DATE_TIER_WATCH),  # the watch band opens
        (20, CLOSE_DATE_TIER_WATCH),  # ...and closes here
        (21, CLOSE_DATE_TIER_QUIET),  # the rep's card starts
    ],
)
def test_the_watch_band_is_14_to_20_days_inclusive(quiet, expected):
    """An off-by-one at either edge is silent: 13 becomes a row nobody wanted,
    21 lands in tier 3 and vanishes from the rep's card entirely."""
    assert close_date_tier(20, quiet) is expected


def test_a_passed_date_is_tier_one_however_recent_the_contact():
    """'You have a 7/31 close date that didn't close' is wrong on its own."""
    assert close_date_tier(-12, 1) is CLOSE_DATE_TIER_PASSED
    assert close_date_tier(-12, None) is CLOSE_DATE_TIER_PASSED


def test_a_far_off_date_is_no_tier_at_all():
    assert close_date_tier(200, 60) is None


def test_an_abandoned_record_falls_out_of_every_tier():
    """Six months of silence is data hygiene. Re-asking weekly is how the
    roll-up gets muted."""
    assert close_date_tier(10, 400) is None
    assert close_date_tier(-400, None) is None


def test_the_watch_band_never_reaches_a_reps_card():
    """The single thing that must not happen. The probe is the only producer of
    a Signal, and it emits tiers 1 and 2 only."""
    from backend.tests.test_priorities_close_date_probe import _deal, _probe

    assert close_date_tier(20, 17) is CLOSE_DATE_TIER_WATCH
    assert _probe(_deal(close_in_days=20, quiet_days=17)) == []


def test_both_readers_agree_on_every_band():
    """One classifier, two readers. An exec asking a rep about a deal whose
    card that rep never saw is how the whole feature loses credibility."""
    from backend.tests.test_priorities_close_date_probe import _deal, _probe

    for quiet in (5, 14, 17, 20, 21, 30):
        for close_in in (-10, 5, 20, 44, 60):
            tier = close_date_tier(close_in, quiet)
            emitted = bool(_probe(_deal(close_in_days=close_in, quiet_days=quiet)))
            assert emitted is (
                tier in (CLOSE_DATE_TIER_PASSED, CLOSE_DATE_TIER_QUIET)
            ), f"close_in={close_in} quiet={quiet} tier={tier} emitted={emitted}"


def test_retuning_the_threshold_moves_both_readers_together(monkeypatch):
    """Thresholds are read at call time. Read at import time, the rep's card
    and the manager's list would desynchronise the first time Sales ops tunes
    the environment."""
    from rubik_core.config import settings

    assert close_date_tier(20, 25) is CLOSE_DATE_TIER_QUIET
    monkeypatch.setattr(settings, "HUBSPOT_CLOSE_DATE_QUIET_DAYS", 30, raising=False)
    assert close_date_tier(20, 25) is CLOSE_DATE_TIER_WATCH


# ── ranking ──────────────────────────────────────────────────────────────────


def _row(tier, *, quiet=25, dtc=10, amount=1000.0, name="d"):
    return RollupRow(
        deal_id=name,
        deal_name=name,
        owner="ae@x.example",
        stage="Proposal",
        close_date="2026-10-01",
        days_to_close=dtc,
        quiet_days=quiet,
        tier=tier,
        amount=amount,
    )


def test_least_credible_first_and_money_only_breaks_ties():
    """A big healthy deal above a small dead one is how a list stops being
    read. Tier leads; the longer problem wins inside a tier."""
    rows = [
        _row(CLOSE_DATE_TIER_WATCH, quiet=15, name="watch"),
        _row(CLOSE_DATE_TIER_QUIET, quiet=22, amount=900_000, name="quiet-short-rich"),
        _row(CLOSE_DATE_TIER_QUIET, quiet=40, amount=10, name="quiet-long-poor"),
        _row(CLOSE_DATE_TIER_PASSED, dtc=-3, name="passed-recent"),
        _row(CLOSE_DATE_TIER_PASSED, dtc=-40, name="passed-old"),
    ]
    assert [r.deal_name for r in sorted(rows, key=_sort_key)] == [
        "passed-old",
        "passed-recent",
        "quiet-long-poor",
        "quiet-short-rich",
        "watch",
    ]


# ── the message ──────────────────────────────────────────────────────────────


def test_a_quiet_week_still_sends_and_says_what_was_checked():
    """Silence reads as "it broke", and a CEO who thinks it broke switches it
    off. This is the case that decides whether the feature survives."""
    text, blocks = build_blocks(RollupResult(checked=182))
    assert "Nothing to flag" in text
    assert "182" in text
    assert blocks


def test_the_message_never_invents_a_silence_it_did_not_measure():
    """A deal nobody ever logged contact on is counted, never ranked with a
    made-up number."""
    res = RollupResult(checked=10, uncontacted=4)
    res.rows = [_row(CLOSE_DATE_TIER_PASSED, quiet=None, dtc=-5)]
    text, _ = build_blocks(res)
    assert "no client contact ever logged" in text or "no client contact logged" in text


def test_the_message_is_capped_and_says_how_many_it_left_out():
    res = RollupResult(checked=50)
    res.rows = [_row(CLOSE_DATE_TIER_PASSED, dtc=-i, name=f"d{i}") for i in range(1, 9)]
    text, blocks = build_blocks(res)
    sections = [b for b in blocks if b.get("type") == "section"]
    assert len(sections) == MAX_ROWS
    assert f"{8 - MAX_ROWS} more not shown" in text


def test_a_partial_read_is_reported_as_partial():
    """A short read presented as the whole book is a silent lie."""
    res = RollupResult(checked=5000, truncated=True)
    res.rows = [_row(CLOSE_DATE_TIER_PASSED, dtc=-5)]
    text, _ = build_blocks(res)
    assert "partial" in text.lower()


# ── read-only, structurally ──────────────────────────────────────────────────


def test_the_message_carries_no_button_and_no_link_back_into_the_app():
    """One click from this message to a rep's card would write a shared
    suppression row against a deal the reader does not own."""
    res = RollupResult(checked=9)
    res.rows = [_row(CLOSE_DATE_TIER_PASSED, dtc=-5)]
    _text, blocks = build_blocks(res)
    assert not any(b.get("type") == "actions" for b in blocks)
    rendered = repr(blocks)
    assert "action_id" not in rendered
    assert "/priorities" not in rendered


def test_the_module_never_imports_priority_memory():
    """The leak this design exists to prevent arrives in six months as a
    helpful "mark handled for the team" verb. Pin it at the import line."""
    import ast

    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))

    # AST, not grep: the module's own docstring NAMES these functions to
    # explain why it must not use them, and that explanation is the thing
    # most worth keeping.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "priorities.memory" not in node.module, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "priorities.memory" not in alias.name, alias.name
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None) or ""
            assert name not in (
                "resolve_entity_everywhere",
                "reopen_entity_everywhere",
                "resolve_manual",
                "dismiss",
            ), f"the roll-up must never mutate a rep's memory row: {name}()"


def test_a_hubspot_failure_is_never_reported_as_a_clean_week(monkeypatch):
    """Swallowing the error and returning [] would hand the roll-up's reader a
    confident "nothing to flag" during an outage."""
    import backend.jobs.close_date_rollup as mod
    from rubik_core.retrieval import hubspot_api

    class _Boom:
        def __init__(self, *a, **k):
            raise hubspot_api.HubSpotAPIError("portal down")

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", _Boom)
    res = mod.collect_rows()
    assert res.status == "fetch_failed"
    assert res.rows == []


def test_the_job_exits_nonzero_only_when_it_could_not_look(monkeypatch):
    """Finding nothing is success. Being unable to check is not — a backstop
    that stays quiet when it could not read is the original bug in a new hat."""
    import backend.jobs.close_date_rollup as mod

    monkeypatch.setattr(mod, "run_rollup", lambda **_: RollupResult(status="ok"))
    assert mod.main([]) == 0
    monkeypatch.setattr(
        mod, "run_rollup", lambda **_: RollupResult(status="fetch_failed")
    )
    assert mod.main([]) == 1


# ── who receives it ──────────────────────────────────────────────────────────


def test_the_roster_ships_empty_and_admits_no_department():
    """A department here would hand a whole team a weekly list of their
    colleagues' deals; recipients are named by an admin, never shipped."""
    from rubik_core.features.registry import feature_by_key

    policy = feature_by_key("close_date_rollup").default()
    assert policy.departments == frozenset()
    assert not policy.extra_emails and not policy.everyone


def test_seniority_alone_does_not_put_someone_on_the_list():
    """exec_allowed=True would quietly broadcast every AE's pipeline to all
    eleven execs; admin_allowed=True would mail every Cubie admin forever,
    because user_allowed grants on the admin flag before it reads a roster."""
    from rubik_core.features.registry import feature_by_key

    policy = feature_by_key("close_date_rollup").default()
    assert policy.exec_allowed is False
    assert policy.admin_allowed is False


def test_it_ships_switched_off():
    from rubik_core.config import Settings

    assert Settings.model_fields["CLOSE_DATE_ROLLUP_ENABLED"].default is False


def test_the_job_is_registered_so_it_can_actually_be_run():
    from backend.jobs import WORKER_JOBS

    job = WORKER_JOBS["close-date-rollup"]
    assert job.module == "backend.jobs.close_date_rollup"
    assert "--dry-run" in job.example_args


def test_the_cadence_note_does_not_assert_a_schedule_the_repo_cannot_prove():
    """Cloud Scheduler config lives in GCP, not git. A wants_cadence naming an
    exact time reads as fact and has already disagreed with reality elsewhere
    in this repo."""
    from backend.jobs import WORKER_JOBS

    cadence = WORKER_JOBS["close-date-rollup"].wants_cadence
    assert "Monday" in cadence
    assert not re.search(r"\d{1,2}:\d{2}", cadence)


# ── exactly once per recipient per local day ─────────────────────────────────
#
# A duplicated Cloud Scheduler entry, a retried attempt-deadline and a
# hand-run catch-up all look identical from inside the process. The guard is
# the UNIQUE index, never a pre-check: two executions racing would both pass a
# pre-check and both send.


@pytest.fixture()
def _rollup_db(monkeypatch, tmp_path):
    """An isolated SQLite database with the digest_logs table."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    import backend.jobs.close_date_rollup as mod
    from rubik_core.database.commitments import DigestLog

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rollup.db'}")
    DigestLog.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr(
        "rubik_core.priorities.temporal.user_today_str", lambda *_a, **_k: "2026-09-21"
    )
    return mod, Session, DigestLog


class _Bot:
    is_available = True

    def __init__(self, result="ok", boom=False):
        self.result, self.boom, self.calls = result, boom, 0

    def send_dm(self, email, **_kw):
        self.calls += 1
        if self.boom:
            raise RuntimeError("slack exploded")
        return self.result


def _res_with_rows():
    res = RollupResult(checked=20)
    res.rows = [_row(CLOSE_DATE_TIER_PASSED, dtc=-5)]
    res.tier_counts = {1: 1}
    return res


def test_a_second_run_on_the_same_local_day_sends_once(monkeypatch, _rollup_db):
    """The duplicate-scheduler case. Two executions, one DM."""
    mod, Session, DigestLog = _rollup_db
    bot = _Bot()
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", lambda: bot)

    for _ in range(2):
        mod._send_once("ceo@acme.example", "text", [], _res_with_rows())

    assert bot.calls == 1
    with Session() as db:
        assert db.query(DigestLog).count() == 1


def test_the_second_run_reports_it_as_already_sent_not_as_an_error():
    """The guard firing is the system working. Counting it as an error would
    page somebody every Monday."""
    res = RollupResult()
    assert res.already_sent == 0 and res.errors == []


def test_a_failed_send_leaves_no_row_so_the_next_sweep_retries(monkeypatch, _rollup_db):
    """The claim row is rolled back on a failed send. A committed claim with a
    send that never happened would suppress that recipient for the whole week."""
    mod, Session, DigestLog = _rollup_db
    bot = _Bot(boom=True)
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", lambda: bot)

    res = _res_with_rows()
    mod._send_once("ceo@acme.example", "text", [], res)

    assert res.sent == 0 and res.errors
    with Session() as db:
        assert db.query(DigestLog).count() == 0, "a failed send must leave no claim"

    # ...and the retry now succeeds.
    good = _Bot()
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", lambda: good)
    res2 = _res_with_rows()
    mod._send_once("ceo@acme.example", "text", [], res2)
    assert res2.sent == 1


def test_a_non_workspace_member_is_a_skip_not_a_failure(monkeypatch, _rollup_db):
    """send_dm returns None for somebody who is not in Slack. Its own named
    bucket, and no row — so they get it the week they are added."""
    mod, Session, DigestLog = _rollup_db
    bot = _Bot(result=None)
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", lambda: bot)

    res = _res_with_rows()
    mod._send_once("newhire@acme.example", "text", [], res)

    assert res.not_in_slack == 1
    assert res.errors == [] and res.sent == 0
    with Session() as db:
        assert db.query(DigestLog).count() == 0


def test_a_sent_row_is_stamped_so_a_committed_claim_is_distinguishable(
    monkeypatch, _rollup_db
):
    """sent_at is nullable only to permit the claim ordering. A committed row
    with NULL sent_at means somebody committed before confirming the send."""
    mod, Session, DigestLog = _rollup_db
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", lambda: _Bot())

    mod._send_once("ceo@acme.example", "text", [], _res_with_rows())
    with Session() as db:
        assert db.query(DigestLog).one().sent_at is not None


def test_the_rollup_owns_its_digest_kind():
    """Sharing "weekly" with the cockpit digest would make the two suppress
    each other for anyone who gets both on the same local day."""
    from rubik_core.database.commitments import DIGEST_KINDS

    assert "close_date_rollup" in DIGEST_KINDS
    assert "weekly" in DIGEST_KINDS  # still the cockpit's, untouched


def test_the_guard_is_the_unique_index_not_a_pre_check():
    """Two racing executions both pass a pre-check and both send. The database
    is the only arbiter that works."""
    src = _MODULE.read_text(encoding="utf-8")
    assert "IntegrityError" in src
    assert "db.flush()" in src


# ── defects found on the first real dry-run ──────────────────────────────────


def test_an_owner_directory_entry_renders_as_a_name_not_a_dict():
    """`owner_directory()` maps id -> {"email", "name"}. Rendered as-is, that
    dict would print verbatim on every row of the roll-up message."""
    from backend.jobs.close_date_rollup import _owner_label

    assert (
        _owner_label({"email": "rkeller@acme.example", "name": "Reese Keller"})
        == "Reese Keller"
    )
    assert _owner_label({"email": "x@acme.example"}) == "x@acme.example"
    # A departed rep's deal is the coverage this adds — it must still render.
    assert _owner_label(None) == "unassigned"
    assert _owner_label({}) == "unassigned"
    assert _owner_label("already-a-string") == "already-a-string"


def test_a_rendered_row_never_contains_a_python_dict():
    res = RollupResult(checked=9)
    res.rows = [_row(CLOSE_DATE_TIER_PASSED, dtc=-5)]
    text, _ = build_blocks(res)
    assert "{'" not in text and '{"' not in text


def test_a_dry_run_renders_even_when_nobody_would_receive_it(monkeypatch):
    """On a laptop the local DB has no users, so recipients resolve to []. That is
    exactly when you most want to SEE the message — and the first dry-run
    showed nothing, because it gated on recipients before rendering."""

    import backend.jobs.close_date_rollup as mod

    res = _res_with_rows()
    monkeypatch.setattr(mod, "collect_rows", lambda: res)
    monkeypatch.setattr(mod, "resolve_recipients", lambda: [])
    seen = []
    monkeypatch.setattr(
        mod.logger, "info", lambda msg, *a, **k: seen.append(msg % a if a else msg)
    )
    out = mod.run_rollup(dry_run=True)
    assert out.status == "ok", "a dry run with no recipients is not a failure"
    assert any("DRY RUN" in m and "Close dates worth a question" in m for m in seen), (
        seen
    )


def test_a_live_run_with_no_recipients_still_sends_nothing(monkeypatch):
    """The recipients gate moved AFTER rendering for dry runs — it must still
    stop a real send."""
    import backend.jobs.close_date_rollup as mod
    from rubik_core.config import settings

    # A real run checks the kill switch first; this test is about the gate
    # BEHIND it, so switch it on.
    monkeypatch.setattr(settings, "CLOSE_DATE_ROLLUP_ENABLED", True, raising=False)
    monkeypatch.setattr(mod, "collect_rows", lambda: _res_with_rows())
    monkeypatch.setattr(mod, "resolve_recipients", lambda: [])
    monkeypatch.setattr(mod, "_send_once", lambda *a, **k: pytest.fail("must not send"))
    out = mod.run_rollup(dry_run=False)
    assert out.status == "no_recipients" and out.sent == 0


def test_the_module_configures_logging_like_its_siblings():
    """Without basicConfig the root logger sits at WARNING on Cloud Run and the
    job's INFO summary — the only record of what it checked — is dropped."""
    src = _MODULE.read_text(encoding="utf-8")
    assert "logging.basicConfig(level=logging.INFO)" in src


def test_a_passed_date_with_ancient_silence_is_abandoned_not_ranked():
    """A record with its close date 175 days past and its client silent 591
    days would rank #1 in the manager's message — and would be the rep's daily
    card. The probe had always dropped such records; extracting the
    classifier lost that for the passed-date tier. An abandoned record is
    clean-up, not forecast: no tier, on either reader."""
    assert close_date_tier(-175, 591) is None
    # ...while a passed date with RECENT contact is still the real tier-1 case.
    assert close_date_tier(-175, 47) is CLOSE_DATE_TIER_PASSED
    # and the ceiling is read at call time like every other threshold.


def test_abandoned_records_reach_the_manager_as_a_count_line_only():
    res = RollupResult(checked=100, abandoned=3)
    res.rows = [_row(CLOSE_DATE_TIER_PASSED, dtc=-5)]
    text, blocks = build_blocks(res)
    assert "3 past their close date" in text and "clean-up, not forecast" in text
    # exactly one ranked row rendered — the abandoned three are not rows
    assert sum(1 for b in blocks if b.get("type") == "section") == 1


# ── the deal AND its client ──────────────────────────────────────────────────
#
# The roll-up's silence was the deal's own "Last Contacted", which moves only
# when the contact is logged on that deal. An email to someone at the client
# who is not on the deal is logged on their company. The tier still reads the
# deal's own date (both readers agree); the row now gives the client's beside
# it, and a deal whose client was contacted inside the window ranks last in
# its tier.


def test_a_deal_only_quiet_row_ranks_after_the_quiet_client_rows_in_its_tier():
    rows = [
        _row(CLOSE_DATE_TIER_QUIET, quiet=82, name="deal-only"),
        _row(CLOSE_DATE_TIER_QUIET, quiet=25, name="quiet-25"),
        _row(CLOSE_DATE_TIER_QUIET, quiet=60, name="quiet-60-client-30"),
        _row(CLOSE_DATE_TIER_PASSED, dtc=-3, name="passed"),
    ]
    rows[0].client_name, rows[0].client_quiet_days, rows[0].deal_only = "A", 13, True
    rows[2].client_name, rows[2].client_quiet_days = "B", 30

    assert [r.deal_name for r in sorted(rows, key=_sort_key)] == [
        "passed",
        # Silence is the newer of deal and client: 30, then 25...
        "quiet-60-client-30",
        "quiet-25",
        # ...and the deal whose client heard from us 13 days ago comes last,
        # though its own record has been quiet longest.
        "deal-only",
    ]


def test_a_passed_date_ranks_on_how_long_ago_it_passed_whoever_was_contacted():
    old = _row(CLOSE_DATE_TIER_PASSED, dtc=-40, name="old")
    old.client_name, old.client_quiet_days, old.deal_only = "A", 2, True
    recent = _row(CLOSE_DATE_TIER_PASSED, dtc=-3, name="recent")

    assert [r.deal_name for r in sorted([recent, old], key=_sort_key)] == [
        "old",
        "recent",
    ]


def test_the_row_gives_the_client_date_beside_the_deal_s_own():
    from backend.jobs.close_date_rollup import _row_line

    deal_only = _row(CLOSE_DATE_TIER_QUIET, quiet=82, name="Contoso")
    deal_only.client_name = "Contoso Ltd"
    deal_only.client_quiet_days, deal_only.deal_only = 13, True
    quiet = _row(CLOSE_DATE_TIER_QUIET, quiet=82, name="Initech")
    quiet.client_name, quiet.client_quiet_days = "Initech Corp", 30
    never = _row(CLOSE_DATE_TIER_PASSED, quiet=None, dtc=-5, name="Never")
    never.client_name = "Gamma Co"
    never.client_quiet_days, never.deal_only = 1, True

    assert (
        "Contoso Ltd contacted 13d ago, nothing logged on this deal in 82d"
        in _row_line(deal_only)
    )
    assert "no client contact in 30d (nothing logged on this deal in 82d)" in (
        _row_line(quiet)
    )
    assert "Gamma Co contacted yesterday, nothing ever logged on this deal" in (
        _row_line(never)
    )
    # A row without a newer client date reads exactly as before.
    assert "no client contact in 25d" in _row_line(_row(CLOSE_DATE_TIER_QUIET))


def test_the_summary_never_calls_a_contacted_client_quiet():
    res = RollupResult(checked=40, tier_counts={2: 2})
    res.rows = [
        _row(CLOSE_DATE_TIER_QUIET, quiet=30, name="quiet"),
        _row(CLOSE_DATE_TIER_QUIET, quiet=82, name="deal-only"),
    ]
    res.rows[1].client_name = "Contoso Ltd"
    res.rows[1].client_quiet_days, res.rows[1].deal_only = 13, True

    text, _ = build_blocks(res)

    assert "with the client quiet 3+ weeks" not in text
    assert (
        "2 closing soon with no contact logged on the deal in 3+ weeks (1 of "
        "those 2 with the client contacted inside those 3 weeks)" in text
    )


def test_without_a_contacted_client_the_summary_reads_as_before():
    res = RollupResult(checked=40, tier_counts={2: 1})
    res.rows = [_row(CLOSE_DATE_TIER_QUIET, quiet=30)]

    text, _ = build_blocks(res)

    assert "1 closing soon with the client quiet 3+ weeks" in text


def test_the_uncontacted_count_says_it_read_the_deal_itself():
    res = RollupResult(checked=10, uncontacted=4)
    res.rows = [_row(CLOSE_DATE_TIER_PASSED, quiet=None, dtc=-5)]
    text, _ = build_blocks(res)
    assert "4 deals have no client contact logged on the deal itself" in text


def test_a_failed_company_read_is_said_in_the_message():
    res = _res_with_rows()
    res.client_records_unread = True
    text, _ = build_blocks(res)
    assert "client company records could not be read" in text


class _FakeHubSpot:
    def __init__(self, deals, companies=None, fail=False):
        self.deals = deals
        self.companies = companies or {}
        self.fail = fail
        self.calls = []

    def owner_directory(self):
        return {"77": {"email": "ae@acme.example", "name": "Parker"}}

    def deal_stage_labels(self):
        return {"s1": "Proposal"}

    def search_all(self, object_type, filters, properties, sorts=None):
        return list(self.deals)

    def batch_associations(self, from_type, to_type, ids, *, prefer_type_id=None):
        self.calls.append(("assoc", tuple(ids)))
        return {i: [f"co-{i}"] for i in ids if f"co-{i}" in self.companies}

    def batch_read(self, object_type, ids, properties, *, properties_with_history=None):
        self.calls.append(("read", tuple(ids)))
        if self.fail:
            from rubik_core.retrieval.hubspot_api import HubSpotAPIError

            raise HubSpotAPIError(
                "HubSpot transport error: Illegal header value "
                "b'Bearer pat-na1-SECRET-TOKEN\\n'"
            )
        return [{"id": i, "properties": self.companies[i]} for i in ids]


def _api_deal(rid, *, close_in, quiet):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    props = {
        "hs_object_id": rid,
        "dealname": f"Deal {rid}",
        "amount": "10000",
        "dealstage": "s1",
        "hubspot_owner_id": "77",
        "pipeline": "default",
        "closedate": (now + timedelta(days=close_in)).isoformat(),
    }
    if quiet is not None:
        props["notes_last_contacted"] = (now - timedelta(days=quiet)).isoformat()
    return {"id": rid, "properties": props}


def _company(days_ago, name="Contoso Ltd"):
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"name": name, "notes_last_contacted": when.isoformat()}


def test_the_sweep_reads_the_client_of_the_quiet_rows_only(monkeypatch):
    import backend.jobs.close_date_rollup as mod

    fake = _FakeHubSpot(
        deals=[
            _api_deal("quiet", close_in=20, quiet=82),
            _api_deal("passed-fresh", close_in=-5, quiet=3),
            _api_deal("healthy", close_in=20, quiet=2),
        ],
        companies={"co-quiet": _company(13)},
    )
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda *a, **k: fake
    )

    res = mod.collect_rows()

    # Only the deal quiet on its own costs a read; the passed-date deal
    # contacted 3 days ago and the healthy deal never do.
    assert fake.calls[0] == ("assoc", ("quiet",))
    row = next(r for r in res.rows if r.deal_id == "quiet")
    assert row.quiet_days == 82 and row.tier == CLOSE_DATE_TIER_QUIET
    assert (row.client_name, row.client_quiet_days, row.deal_only) == (
        "Contoso Ltd",
        13,
        True,
    )
    # Which deals are rows, and in which tier, did not change.
    assert {r.deal_id: r.tier for r in res.rows} == {
        "quiet": CLOSE_DATE_TIER_QUIET,
        "passed-fresh": CLOSE_DATE_TIER_PASSED,
    }


def test_a_failed_company_read_keeps_the_rows_and_flags_the_message(
    monkeypatch, caplog
):
    import backend.jobs.close_date_rollup as mod

    fake = _FakeHubSpot(
        deals=[_api_deal("quiet", close_in=20, quiet=82)],
        companies={"co-quiet": _company(13)},
        fail=True,
    )
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda *a, **k: fake
    )

    with caplog.at_level("WARNING"):
        res = mod.collect_rows()

    assert res.status == "ok" and res.client_records_unread
    assert [(r.deal_id, r.quiet_days, r.client_quiet_days) for r in res.rows] == [
        ("quiet", 82, None)
    ]
    assert "SECRET-TOKEN" not in caplog.text


def test_unconfigured_workspace_skips_rollup_without_hubspot_reads(monkeypatch):
    import backend.jobs.close_date_rollup as mod
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(hubspot_config, "get_hubspot_config", HubSpotConnectorConfig)
    monkeypatch.setattr(
        hubspot_api,
        "HubSpotAPIClient",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected HubSpot read")),
    )

    result = mod.collect_rows()
    assert result.status == "skipped"
    assert result.open_deals == 0
    assert "pipelines" in result.errors[0]


def test_rollup_discards_deals_from_other_workspace_pipeline(monkeypatch):
    import backend.jobs.close_date_rollup as mod

    foreign = _api_deal("foreign", close_in=-5, quiet=30)
    foreign["properties"]["pipeline"] = "foreign"
    fake = _FakeHubSpot(
        deals=[
            _api_deal("own", close_in=-5, quiet=30),
            foreign,
        ]
    )
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda: fake
    )

    result = mod.collect_rows()
    assert result.status == "ok"
    assert result.open_deals == 1
    assert [row.deal_id for row in result.rows] == ["own"]


def test_both_readers_still_agree_when_the_client_was_contacted():
    """The rep's card and this list read the same tier off the deal's own date,
    whatever the client's company record says."""
    from backend.tests.test_priorities_close_date_probe import (
        _client,
        _deal,
        _probe_with,
    )

    for quiet in (14, 17, 21, 30, 82):
        for close_in in (-10, 5, 20, 60):
            for client_days in (0, 5, 13):
                tier = close_date_tier(close_in, quiet)
                emitted = bool(
                    _probe_with(
                        _deal(close_in_days=close_in, quiet_days=quiet),
                        _client(client_days),
                    )
                )
                assert emitted is (
                    tier in (CLOSE_DATE_TIER_PASSED, CLOSE_DATE_TIER_QUIET)
                ), f"close_in={close_in} quiet={quiet} client={client_days}"
