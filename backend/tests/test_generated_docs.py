"""Cubie-generated docs: Word renderers, save-to-Assets, sharing, Gmail drafts.

Pure unit tests — the DAM registry, notification writer, and Gmail transport
are faked/patched, so nothing here can touch Cloud SQL, GCS, or real Gmail
(a test that reaches a shared database pollutes it). Router glue is thin and
exercised by the existing RBAC suite's harness.
"""

from __future__ import annotations

import base64
import email
import hashlib
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from docx import Document

from rubik_core.deep_prep.docx import render_deep_prep_docx, render_meeting_prep_docx
from rubik_core.identifiers import ASSET_TAG_GENERATED
from rubik_core.xray.docx import render_xray_docx

# ─── Renderers ───────────────────────────────────────────────────────────────


def _texts(data: bytes) -> list[str]:
    doc = Document(BytesIO(data))  # re-opening proves it's a valid .docx
    return [p.text for p in doc.paragraphs if p.text.strip()]


def test_meeting_prep_docx_renders_and_relabels_internal():
    resp = {
        "title": "Q3 Review with Acme",
        "executive_summary": "**Summary** line",
        "likely_objections": "Price sensitivity",
        "discovery_questions": ["Budget?", "Timeline?"],
        "stakeholder_map": '[{"name": "Jane", "role": "CTO", "email": "j@acmecorp.example", "context": "Champion"}]',
        "source_evidence": [],
        "is_internal": True,
        "is_sales_user": False,
    }
    texts = _texts(render_meeting_prep_docx(resp, prepared_for="bellison@acme.example"))
    assert "Q3 Review with Acme" in texts
    # Internal briefs relabel sections, exactly like the PDF/frontend.
    assert "Potential friction" in texts
    assert "Key questions" in texts
    assert any("Jane — CTO" in t for t in texts)
    # Markdown tokens never reach the document text.
    assert not any("**" in t for t in texts)


def test_meeting_prep_docx_sets_word_title_metadata():
    data = render_meeting_prep_docx(
        {"title": "Kickoff with Globex"}, prepared_for="u@acme.example"
    )
    assert Document(BytesIO(data)).core_properties.title == "Kickoff with Globex"


def test_deep_prep_docx_renders_variable_sections():
    result = {
        "title": "Deep: Acme Renewal",
        "headline": "Renewal is at risk.",
        "sections": [
            {"heading": "Where things stand", "body": "Body.", "sources": ["Email 7/1"]}
        ],
        "data_gaps": ["No 2026 pricing doc"],
        "open_questions": ["Who signs?"],
        "all_sources": ["CRM"],
        "is_sales_user": True,
        "is_internal": False,
    }
    texts = _texts(render_deep_prep_docx(result, prepared_for="u@acme.example"))
    for expected in (
        "Deep: Acme Renewal",
        "In brief",
        "Where things stand",
        "What we don't know",
        "Open questions",
    ):
        assert expected in texts, f"missing {expected!r}"


def test_xray_docx_renders_churn_and_sections():
    data = render_xray_docx(
        company_name="Acme Corp",
        signals={
            "churn": {
                "score": 42,
                "band": "Watch",
                "rationale_md": "Drivers below.",
                "factors": [
                    {"name": "no_touch", "points": 70, "value": "45d", "evidence": "ev"}
                ],
            },
            "client_status": "Active",
        },
        sections={
            "executive_summary": "Summary.",
            "opportunities": [
                {"title": "Upsell QC", "why_now": "w", "play": "p", "entity": "Acme"}
            ],
        },
        prepared_for="u@acme.example",
        generated_at="Jul 22, 2026",
    )
    texts = _texts(data)
    assert any("Churn risk: 42/100 — Watch" in t for t in texts)
    assert "Executive summary" in texts
    assert any("Upsell QC" in t for t in texts)
    assert Document(BytesIO(data)).core_properties.title == "Customer X-Ray - Acme Corp"


# ─── save_generated_doc (local mode, fake registry) ──────────────────────────


class FakeRegistry:
    def __init__(self):
        self.assets: dict[str, object] = {}

    def register_asset(self, asset):
        self.assets[asset.doc_id] = asset

    def update_asset(self, doc_id, fields):
        for k, v in fields.items():
            setattr(self.assets[doc_id], k, v)
        return self.assets[doc_id]

    def get_asset_by_content_hash(self, content_hash):
        return next(
            (a for a in self.assets.values() if a.content_hash == content_hash), None
        )


@pytest.fixture()
def gen_docs(tmp_path, monkeypatch):
    from backend.services import generated_docs as mod
    from rubik_core.company import operations
    from rubik_core.config import AppMode, settings

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    fake = FakeRegistry()
    monkeypatch.setattr(mod, "_registry", fake)
    monkeypatch.setattr(mod, "log_action", lambda *a, **k: None)
    monkeypatch.setattr(
        operations,
        "get_operations_config",
        lambda: SimpleNamespace(deep_prep_sender_mailbox=""),
    )
    return mod, fake


