"""Slack DM blocks must stay under Slack's 3000-char section
cap so chat.postMessage never rejects the whole message with `invalid_blocks`.

Background: a long priority `next_action`/`body` produced a section over 3000
chars; Slack rejected the entire DM (the text fallback isn't posted either), so
the user silently got nothing. These tests pin the truncation that prevents it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from backend.agents.slack_delivery import (
    _SECTION_TEXT_LIMIT,
    _build_digest_blocks,
    _bundle_priority_to_item,
    _BundlePriority,
    _compiled_at_line,
    _priority_blocks,
    _time_of_day_greeting,
    _truncate_mrkdwn,
)
from rubik_core.brand import PRODUCT_NAME

# Slack's hard limit for a section's text.text (mrkdwn) field.
SLACK_SECTION_MAX = 3000


def _section_texts(blocks: list[dict]) -> list[str]:
    return [
        b["text"]["text"]
        for b in blocks
        if b.get("type") == "section" and "text" in b.get("text", {})
    ]


# ── _truncate_mrkdwn ──────────────────────────────────────────────────────────


def test_truncate_passes_short_text_through_unchanged():
    text = "🔴 *Follow up with Acme*\nSend the renewal quote today."
    assert _truncate_mrkdwn(text) == text


def test_truncate_caps_long_text_under_limit_with_suffix():
    out = _truncate_mrkdwn("x" * 10_000)
    assert len(out) <= _SECTION_TEXT_LIMIT <= SLACK_SECTION_MAX
    assert out.endswith(f"_(more in {PRODUCT_NAME})_")


def test_truncate_never_leaves_a_half_open_link():
    # A `<url|label>` link straddling the cut point must not leave a dangling `<`
    # (itself invalid Block Kit). Place the link just before the budget boundary.
    text = "a" * 2870 + "<https://example.com/x|More info>" + "b" * 200
    out = _truncate_mrkdwn(text)
    assert out.count("<") == out.count(">")  # balanced — no orphaned bracket
    assert len(out) <= _SECTION_TEXT_LIMIT


# ── _priority_blocks ──────────────────────────────────────────────────────────


def _item(**kw):
    base = dict(
        severity="urgent",
        title="Renewal at risk",
        confidence=0.9,
        next_action="",
        body="",
        link=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_priority_blocks_long_body_stays_under_slack_cap():
    """A 5000-char next_action must not blow past Slack's section limit."""
    items = [_item(next_action="Call the client. " * 350)]  # ~5950 chars
    _fallback, blocks = _priority_blocks(
        items, top_n=5, web_url="https://app.acme.example"
    )
    texts = _section_texts(blocks)
    assert texts, "expected at least one section block"
    for t in texts:
        assert len(t) <= SLACK_SECTION_MAX
        assert t.count("<") == t.count(">")  # no orphaned link bracket


def test_priority_blocks_fallback_lists_titles_for_text_only_degradation():
    items = [
        _item(title="Renewal at risk", severity="urgent"),
        _item(title="New lead from webinar", severity="info"),
    ]
    fallback, _blocks = _priority_blocks(items, top_n=5, web_url="")
    # The fallback doubles as the text-only body if we degrade on invalid_blocks,
    # so every shown priority's title must appear in it.
    assert "Renewal at risk" in fallback
    assert "New lead from webinar" in fallback


def test_priority_blocks_short_body_is_not_truncated():
    items = [_item(next_action="Send the renewal quote.")]
    _fallback, blocks = _priority_blocks(items, top_n=5, web_url="")
    (section,) = _section_texts(blocks)
    assert f"more in {PRODUCT_NAME}" not in section  # no truncation suffix
    assert "Send the renewal quote." in section


# ── _time_of_day_greeting (header salutation matches send-time local hour) ───


def _patch_local_hour(monkeypatch, hour: int):
    """Pin resolve_user_now() to a local datetime at ``hour`` (UTC tz, but only
    the hour is read)."""
    now = datetime(2026, 7, 24, hour, 0, tzinfo=timezone.utc)

    def _fake(_email=None, **_kw):
        return SimpleNamespace(now_local=now)

    monkeypatch.setattr("rubik_core.priorities.temporal.resolve_user_now", _fake)


def test_greeting_morning(monkeypatch):
    _patch_local_hour(monkeypatch, 8)
    assert _time_of_day_greeting("u@x.example") == "Good morning"


def test_greeting_afternoon(monkeypatch):
    # The 1:45pm weekly-ingest send that motivated this fix.
    _patch_local_hour(monkeypatch, 13)
    assert _time_of_day_greeting("u@x.example") == "Good afternoon"


def test_greeting_evening(monkeypatch):
    _patch_local_hour(monkeypatch, 20)
    assert _time_of_day_greeting("u@x.example") == "Good evening"


