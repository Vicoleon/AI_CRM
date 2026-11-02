"""Tenant-local process caches cannot return another workspace's data."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.agents import alignment_context
from rubik_core.company import domain as company_domain
from rubik_core.company import profile as company_profile
from rubik_core.insights import cache as insights_cache
from rubik_core.sales import rhythm_signals
from rubik_core.tenancy import context
from rubik_core.tenancy.context import NoWorkspaceContext, use_workspace


def test_company_profile_requires_context_and_scoped_invalidation(monkeypatch):
    values = {"alpha": "Alpha", "beta": "Beta"}
    reads: list[str] = []

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("rubik_core.database.db.SessionLocal", lambda: DummySession())

    def read(_db):
        slug = context.current_workspace()
        reads.append(slug)
        return company_profile.CompanyProfile(name=values[slug])

    monkeypatch.setattr(company_profile, "_read", read)
    company_profile.clear_cache()
    with use_workspace("alpha"):
        assert company_profile.get_company_profile().name == "Alpha"
        assert company_profile.get_company_profile().name == "Alpha"
    with use_workspace("beta"):
        assert company_profile.get_company_profile().name == "Beta"
        assert company_profile.get_company_profile().name == "Beta"
    assert reads == ["alpha", "beta"]

    values["alpha"] = "Alpha Two"
    with use_workspace("alpha"):
        company_profile.clear_cache("alpha")
        assert company_profile.get_company_profile().name == "Alpha Two"
    with use_workspace("beta"):
        assert company_profile.get_company_profile().name == "Beta"
    assert reads == ["alpha", "beta", "alpha"]

    monkeypatch.setattr(context, "_process_workspace", None)
    with pytest.raises(NoWorkspaceContext):
        company_profile.get_company_profile()


def test_company_profile_save_invalidates_only_selected_workspace(monkeypatch):
    from rubik_core.database.models import Base

    engines = {
        slug: create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        for slug in ("alpha", "beta")
    }
    for engine in engines.values():
        Base.metadata.create_all(engine)
    sessions = {slug: sessionmaker(bind=engine) for slug, engine in engines.items()}
    monkeypatch.setattr(
        "rubik_core.database.db.SessionLocal",
        lambda: sessions[context.current_workspace()](),
    )
    company_profile.clear_cache()
    reads: list[str] = []
    original_read = company_profile._read

    def tracked_read(db):
        reads.append(context.current_workspace())
        return original_read(db)

    monkeypatch.setattr(company_profile, "_read", tracked_read)
    try:
        for slug in ("alpha", "beta"):
            with use_workspace(slug), sessions[slug]() as db:
                company_profile.save_company_profile(
                    db, company_profile.CompanyProfile(name=slug)
                )
                assert company_profile.get_company_profile().name == slug
        with use_workspace("alpha"), sessions["alpha"]() as db:
            company_profile.save_company_profile(
                db, company_profile.CompanyProfile(name="Alpha Two")
            )
            assert company_profile.get_company_profile().name == "Alpha Two"
        with use_workspace("beta"):
            assert company_profile.get_company_profile().name == "beta"
        assert reads == ["alpha", "beta", "alpha"]
    finally:
        company_profile.clear_cache()
        for engine in engines.values():
            engine.dispose()


def test_company_domain_cache_is_separate_and_invalidates_one_slug(monkeypatch):
    reads: list[str] = []

    def rows(slug):
        reads.append(slug)
        return [(f"{slug}.example", True)]

    monkeypatch.setattr("rubik_core.control.repository.domains_for_workspace", rows)
    company_domain.clear_cache()
    with use_workspace("alpha"):
        assert company_domain.workspace_domain() == "alpha.example"
    with use_workspace("beta"):
        assert company_domain.workspace_domain() == "beta.example"
    with use_workspace("alpha"):
        company_domain.clear_cache("alpha")
        assert company_domain.workspace_domain() == "alpha.example"
    with use_workspace("beta"):
        assert company_domain.workspace_domain() == "beta.example"
    assert reads == ["alpha", "beta", "alpha"]
    company_domain.clear_cache()


def test_insights_payload_cache_separates_same_key_by_workspace():
    insights_cache.clear_cache()
    with use_workspace("alpha"):
        assert insights_cache.cached("same-viewer", lambda: "alpha")[0] == "alpha"
    with use_workspace("beta"):
        assert insights_cache.cached("same-viewer", lambda: "beta")[0] == "beta"
    with use_workspace("alpha"):
        assert insights_cache.cached("same-viewer", lambda: "wrong")[0] == "alpha"


def test_alignment_cache_separates_same_email_by_workspace():
    alignment_context._cache.clear()
    with use_workspace("alpha"):
        alignment_context._cache_set("person@example.com", "alpha")
    with use_workspace("beta"):
        assert alignment_context._cache_get("person@example.com") is None
        alignment_context._cache_set("person@example.com", "beta")
    with use_workspace("alpha"):
        assert alignment_context._cache_get("person@example.com") == "alpha"
        alignment_context.invalidate_cache("person@example.com")
        assert alignment_context._cache_get("person@example.com") is None
    with use_workspace("beta"):
        assert alignment_context._cache_get("person@example.com") == "beta"


def test_rhythm_mail_and_calendar_caches_separate_same_mailbox(monkeypatch):
    rhythm_signals.clear_cache()
    mail_reads: list[str] = []
    calendar_reads: list[str] = []
    monkeypatch.setattr(rhythm_signals, "_gmail_service", lambda mailbox: object())

    def latest(_service, queries):
        slug = context.current_workspace()
        mail_reads.append(slug)
        return {
            key: datetime(2026, 1 if slug == "alpha" else 2, 1, tzinfo=timezone.utc)
            for key in queries
        }

    class Calendar:
        def events(self):
            return self

        def list(self, **_kwargs):
            return self

        def execute(self):
            slug = context.current_workspace()
            calendar_reads.append(slug)
            return {"items": [{"summary": slug}]}

    monkeypatch.setattr(rhythm_signals, "_gmail_latest", latest)
    monkeypatch.setattr(rhythm_signals, "_calendar_service", lambda mailbox: Calendar())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    terms = {"deal-1": ["client@example.com"]}
    with use_workspace("alpha"):
        alpha_mail = rhythm_signals._gmail_touches("same@example.com", terms, 7)
        alpha_events = rhythm_signals._calendar_events("same@example.com", now)
    with use_workspace("beta"):
        beta_mail = rhythm_signals._gmail_touches("same@example.com", terms, 7)
        beta_events = rhythm_signals._calendar_events("same@example.com", now)
    with use_workspace("alpha"):
        assert rhythm_signals._gmail_touches("same@example.com", terms, 7) == alpha_mail
        assert rhythm_signals._calendar_events("same@example.com", now) == alpha_events
    assert alpha_mail != beta_mail
    assert alpha_events != beta_events
    assert mail_reads == calendar_reads == ["alpha", "beta"]


def test_whats_new_secret_cache_keeps_workspace_keys_apart():
    from rubik_core.whats_new import stats

    stats.reset_caches()

    class SecretRows:
        def get(self, _model, _key):
            slug = context.current_workspace()
            return SimpleNamespace(value={"secret": (slug + "x" * 32)})

    db = SecretRows()
    with use_workspace("alpha"):
        alpha = stats.viewer_key(db, "release", "same@example.com")
    with use_workspace("beta"):
        beta = stats.viewer_key(db, "release", "same@example.com")
    with use_workspace("alpha"):
        assert stats.viewer_key(db, "release", "same@example.com") == alpha
    assert alpha != beta
    stats.reset_caches()


def test_private_corpus_cache_is_workspace_scoped(monkeypatch):
    from rubik_core.chat_files import private_index
    from rubik_core.config import AppMode, settings

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(settings, "CHAT_FILE_PRIVATE_INDEX_ENABLED", True)
    monkeypatch.setattr(settings, "CHAT_FILE_PRIVATE_CORPUS", "private-library")
    private_index._corpus_names.clear()
    reads = []

    def control_binding(kind, *, create=False, audience=None):
        assert (kind, audience) == ("private_uploads", None)
        slug = context.current_workspace()
        reads.append(slug)
        return f"corpora/{slug}"

    monkeypatch.setattr(private_index, "_rag", lambda: SimpleNamespace())
    monkeypatch.setattr("rubik_core.tenancy.corpora.corpus_id", control_binding)
    with use_workspace("alpha"):
        assert private_index.resolve_corpus() == "corpora/alpha"
    with use_workspace("beta"):
        assert private_index.resolve_corpus() == "corpora/beta"
    with use_workspace("alpha"):
        assert private_index.resolve_corpus() == "corpora/alpha"
        private_index.forget_corpus()
    with use_workspace("beta"):
        assert private_index.resolve_corpus() == "corpora/beta"
    assert reads == ["alpha", "beta", "alpha", "beta"]
    assert private_index._corpus_names == {"beta": "corpora/beta"}


def test_hubspot_intent_cache_is_scoped_and_bounded(monkeypatch):
    from rubik_core.retrieval import hubspot

    monkeypatch.setattr(
        hubspot,
        "resolve_model_route",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="vertex_managed", model="test-model"
        ),
    )
    hubspot.INTENT_CACHE.clear()
    reads = []

    def generate_content(**_kwargs):
        slug = context.current_workspace()
        reads.append(slug)
        return SimpleNamespace(text=f'{{"intent":"{slug}"}}')

    retriever = hubspot.HubSpotRetriever.__new__(hubspot.HubSpotRetriever)
    retriever.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content)
    )
    retriever.model_id = "test-model"
    with use_workspace("alpha"):
        assert retriever._parse_intent("same query")["intent"] == "alpha"
    with use_workspace("beta"):
        assert retriever._parse_intent("same query")["intent"] == "beta"
    with use_workspace("alpha"):
        assert retriever._parse_intent("same query")["intent"] == "alpha"
    assert reads == ["alpha", "beta"]
    with use_workspace("alpha"):
        for index in range(hubspot._INTENT_CACHE_MAX + 2):
            assert retriever._parse_intent(f"new query {index}") is not None
    assert len(hubspot.INTENT_CACHE) <= hubspot._INTENT_CACHE_MAX
    hubspot.INTENT_CACHE.clear()


def test_call_capture_cache_invalidation_keeps_other_workspace():
    from rubik_core.call_capture import domain_match, hubspot_schema

    domain_match._MATCH_CACHE.clear()
    hubspot_schema._SCHEMA_CACHE.clear()
    domain_match._MATCH_CACHE[("alpha", "client.example")] = object()
    domain_match._MATCH_CACHE[("beta", "client.example")] = object()
    hubspot_schema._SCHEMA_CACHE["alpha"] = object()
    hubspot_schema._SCHEMA_CACHE["beta"] = object()
    with use_workspace("alpha"):
        domain_match.clear_match_cache()
        hubspot_schema.invalidate_cache()
    assert ("alpha", "client.example") not in domain_match._MATCH_CACHE
    assert ("beta", "client.example") in domain_match._MATCH_CACHE
    assert "alpha" not in hubspot_schema._SCHEMA_CACHE
    assert "beta" in hubspot_schema._SCHEMA_CACHE
    domain_match._MATCH_CACHE.clear()
    hubspot_schema._SCHEMA_CACHE.clear()


def test_hubspot_signal_directory_cache_is_scoped_to_workspace():
    from backend.signals import hubspot as signals

    signals._DIR_CACHE.clear()
    calls: list[str] = []

    class Client:
        token = "same-token"

        def owner_directory(self):
            slug = context.current_workspace()
            calls.append(slug)
            return {slug: {"name": slug}}

        def deal_stage_labels(self):
            return {context.current_workspace(): "open"}

    with use_workspace("alpha"):
        assert "alpha" in signals._cached_directories(Client())[0]
    with use_workspace("beta"):
        assert "beta" in signals._cached_directories(Client())[0]
    with use_workspace("alpha"):
        assert "alpha" in signals._cached_directories(Client())[0]
    assert calls == ["alpha", "beta"]
    signals._DIR_CACHE.clear()