def test_save_generated_doc_registers_without_ingestion(gen_docs, tmp_path):
    mod, fake = gen_docs
    asset, created = mod.save_generated_doc(
        "bellison@acme.example",
        title="Q3 Review",
        filename="meeting-prep-q3-review.docx",
        data=b"docx-bytes",
        kind="meeting-prep",
        source_prep_run_id="run-1",
    )
    assert created is True
    assert asset.doc_id.startswith("gendoc-")
    assert ASSET_TAG_GENERATED in asset.tags and "meeting-prep" in asset.tags
    assert f"{mod.MEETING_PREP_RUN_TAG_PREFIX}run-1" in asset.tags
    assert f"{mod.MEETING_PREP_WORKSPACE_TAG_PREFIX}test" in asset.tags
    assert asset.topic == "Meeting Prep"
    assert asset.processing is False  # never enters the ingestion pipeline
    # Bytes really landed on disk and round-trip through load_doc_bytes.
    assert mod.load_doc_bytes(asset.source_path) == b"docx-bytes"


def test_save_generated_doc_stamps_department(gen_docs):
    """The saved asset must carry a department — a department-less doc escapes
    scoped-Exec oversight and access policies (has_document_access rules 3/5)."""
    mod, _ = gen_docs
    asset, _ = mod.save_generated_doc(
        "bellison@acme.example",
        title="Q3 Review",
        filename="meeting-prep-q3.docx",
        data=b"bytes",
        kind="meeting-prep",
        source_prep_run_id="run-1",
        department="Systems",
    )
    assert asset.department == "Systems"


def test_xray_saved_doc_carries_immutable_company_provenance(gen_docs):
    mod, _ = gen_docs
    asset, created = mod.save_generated_doc(
        "bellison@acme.example",
        title="Customer X-Ray",
        filename="customer-xray-acme.docx",
        data=b"xray-content",
        kind="customer-xray",
        source_company_id="123",
    )
    assert created
    assert asset.doc_id.startswith(mod.XRAY_DOC_ID_PREFIX)
    assert f"{mod.XRAY_COMPANY_TAG_PREFIX}123" in asset.tags
    with pytest.raises(ValueError, match="company ID"):
        mod.save_generated_doc(
            "bellison@acme.example",
            title="Customer X-Ray",
            filename="customer-xray-acme.docx",
            data=b"another-content",
            kind="customer-xray",
        )


def test_legacy_xray_dedup_without_company_provenance_is_refused(gen_docs):
    mod, fake = gen_docs
    data = b"old-xray-content"
    fake.register_asset(
        _asset(
            doc_id="legacy",
            owner="bellison@acme.example",
            tags=[mod.ASSET_TAG_GENERATED, "customer-xray"],
            content_hash=hashlib.sha256(data).hexdigest(),
        )
    )
    asset, created = mod.save_generated_doc(
        "bellison@acme.example",
        title="Customer X-Ray",
        filename="customer-xray-acme.docx",
        data=data,
        kind="customer-xray",
        source_company_id="123",
    )
    assert created
    assert asset.doc_id != "legacy"


def test_save_generated_doc_dedups_identical_bytes(gen_docs):
    mod, fake = gen_docs
    first, created1 = mod.save_generated_doc(
        "bellison@acme.example",
        title="T",
        filename="a.docx",
        data=b"same",
        kind="meeting-prep",
        source_prep_run_id="run-1",
    )
    second, created2 = mod.save_generated_doc(
        "bellison@acme.example",
        title="T",
        filename="a.docx",
        data=b"same",
        kind="meeting-prep",
        source_prep_run_id="run-1",
    )
    assert created1 is True and created2 is False
    assert second.doc_id == first.doc_id
    assert len(fake.assets) == 1


def test_meeting_prep_dedup_does_not_reuse_another_runs_provenance(gen_docs):
    mod, _ = gen_docs
    first, _ = mod.save_generated_doc(
        "bellison@acme.example",
        title="T",
        filename="a.docx",
        data=b"same",
        kind="meeting-prep",
        source_prep_run_id="run-1",
    )
    second, created = mod.save_generated_doc(
        "bellison@acme.example",
        title="T",
        filename="a.docx",
        data=b"same",
        kind="meeting-prep",
        source_prep_run_id="run-2",
    )
    assert created
    assert first.doc_id != second.doc_id


def test_is_generated_asset_reads_tag(gen_docs):
    mod, _ = gen_docs
    assert mod.is_generated_asset(SimpleNamespace(tags=[ASSET_TAG_GENERATED, "x"]))
    assert not mod.is_generated_asset(SimpleNamespace(tags=["upload"]))
    assert not mod.is_generated_asset(SimpleNamespace(tags=None))


# ─── share_asset ─────────────────────────────────────────────────────────────


def _asset(**over):
    from rubik_core.dam.registry import AssetMetadata

    base = dict(
        doc_id="gendoc-1",
        owner="bellison@acme.example",
        status="approved",
        topic="Meeting Prep",
        tags=[ASSET_TAG_GENERATED, "meeting-prep"],
        original_filename="meeting-prep-q3.docx",
        source_path="/tmp/x.docx",
        allowed_emails=[],
        content_hash="h",
        created_at=datetime(2026, 7, 22),
        updated_at=datetime(2026, 7, 22),
    )
    base.update(over)
    return AssetMetadata(**base)