def test_greeting_falls_back_to_hello_on_lookup_error(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("tz lookup down")

    monkeypatch.setattr("rubik_core.priorities.temporal.resolve_user_now", _boom)
    assert _time_of_day_greeting("u@x.example") == "Hello"


def test_priority_blocks_uses_supplied_greeting_in_header_and_fallback():
    items = [_item(title="Renewal at risk")]
    fallback, blocks = _priority_blocks(
        items, top_n=5, web_url="", greeting="Good afternoon"
    )
    header = blocks[0]["text"]["text"]
    assert header == "Good afternoon — your top priorities for today"
    assert fallback.startswith("Good afternoon — your top priorities for today")


# ── _build_digest_blocks (manager digest, same cap) ──────────────────────────


def test_digest_blocks_long_title_stays_under_slack_cap():
    # Manager digest now renders each team member's AI-Priority title; a very
    # long title must still be truncated under Slack's section cap.
    long_p = _BundlePriority(
        title="Escalate immediately. " * 300,  # ~6600 chars
        severity="urgent",
        body="",
        link=None,
    )
    _text, blocks = _build_digest_blocks(
        "engineering", [("rep", long_p)], max_items=5, web_url=""
    )
    for t in _section_texts(blocks):
        assert len(t) <= SLACK_SECTION_MAX


# ── judged-bundle mapping (the fix: the DM mirrors the Cubie panel) ───────────
#
# Regression for a reported bug: the Slack DM showed the mechanical
# "Contact priority: <company>" card five times with a "100%" chip, while the
# app showed distinct, judged cards. The DM now maps the SAME judged bundle the
# web panel renders (home._load_today_priorities), so titles/order/set match and
# no misleading confidence chip appears.


def _bundle_priority(**kw):
    base = dict(
        id="p1",
        title="Prepare for 1:1 with Jordan Hayes (Initech Partners)",
        why_it_matters="You have an external 1:1 with Jordan today, July 2.",
        suggested_action="Spend 15 minutes reviewing the Initech doc.",
        urgency="high",
        source_links=["https://mail.google.com/mail/u/0/#inbox/abc"],
    )
    base.update(kw)
    return base


def test_bundle_priority_maps_judged_title_not_mechanical():
    item = _bundle_priority_to_item(_bundle_priority())
    assert item.title == "Prepare for 1:1 with Jordan Hayes (Initech Partners)"
    assert not item.title.lower().startswith("contact priority")
    assert item.severity == "urgent"  # high urgency → red dot
    assert item.confidence is None  # no misleading "100%" chip
    assert "You have an external 1:1" in item.body  # why_it_matters
    assert "*Next:* Spend 15 minutes" in item.body  # suggested_action as Next line
    assert item.link == "https://mail.google.com/mail/u/0/#inbox/abc"


def test_bundle_priority_urgency_severity_mapping():
    assert (
        _bundle_priority_to_item(_bundle_priority(urgency="high")).severity == "urgent"
    )
    assert (
        _bundle_priority_to_item(_bundle_priority(urgency="medium")).severity == "info"
    )
    assert _bundle_priority_to_item(_bundle_priority(urgency="low")).severity == "ok"
    # Unknown/empty urgency falls back to a neutral (non-red) dot.
    assert _bundle_priority_to_item(_bundle_priority(urgency="")).severity == "info"


def test_bundle_priority_ignores_non_url_source_links():
    item = _bundle_priority_to_item(_bundle_priority(source_links=["not-a-url", ""]))
    assert item.link is None


def test_bundle_priorities_render_distinct_titles_no_confidence_chip():
    # The old bug: 5 identical "Contact priority: <company>" sections + "100%".
    # The judged bundle → distinct titles, no percentage chip, no mechanical label.
    bundle = [
        _bundle_priority(title="Review John Adams Compliance Meeting Actions"),
        _bundle_priority(title="Prepare for 1:1 with Jordan Hayes (Initech Partners)"),
        _bundle_priority(title="Follow up on the data purchase", urgency="low"),
    ]
    items = [_bundle_priority_to_item(p) for p in bundle]
    _fallback, blocks = _priority_blocks(
        items, top_n=5, web_url="https://app.acme.example"
    )
    texts = _section_texts(blocks)
    titles = [t.splitlines()[0] for t in texts]
    assert len(titles) == len(set(titles)), "each judged priority is a distinct section"
    for t in texts:
        assert "%`" not in t  # no confidence percentage chip
        assert "Contact priority:" not in t  # no mechanical rec title


def test_dm_source_is_the_apps_judged_bundle_accessor():
    """The DM reads the SAME bundle accessor the web panel uses. Pin that the
    private symbol the loader lazy-imports still exists + is callable, so a
    future rename fails loudly here instead of silently breaking Slack delivery
    (and quietly reverting to the mechanical-recs bug)."""
    from backend.agents.slack_delivery import _load_bundle_priority_items
    from backend.routers.home import _load_today_priorities

    assert callable(_load_bundle_priority_items)
    assert callable(_load_today_priorities)


# ── The "compiled at" freshness stamp ─────────────────────────────────────────
#
# The reader wants to know how old the list is before acting on it, and to be
# offered a refresh. These pin the three ways the line can go wrong — a
# fabricated time, a missing plain-text fallback, and a claim about data
# freshness that Cubie cannot actually support.


def test_compiled_at_line_renders_in_the_readers_timezone():
    """A UTC instant is shown as the READER's wall clock, with the zone named.

    Readers span ET and Central America, so an unlabelled time is ambiguous.
    """
    line = _compiled_at_line("2026-09-21T10:04:00+00:00", "someone@acme.example")
    assert line is not None
    assert line.startswith("Compiled ")
    assert "refresh" in line.lower()
    # A zone must be named, and it must not be a bare offset-less clock.
    assert any(c.isalpha() for c in line.split("Compiled ", 1)[1].split("—")[0])


def test_compiled_at_line_is_none_when_there_is_no_usable_stamp():
    """No line beats a wrong line: a missing or unparseable stamp renders
    nothing rather than inventing a time."""
    assert _compiled_at_line(None) is None
    assert _compiled_at_line("") is None
    assert _compiled_at_line("not-a-timestamp") is None


def test_compiled_at_line_never_claims_freshness_of_the_UNDERLYING_data():
    """`generated_at` is when the PIPELINE finished, not how fresh the email or
    HubSpot data is. Per-channel freshness is computed but never persisted, so
    any "your email as of X" phrasing would be fabricated."""
    line = _compiled_at_line("2026-09-21T10:04:00+00:00", "someone@acme.example")
    assert line is not None
    lowered = line.lower()
    assert "compiled" in lowered
    for fabricated in ("as of", "your email", "up to date", "current as"):
        assert fabricated not in lowered


def test_compiled_at_stamp_reaches_both_the_blocks_and_the_text_fallback():
    """The fallback is the notification preview AND the text-only degradation
    path on invalid_blocks — a context block alone would lose the stamp there."""
    stamp = f"Compiled 6:04 AM EDT — open {PRODUCT_NAME} to refresh."
    fallback, blocks = _priority_blocks(
        [_item(title="Renewal at risk")],
        top_n=5,
        web_url="https://app.acme.example",
        compiled_at=stamp,
    )
    contexts = [
        e["text"]
        for b in blocks
        if b.get("type") == "context"
        for e in b.get("elements", [])
    ]
    assert stamp in contexts
    assert stamp in fallback


def test_compiled_at_stamp_is_omitted_entirely_when_unknown():
    """A bundle with no stamp must render exactly as it did before — no empty
    context block, no placeholder."""
    _fallback, blocks = _priority_blocks(
        [_item(title="Renewal at risk")],
        top_n=5,
        web_url="https://app.acme.example",
        compiled_at=None,
    )
    contexts = [
        e["text"]
        for b in blocks
        if b.get("type") == "context"
        for e in b.get("elements", [])
    ]
    assert not any("Compiled" in c for c in contexts)


def test_compiled_at_stamp_sits_before_the_open_app_button():
    """Ordering is the whole point of the ask: the freshness claim and the
    control that renews it must read as one thought."""
    _fallback, blocks = _priority_blocks(
        [_item(title="Renewal at risk")],
        top_n=5,
        web_url="https://app.acme.example",
        compiled_at=f"Compiled 6:04 AM EDT — open {PRODUCT_NAME} to refresh.",
    )
    kinds = [b.get("type") for b in blocks]
    assert "actions" in kinds, f"expected the Open {PRODUCT_NAME} button"
    stamp_idx = next(
        i
        for i, b in enumerate(blocks)
        if b.get("type") == "context"
        and any("Compiled" in e.get("text", "") for e in b.get("elements", []))
    )
    assert stamp_idx < kinds.index("actions")


def test_compiled_at_stamp_is_truncated_like_every_other_section():
    """Defensive: the stamp is short today, but it flows through the same cap so
    a future wording change can never be the thing that trips invalid_blocks."""
    _fallback, blocks = _priority_blocks(
        [_item(title="Renewal at risk")],
        top_n=5,
        web_url="https://app.acme.example",
        compiled_at="C" * 10_000,
    )
    for b in blocks:
        for e in b.get("elements", []) if b.get("type") == "context" else []:
            assert len(e["text"]) <= SLACK_SECTION_MAX
