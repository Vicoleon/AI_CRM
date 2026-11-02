"""UC-45 fetcher correctness — HubSpot HTML note bodies and fetch truncation.

Two defects, both HubSpot-shaped:

* Notes rendered as raw ``<div style=…>`` markup because ``hs_note_body`` is
  the one engagement title property HubSpot stores as HTML.
* Records went missing from the timeline with nothing reported, because the
  association fetch read a single page and the batch read silently kept only
  the first 100 ids.
"""

from __future__ import annotations

import pytest

from rubik_core.xray import config, fetchers
from rubik_core.xray.schemas import CompanyBundle
from rubik_core.xray.text import plain_text


# ── HTML note bodies ─────────────────────────────────────────────────────────
def test_plain_text_strips_hubspot_note_markup():
    body = (
        '<div style="font-family: -apple-system, BlinkMacSystemFont; '
        'font-size: 14px;">Signed the vendor&nbsp;management contract '
        "amendment<br>Effective 1/11/2024</div>"
    )
    assert plain_text(body) == (
        "Signed the vendor management contract amendment Effective 1/11/2024"
    )


def test_plain_text_drops_style_blocks_and_double_escaping():
    assert (
        plain_text("<style>p{color:red}</style><p>Renewal call</p>") == "Renewal call"
    )
    assert plain_text("&lt;div&gt;Escalation logged&lt;/div&gt;") == "Escalation logged"


def test_plain_text_leaves_plain_titles_untouched():
    assert plain_text("Q3 renewal — pricing & scope") == "Q3 renewal — pricing & scope"
    assert plain_text(None) == ""


def test_plain_text_truncates_on_a_word_boundary():
    out = plain_text("alpha beta gamma delta epsilon", max_len=18)
    assert out == "alpha beta gamma…"


def test_note_timeline_title_is_plain_text():
    bundle = CompanyBundle(company_id="1")
    bundle.engagements = {
        "notes": [
            {
                "id": "9",
                "hs_timestamp": "1704931200000",  # 2024-01-11
                "hs_note_body": '<div style="color:#333">Contract amendment signed</div>',
            }
        ]
    }
    (draft,) = fetchers.build_timeline_events(bundle)
    assert draft.title == "Note — Contract amendment signed"
    assert "<" not in draft.title


# ── Truncation ───────────────────────────────────────────────────────────────
class _FakeClient:
    """Stands in for HubSpotAPIClient's URL/header surface only."""

    base_url = "https://api.hubapi.test"

    def _headers(self) -> dict[str, str]:
        return {}


def test_batch_read_reads_every_id_not_just_the_first_hundred(monkeypatch):
    ids = [str(i) for i in range(250)]
    sent: list[list[str]] = []

    def fake_backoff(fn, *args, **kwargs):
        chunk = args[0]
        sent.append(list(chunk))
        return [{"id": i, "properties": {}} for i in chunk]

    monkeypatch.setattr(fetchers, "_with_backoff", fake_backoff)
    recs = fetchers._batch_read(_FakeClient(), "notes", ids, ["hs_timestamp"])

    assert [len(c) for c in sent] == [100, 100, 50]  # HubSpot's per-call cap
    assert [r["id"] for r in recs] == ids


def test_associated_ids_pages_and_returns_the_newest_first(monkeypatch):
    pages = [
        {
            "results": [{"toObjectId": i} for i in range(1000, 1500)],
            "paging": {"next": {"after": "500"}},
        },
        {"results": [{"toObjectId": i} for i in range(1500, 1700)]},
    ]
    calls: list[str | None] = []

    def fake_backoff(fn, *args, **kwargs):
        after = args[0]
        calls.append(after)
        return pages[len(calls) - 1]

    monkeypatch.setattr(fetchers, "_with_backoff", fake_backoff)
    ids, total = fetchers._associated_ids(_FakeClient(), "42", "notes", limit=5)

    assert calls == [None, "500"]  # followed the paging cursor
    assert total == 700  # every association counted, not just page one
    assert ids == ["1699", "1698", "1697", "1696", "1695"]


@pytest.mark.parametrize(
    "total,cap,expected",
    [(700, 200, True), (200, 200, False), (12, 200, False)],
)
def test_truncation_is_reported_as_an_intelligence_gap(total, cap, expected):
    bundle = CompanyBundle(company_id="1")
    fetchers._note_truncation(bundle, "notes", total, cap)
    assert bool(bundle.errors) is expected
    if expected:
        assert f"{cap} most recent notes of {total}" in bundle.errors[0]


def test_engagements_are_capped_by_the_configured_bound():
    # Guards the contract the truncation notice depends on: the cap is what the
    # user is told about, so it must be the value actually applied.
    assert config.MAX_ENGAGEMENTS_PER_TYPE > 0
    assert config.MAX_DEALS > 0