def test_share_rejects_external_recipients(gen_docs):
    mod, fake = gen_docs
    fake.register_asset(_asset())
    with pytest.raises(ValueError, match="verified workspace domain"):
        mod.share_asset(
            "bellison@acme.example", fake.assets["gendoc-1"], ["x@gmail.com"]
        )


def test_share_appends_acl_notifies_and_emails(gen_docs, monkeypatch):
    mod, fake = gen_docs
    fake.register_asset(_asset(allowed_emails=["already@acme.example"]))
    notified: list[str] = []
    monkeypatch.setattr(
        mod, "_notify", lambda recipient, **k: notified.append(recipient)
    )
    sent = {}

    def fake_send(spec, **kw):
        sent["to"] = spec.to
        return SimpleNamespace(status="disabled")  # dark rollout is a fine outcome

    with patch("rubik_core.tools.email_sender.send_email", fake_send):
        out = mod.share_asset(
            "bellison@acme.example",
            fake.assets["gendoc-1"],
            # dupes, self, and an existing grantee all get filtered
            [
                "IFoster@acme.example",
                "ifoster@acme.example",
                "bellison@acme.example",
                "already@acme.example",
            ],
        )
    assert out["shared_with"] == ["ifoster@acme.example"]
    assert out["already_had_access"] == ["already@acme.example"]
    assert out["email_status"] == "disabled"
    assert notified == ["ifoster@acme.example"]  # bell only for NEW grantees
    assert sent["to"] == ["ifoster@acme.example"]
    assert fake.assets["gendoc-1"].allowed_emails == [
        "already@acme.example",
        "ifoster@acme.example",
    ]


def test_share_email_uses_workspace_sender_and_refuses_policy_outage(
    gen_docs, monkeypatch
):
    from rubik_core.company import operations

    mod, fake = gen_docs
    asset = _asset()
    fake.register_asset(asset)
    monkeypatch.setattr(mod, "_notify", lambda *_a, **_k: None)
    sender: list[str] = []

    def fake_send(_spec, *, impersonate):
        sender.append(impersonate)
        return SimpleNamespace(status="sent")

    monkeypatch.setattr(
        operations,
        "get_operations_config",
        lambda: SimpleNamespace(deep_prep_sender_mailbox="sales@acme.example"),
    )
    with patch("rubik_core.tools.email_sender.send_email", fake_send):
        result = mod.share_asset(
            "bellison@acme.example", asset, ["ifoster@acme.example"]
        )
    assert result["email_status"] == "sent"
    assert sender == ["sales@acme.example"]

    def unavailable():
        raise operations.OperationsConfigUnavailable("policy unavailable")

    monkeypatch.setattr(operations, "get_operations_config", unavailable)
    with patch("rubik_core.tools.email_sender.send_email", fake_send):
        result = mod.share_asset(
            "bellison@acme.example", asset, ["newperson@acme.example"]
        )
    assert result["email_status"] == "failed"
    assert sender == ["sales@acme.example"]


def test_share_email_escapes_the_document_name(gen_docs, monkeypatch):
    """The name can come from a model or an uploaded file; raw, it is HTML in
    mail we send from our own mailbox."""
    mod, fake = gen_docs
    fake.register_asset(
        _asset(original_filename='q3<img src="https://evil.example/x">.docx')
    )
    monkeypatch.setattr(mod, "_notify", lambda recipient, **k: None)
    sent = {}

    def fake_send(spec, **kw):
        sent["html"] = spec.html_body
        return SimpleNamespace(status="sent")

    with patch("rubik_core.tools.email_sender.send_email", fake_send):
        mod.share_asset(
            "bellison@acme.example",
            fake.assets["gendoc-1"],
            ["ifoster@acme.example"],
        )
    assert "<img" not in sent["html"]
    assert "q3&lt;img" in sent["html"]


# ─── Gmail draft with attachment ─────────────────────────────────────────────


def test_create_draft_embeds_attachment():
    from rubik_core.tools.gmail import GmailClient

    service = MagicMock()
    service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
        "id": "draft-1"
    }
    with (
        patch.object(GmailClient, "_build_service", return_value=service),
        patch.object(GmailClient, "get_signature", return_value=""),
    ):
        out = GmailClient("bellison@acme.example").create_draft(
            to=["ifoster@acme.example"],
            subject="Prep for Acme",
            body_markdown="See attached.",
            attachments=[
                (
                    "meeting-prep-q3.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    b"docx-bytes",
                )
            ],
        )
    assert out == {
        "draft_id": "draft-1",
        "gmail_draft_url": (
            "https://mail.google.com/mail/?authuser=bellison%40acme.example"
            "#drafts?compose=draft-1"
        ),
    }
    body = service.users.return_value.drafts.return_value.create.call_args.kwargs[
        "body"
    ]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
    attachments = [p for p in msg.walk() if p.get_content_disposition() == "attachment"]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "meeting-prep-q3.docx"
    assert attachments[0].get_payload(decode=True) == b"docx-bytes"
