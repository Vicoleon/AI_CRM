"""Tests for the AI-Cost accounting layer (rubik_core.cost).

Covers the rate card / cost math, the feature-attribution context, the
best-effort buffered recorder (against an in-memory SQLite DB — never the real
Cloud SQL), and the instrumented genai-client wrapper. Pure/isolated: no
network, no GCP, no shared DB.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.cost import pricing
from rubik_core.cost.pricing import compute_cost_usd, is_priced
from rubik_core.cost.usage import (
    attribute_to,
    current_feature,
    default_feature,
    extract_usage,
    feature_context,
    record_usage,
)


# ── Pricing ──────────────────────────────────────────────────────────────
def test_cost_pro_input_and_output():
    # 1M input @ $1.25, 1M output @ $10.00 (default rate card).
    assert compute_cost_usd("gemini-2.5-pro", prompt_tokens=1_000_000) == 1.25
    assert compute_cost_usd("gemini-2.5-pro", completion_tokens=1_000_000) == 10.0


def test_cost_zero_when_no_tokens():
    assert compute_cost_usd("gemini-2.5-pro") == 0.0


def test_cached_tokens_are_discounted():
    full = compute_cost_usd("gemini-2.5-pro", prompt_tokens=10_000)
    cached = compute_cost_usd(
        "gemini-2.5-pro", prompt_tokens=10_000, cached_tokens=10_000
    )
    assert cached < full  # cached input bills at the discounted rate


def test_longest_prefix_match_beats_alias():
    # "gemini-2.5-flash-lite" must resolve to the lite entry, not "gemini-2.5-flash".
    lite = compute_cost_usd("gemini-2.5-flash-lite-002", completion_tokens=1_000_000)
    flash = compute_cost_usd("gemini-2.5-flash-002", completion_tokens=1_000_000)
    assert lite == 0.40
    assert flash == 2.50


def test_unknown_model_uses_fallback_not_zero():
    assert not is_priced("totally-made-up-model")
    # Fallback rate is non-zero so unpriced spend stays visible.
    assert compute_cost_usd("totally-made-up-model", completion_tokens=1_000_000) > 0


def test_configured_aliases_are_priced():
    assert is_priced("gemini-flash-latest")
    assert is_priced("gemini-3.1-flash-lite")
    # 3.7/3.8 Flash both serve on Vertex and 3.8 is the FLASH tier. Asserted as
    # literals because test_every_configured_tier_and_catalog_model_is_priced
    # follows the settings, so it would go quiet if a tier were rolled back.
    assert is_priced("gemini-3.8-flash")
    assert is_priced("gemini-3.7-flash")
    assert is_priced("text-embedding-004")


def test_every_configured_tier_and_catalog_model_is_priced():
    """An unpriced model is not a missing row — it silently bills at the
    mid-Flash fallback, which reported Lite spend at ~3x input / 6x output until
    the 3.5/3.6 ids were added. Anything reachable from config or the Admin →
    Chat dropdown must be on the rate card."""
    from rubik_core.agent_runtime.chat_config import CHAT_MODEL_CATALOG
    from rubik_core.config import settings

    reachable = {
        settings.VERTEX_LLM_MODEL_LITE,
        settings.VERTEX_LLM_MODEL_FLASH,
        settings.VERTEX_LLM_MODEL_PRO,
        *CHAT_MODEL_CATALOG,
    }
    unpriced = sorted(m for m in reachable if not is_priced(m))
    assert not unpriced, f"unpriced models bill at the fallback rate: {unpriced}"


def test_gemini_4_argon_is_priced_at_its_introductory_rate():
    """No tier pins Argon yet, but the PRO-tier replay benchmark prices every
    call through compute_cost_usd, and unpriced it would bill at the mid-Flash
    fallback: 7x under on input, 4x under on output."""
    assert is_priced("gemini-4-argon")
    assert compute_cost_usd("gemini-4-argon", prompt_tokens=1_000_000) == 2.00
    assert compute_cost_usd("gemini-4-argon", completion_tokens=1_000_000) == 10.00
    assert (
        compute_cost_usd(
            "gemini-4-argon", prompt_tokens=1_000_000, cached_tokens=1_000_000
        )
        == 0.10
    )
    # The new key must not change what the PRO tier bills at.
    assert compute_cost_usd("gemini-2.5-pro", prompt_tokens=1_000_000) == 1.25


def test_flash_lite_prefix_beats_flash_on_the_3x_family():
    """Same longest-prefix trap as 2.5: "gemini-3.5-flash-lite" must not resolve
    to the "gemini-3.5-flash" entry and bill at 6x the output rate."""
    assert (
        compute_cost_usd("gemini-3.5-flash-lite", completion_tokens=1_000_000) == 0.40
    )
    assert compute_cost_usd("gemini-3.5-flash", completion_tokens=1_000_000) == 2.50


def test_cost_never_raises_on_bad_input():
    assert compute_cost_usd("gemini-2.5-pro", prompt_tokens=-5) == 0.0
    assert compute_cost_usd("", prompt_tokens=100) >= 0.0


def test_env_override_merges_over_defaults(monkeypatch):
    monkeypatch.setenv("LLM_PRICING_JSON", '{"gemini-2.5-pro": {"output": 99.0}}')
    pricing.reset_rate_card_cache()
    try:
        # Overridden output rate, but input untouched by the partial merge.
        assert compute_cost_usd("gemini-2.5-pro", completion_tokens=1_000_000) == 99.0
        assert compute_cost_usd("gemini-2.5-pro", prompt_tokens=1_000_000) == 1.25
    finally:
        monkeypatch.delenv("LLM_PRICING_JSON", raising=False)
        pricing.reset_rate_card_cache()


# ── extract_usage ─────────────────────────────────────────────────────────
class _FakeUsage:
    prompt_token_count = 120
    candidates_token_count = 30
    total_token_count = 150
    cached_content_token_count = 20


class _FakeResponse:
    usage_metadata = _FakeUsage()
    text = "{}"


def test_extract_usage_reads_all_fields():
    u = extract_usage(_FakeResponse())
    assert u == {
        "prompt": 120,
        "completion": 30,
        "thinking": 0,
        "total": 150,
        "cached": 20,
    }


def test_extract_usage_handles_missing_metadata():
    class Empty:
        pass

    assert extract_usage(Empty()) == {
        "prompt": 0,
        "completion": 0,
        "thinking": 0,
        "total": 0,
        "cached": 0,
    }


# ── Attribution context ────────────────────────────────────────────────────
def test_feature_context_sets_and_restores():
    assert current_feature.get() == "unattributed"
    with feature_context("chat"):
        assert current_feature.get() == "chat"
    assert current_feature.get() == "unattributed"


def test_attribute_to_decorator_sync_and_async():
    @attribute_to("meeting_prep")
    def sync_fn():
        return current_feature.get()

    @attribute_to("deep_prep")
    async def async_fn():
        return current_feature.get()

    assert sync_fn() == "meeting_prep"
    assert asyncio.run(async_fn()) == "deep_prep"
    assert current_feature.get() == "unattributed"  # restored


# ── Buffered recorder (in-memory SQLite) ────────────────────────────────────
@pytest.fixture
def mem_db(monkeypatch):
    """Point the recorder's SessionLocal at a throwaway in-memory DB."""
    import rubik_core.cost.usage as usage_mod
    import rubik_core.database.db as dbmod
    from rubik_core.database.models import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(dbmod, "SessionLocal", TestSession)

    # Start each test with a clean buffer AND a fresh flush clock.
    #
    # _last_flush_monotonic is set once at import time, so in any suite running
    # longer than _FLUSH_INTERVAL_S (30s) the first record_usage() here sees an
    # "overdue" interval and spawns the daemon ai-cost-flush thread. That thread
    # drains _BUFFER, so the test's own flush_usage() finds it empty and returns
    # 0 — a race the test loses intermittently, and loses more often under
    # `pytest -n 2` where two workers contend for the runner's two cores.
    # Resetting the clock keeps the interval branch false for the test's
    # duration, so the only flush is the one the test performs itself.
    import time as _time

    with usage_mod._LOCK:
        usage_mod._BUFFER.clear()
        usage_mod._last_flush_monotonic = _time.monotonic()
    yield TestSession
    with usage_mod._LOCK:
        usage_mod._BUFFER.clear()


def test_record_and_flush_writes_row(mem_db):
    from rubik_core.cost.usage import flush_usage
    from rubik_core.database.models import LLMUsageEvent

    with feature_context("meeting_prep", user_email="a@b.com"):
        record_usage(
            operation="generate",
            model="gemini-2.5-pro",
            prompt_tokens=1000,
            completion_tokens=500,
        )
    written = flush_usage()
    assert written == 1

    session = mem_db()
    try:
        rows = session.query(LLMUsageEvent).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.feature == "meeting_prep"
        assert row.user_email == "a@b.com"
        assert row.total_tokens == 1500
        assert row.cost_usd == pytest.approx(
            compute_cost_usd("gemini-2.5-pro", 1000, 500)
        )
    finally:
        session.close()


def test_record_skips_zero_token_events(mem_db):
    from rubik_core.cost.usage import flush_usage

    record_usage(operation="generate", model="gemini-2.5-pro")  # no tokens
    assert flush_usage() == 0


def test_record_never_raises_when_tracking_disabled(monkeypatch, mem_db):
    from rubik_core.cost.usage import flush_usage

    monkeypatch.setenv("AI_COST_TRACKING_ENABLED", "false")
    record_usage(operation="generate", model="gemini-2.5-pro", prompt_tokens=100)
    assert flush_usage() == 0  # nothing recorded while disabled


# ── Instrumented client wrapper ─────────────────────────────────────────────
def test_instrumented_models_records_generate(mem_db):
    from rubik_core.cost.usage import flush_usage
    from rubik_core.database.models import LLMUsageEvent
    from rubik_core.models import _InstrumentedModels

    class FakeRealModels:
        def generate_content(self, *, model, contents, **kwargs):
            return _FakeResponse()

    wrapped = _InstrumentedModels(FakeRealModels())
    with feature_context("customer_xray"):
        resp = wrapped.generate_content(model="gemini-flash-latest", contents="hi")
    assert resp.text == "{}"  # passthrough of the real response
    flush_usage()

    session = mem_db()
    try:
        rows = session.query(LLMUsageEvent).all()
        assert len(rows) == 1
        assert rows[0].feature == "customer_xray"
        assert rows[0].model == "gemini-flash-latest"
        assert rows[0].total_tokens == 150
    finally:
        session.close()


# ── Regression: contextvar propagation across a ThreadPoolExecutor ──────────
# The priorities & X-Ray orchestrators fan channel agents out through a
# ContextThreadPoolExecutor, which copies contextvars at every submission.
# These tests pin BOTH the broken baseline and the fix.
def test_raw_threadpool_loses_feature_contextvar():
    import concurrent.futures

    with feature_context("ai_priorities"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            got = list(pool.map(lambda _: current_feature.get(), range(2)))
    # Raw submit drops the contextvar — this is the bug the fix works around.
    assert got == ["unattributed", "unattributed"]


def test_copy_context_propagates_feature_into_workers():
    import concurrent.futures
    import contextvars

    with feature_context("ai_priorities"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, current_feature.get)
                for _ in range(3)
            ]
            got = [f.result() for f in futures]
    assert got == ["ai_priorities", "ai_priorities", "ai_priorities"]


def test_orchestrators_use_context_aware_pool():
    # Guard against a future refactor silently reintroducing the raw submit.
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in [
        "rubik_core/priorities/orchestrator.py",
        "rubik_core/xray/orchestrator.py",
    ]:
        src = (root / rel).read_text()
        assert "ContextThreadPoolExecutor" in src, (
            f"{rel} must propagate context to workers"
        )


def test_xray_service_entry_points_are_attributed():
    # Customer X-Ray's true entry points are service.run_fast (Fast) and
    # service.run_deep_job (Deep — runs in a separate Cloud Run Job process).
    # Both must carry @attribute_to("customer_xray") or their spend lands in
    # "unattributed". Verify via the wrapped function's identity + source.
    import inspect

    from rubik_core.xray import service

    for fn_name in ("run_fast", "run_deep_job"):
        fn = getattr(service, fn_name)
        # @attribute_to wraps the function (functools.wraps sets __wrapped__).
        assert hasattr(fn, "__wrapped__"), f"service.{fn_name} must be decorated"
    src = inspect.getsource(service)
    assert src.count('@attribute_to("customer_xray")') >= 2


def test_priorities_batch_runner_attributes_run(monkeypatch):
    # The scheduled AI Priorities batch runs as a Cloud Run Job entry
    # (python -m rubik_core.priorities.batch_runner) — distinct from the
    # in-service path. Its run_batch call must execute under
    # feature_context("ai_priorities") or the whole nightly batch (the bulk of
    # priorities spend) records as "unattributed".
    import types

    from rubik_core.priorities import batch as batch_mod
    from rubik_core.priorities import batch_runner

    captured = {}

    def fake_run_batch(run_id, user_emails, per_user_timeout=None, report_kind=None):
        captured["feature"] = current_feature.get()
        return types.SimpleNamespace(
            run_id=run_id,
            users_total=len(user_emails),
            users_succeeded=len(user_emails),
            users_failed=0,
            total_duration_seconds=0.0,
        )

    monkeypatch.setattr(batch_mod, "run_batch", fake_run_batch)
    monkeypatch.setattr(batch_mod, "PER_USER_TIMEOUT_SECONDS", 1, raising=False)

    rc = batch_runner.main(["--target-email", "u@example.com"])
    assert rc == 0
    assert captured["feature"] == "ai_priorities"
    # And the context is restored afterward.
    assert current_feature.get() == "unattributed"


# ── Regression: LLM_PRICING_JSON per-entry validation ───────────────────────
def test_pricing_override_bad_entry_does_not_drop_others(monkeypatch):
    # A non-numeric value in one entry must NOT abort the whole merge or leave
    # a half-applied card: the good entry applies, the bad one is skipped, and
    # untouched models keep their defaults.
    monkeypatch.setenv(
        "LLM_PRICING_JSON",
        '{"gemini-2.5-flash": {"input": 0.99}, "gemini-2.5-pro": {"input": "tbd"}}',
    )
    pricing.reset_rate_card_cache()
    try:
        # good override applied
        assert compute_cost_usd("gemini-2.5-flash", prompt_tokens=1_000_000) == 0.99
        # bad entry skipped → pro keeps its default input rate
        assert compute_cost_usd("gemini-2.5-pro", prompt_tokens=1_000_000) == 1.25
    finally:
        monkeypatch.delenv("LLM_PRICING_JSON", raising=False)
        pricing.reset_rate_card_cache()


def test_pricing_override_ignores_unknown_keys(monkeypatch):
    # A descriptive/unknown key must not crash the entry.
    monkeypatch.setenv(
        "LLM_PRICING_JSON",
        '{"gemini-2.5-pro": {"output": 42.0, "note": "why we set this"}}',
    )
    pricing.reset_rate_card_cache()
    try:
        assert compute_cost_usd("gemini-2.5-pro", completion_tokens=1_000_000) == 42.0
    finally:
        monkeypatch.delenv("LLM_PRICING_JSON", raising=False)
        pricing.reset_rate_card_cache()


# ── Regression: unattributed events carry an origin hint ────────────────────
def test_unattributed_event_stamps_origin(mem_db):
    # A call with NO feature_context must record feature="unattributed" AND an
    # `origin` in metadata pointing at the caller, so the Admin bucket is
    # traceable. (Tagged calls must NOT get an origin.)
    from rubik_core.cost.usage import flush_usage
    from rubik_core.database.models import LLMUsageEvent

    # unattributed (no context)
    record_usage(operation="generate", model="gemini-2.5-pro", prompt_tokens=100)
    # tagged
    with feature_context("chat"):
        record_usage(operation="generate", model="gemini-2.5-pro", prompt_tokens=100)
    flush_usage()

    session = mem_db()
    try:
        rows = {r.feature: r for r in session.query(LLMUsageEvent).all()}
        assert "unattributed" in rows
        origin = (rows["unattributed"].metadata_json or {}).get("origin")
        assert origin and "test_ai_cost" in origin, origin
        # Tagged row has no origin noise.
        assert (rows["chat"].metadata_json or {}).get("origin") is None
    finally:
        session.close()


# ── Regression: the AI-Priorities batch lost its tag at the thread hop ──────
# The scheduled batch was nearly all "unattributed" spend (99.98% of a
# measured month, ~5,875 calls). batch_runner DID
# set feature_context("ai_priorities"), but batch.run_user_with_timeout then
# handed each user to a raw ThreadPoolExecutor, which does not copy
# contextvars — so every LLM call the pipeline made ran back at the
# "unattributed" default. test_priorities_batch_runner_attributes_run above
# missed it because it stubs run_batch, asserting the tag at the boundary just
# BEFORE the hop that drops it. These tests follow the tag through the hop.
def test_batch_run_priorities_for_user_is_decorated():
    # Structural guard: the decorator must be on the function that does the
    # work, not only on the Job entrypoint — a decorator is re-applied inside
    # whatever thread runs it, an entry-point contextvar is not.
    from rubik_core.priorities import batch as batch_mod

    assert hasattr(batch_mod.run_priorities_for_user, "__wrapped__"), (
        "batch.run_priorities_for_user must carry @attribute_to; without it the "
        "whole scheduled batch records as unattributed"
    )


def test_run_user_with_timeout_tags_worker_thread(monkeypatch):
    # The real regression: what feature does the pipeline observe, in the
    # thread it actually runs in? Must be "ai_priorities" even with NO outer
    # context, because the decorator re-establishes it past the hop.
    from rubik_core.priorities import batch as batch_mod

    seen: dict[str, str] = {}

    def fake_inner(user_email, report_kind="daily"):
        seen["feature"] = current_feature.get()
        return batch_mod.PrioritiesUserResult(user_email=user_email, succeeded=True)

    monkeypatch.setattr(batch_mod, "run_priorities_for_user", fake_inner)
    # No surrounding feature_context at all — mimics the worker thread having
    # lost it. The context-aware pool carries whatever the caller had.
    with feature_context("ai_priorities"):
        result = batch_mod.run_user_with_timeout("u@example.com", timeout_seconds=5)
    assert result.succeeded
    assert seen["feature"] == "ai_priorities"


def test_run_user_with_timeout_uses_context_aware_pool():
    # Guard against a refactor returning to a raw submit.
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "rubik_core/priorities/batch.py").read_text()
    assert "ContextThreadPoolExecutor" in src, (
        "batch.run_user_with_timeout must propagate context into the worker"
    )


def test_decorated_pipeline_tags_calls_made_in_a_worker_thread(mem_db):
    # End-to-end at the recorder: a decorated function submitted to a RAW
    # ThreadPoolExecutor (worst case, no copy_context) still records its spend
    # under the right feature. This is what makes the fix robust rather than
    # dependent on every future fan-out remembering copy_context.
    import concurrent.futures

    from rubik_core.cost.usage import flush_usage
    from rubik_core.database.models import LLMUsageEvent

    @attribute_to("ai_priorities")
    def work():
        record_usage(operation="generate", model="gemini-2.5-pro", prompt_tokens=500)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(work).result()
    flush_usage()

    session = mem_db()
    try:
        features = [r.feature for r in session.query(LLMUsageEvent).all()]
        assert features == ["ai_priorities"], features
    finally:
        session.close()


# ── default_feature: fill the blank, never override a caller ────────────────
def test_default_feature_fills_in_when_untagged():
    with default_feature("web_search"):
        assert current_feature.get() == "web_search"
    assert current_feature.get() == "unattributed"


def test_default_feature_never_overrides_an_owning_feature():
    # A web search issued during a chat turn is CHAT spend. Stamping it
    # "web_search" would rob Chat of its true cost.
    with feature_context("chat"):
        with default_feature("web_search"):
            assert current_feature.get() == "chat"
        assert current_feature.get() == "chat"


def test_attribute_to_only_if_unset_matches_default_feature():
    @attribute_to("web_search", only_if_unset=True)
    def leaf():
        return current_feature.get()

    assert leaf() == "web_search"
    with feature_context("chat"):
        assert leaf() == "chat"


def test_attribute_to_default_works_on_async(monkeypatch):
    @attribute_to("hubspot_retrieval", only_if_unset=True)
    async def leaf():
        return current_feature.get()

    assert asyncio.run(leaf()) == "hubspot_retrieval"

    async def under_chat():
        with feature_context("chat"):
            return await leaf()

    assert asyncio.run(under_chat()) == "chat"


def test_shared_leaves_are_fallback_tagged_not_hard_tagged():
    # HubSpot _plan inherits the caller. Chat web search has its own model
    # route and AI Cost category, so it must override the outer Chat context.
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    web_search = (root / "rubik_core/adk_agent/tools.py").read_text()
    assert '@attribute_to("web_search")' in web_search
    hubspot = (root / "rubik_core/retrieval/hubspot_api.py").read_text()
    assert '@attribute_to("hubspot_retrieval", only_if_unset=True)' in hubspot


# ── Admin: the Unattributed row explains itself ─────────────────────────────
def test_unattributed_origins_groups_by_code_path(mem_db):
    # The Admin tab's "Unattributed" row was a black box: one big number that
    # named no cause. It now expands into the origins the recorder stamps, so
    # an untagged path points at itself. Tagged rows must not leak in.
    import uuid
    from datetime import datetime

    from backend.routers.admin import _unattributed_origins
    from rubik_core.database.models import LLMUsageEvent

    session = mem_db()
    try:
        # Rows are written directly rather than through record_usage: the
        # recorder derives `origin` from the live call stack, so every event it
        # writes here would point at this test file instead of the distinct
        # code paths the grouping needs to be exercised with.
        def _row(feature: str, origin: str | None, tokens: int, cost: float):
            session.add(
                LLMUsageEvent(
                    id=str(uuid.uuid4()),
                    timestamp=datetime.utcnow(),
                    feature=feature,
                    operation="generate",
                    model="gemini-2.5-pro",
                    prompt_tokens=tokens,
                    total_tokens=tokens,
                    cost_usd=cost,
                    metadata_json={"origin": origin} if origin else None,
                )
            )

        _row("unattributed", "mod.a:1", 1000, 1.0)
        _row("unattributed", "mod.a:1", 1000, 1.0)
        _row("unattributed", "mod.b:2", 100, 0.1)
        # A row with no breadcrumb (predates origin stamping) still gets a bucket.
        _row("unattributed", None, 50, 0.05)
        # Tagged spend must never leak into the breakdown.
        _row("chat", None, 9999, 50.0)
        session.commit()

        out = _unattributed_origins(session.query(LLMUsageEvent))
        by_origin = {o["origin"]: o for o in out}
        # Chat's spend is excluded entirely — 4 unattributed rows, 3 buckets.
        assert sum(o["calls"] for o in out) == 4
        assert by_origin["mod.a:1"]["calls"] == 2
        assert by_origin["mod.a:1"]["total_tokens"] == 2000
        assert by_origin["mod.b:2"]["calls"] == 1
        # The origin-less row is bucketed, not dropped — real spend is real.
        assert "(no origin recorded)" in by_origin
        # Sorted most-expensive-first so the top line is the thing to go fix.
        assert out[0]["origin"] == "mod.a:1"
    finally:
        session.close()


# ── Thinking tokens (Vertex ``thoughts_token_count``) ────────────────────────
# Vertex reports a thinking model's reasoning as its own slice of the usage
# (total == prompt + candidates + thoughts, verified 2026-10-14) and bills it at
# the OUTPUT rate. The ledger used to read candidates only, so a PRO call with
# dynamic thinking was priced at a quarter or less of its bill. The figures
# below are the Priorities decision_maker call from that day's replay smoke run.
class _ThinkingUsage:
    prompt_token_count = 0
    candidates_token_count = 395
    thoughts_token_count = 5_382
    total_token_count = 5_777
    cached_content_token_count = 0


class _ThinkingResponse:
    usage_metadata = _ThinkingUsage()
    text = "{}"


def test_thinking_tokens_are_priced_at_the_output_rate():
    # gemini-2.5-pro output is $10 per 1M tokens on the default rate card.
    assert compute_cost_usd("gemini-2.5-pro", thinking_tokens=1_000_000) == 10.0
    answer_only = compute_cost_usd("gemini-2.5-pro", completion_tokens=395)
    with_thinking = compute_cost_usd(
        "gemini-2.5-pro", completion_tokens=395, thinking_tokens=5_382
    )
    assert answer_only == pytest.approx(0.00395)
    assert with_thinking == pytest.approx((395 + 5_382) * 10.0 / 1_000_000)
    # Embeddings have no output rate, so thinking can never cost them anything.
    assert compute_cost_usd("text-embedding-005", thinking_tokens=1_000) == 0.0


def test_extract_usage_reads_thinking_as_its_own_slice():
    u = extract_usage(_ThinkingResponse())
    assert u["completion"] == 395  # the visible answer, unchanged
    assert u["thinking"] == 5_382
    assert u["total"] == u["prompt"] + u["completion"] + u["thinking"]


def test_extract_usage_total_fallback_includes_thinking():
    class NoTotal:
        prompt_token_count = 100
        candidates_token_count = 10
        thoughts_token_count = 40
        total_token_count = None

    class Resp:
        usage_metadata = NoTotal()

    assert extract_usage(Resp())["total"] == 150


def test_record_usage_prices_thinking_and_keeps_its_own_count(mem_db):
    from rubik_core.cost.usage import THINKING_TOKENS_KEY, flush_usage
    from rubik_core.database.models import LLMUsageEvent

    with feature_context("ai_priorities"):
        record_usage(
            operation="generate",
            model="gemini-2.5-pro",
            completion_tokens=395,
            thinking_tokens=5_382,
        )
    assert flush_usage() == 1

    session = mem_db()
    try:
        row = session.query(LLMUsageEvent).one()
        assert row.completion_tokens == 395  # still the visible answer
        assert row.metadata_json == {THINKING_TOKENS_KEY: 5_382}
        assert row.total_tokens == 395 + 5_382
        assert row.cost_usd == pytest.approx(
            compute_cost_usd("gemini-2.5-pro", 0, 395, 0, 5_382)
        )
    finally:
        session.close()


def test_record_usage_thinking_merges_with_other_metadata(mem_db):
    from rubik_core.cost.usage import THINKING_TOKENS_KEY, flush_usage
    from rubik_core.database.models import LLMUsageEvent

    record_usage(
        operation="generate",
        model="gemini-2.5-pro",
        prompt_tokens=10,
        thinking_tokens=0,
        feature="chat_impersonation",
        metadata={"impersonating": "x@y.com"},
    )
    record_usage(operation="embed", model="text-embedding-005", prompt_tokens=10)
    flush_usage()

    session = mem_db()
    try:
        rows = {r.operation: r for r in session.query(LLMUsageEvent).all()}
        # A measured zero is stamped, so the call counts as measured.
        assert rows["generate"].metadata_json == {
            "impersonating": "x@y.com",
            THINKING_TOKENS_KEY: 0,
        }
        # Embeddings never think, so they carry no thinking count at all.
        assert not (rows["embed"].metadata_json or {}).get(THINKING_TOKENS_KEY)
    finally:
        session.close()


def test_record_usage_counts_a_thinking_only_call(mem_db):
    # A call whose whole output was thinking (an empty answer) is still spend.
    from rubik_core.cost.usage import flush_usage

    record_usage(operation="generate", model="gemini-2.5-pro", thinking_tokens=500)
    assert flush_usage() == 1


def test_instrumented_models_records_thinking_cost(mem_db):
    from rubik_core.cost.usage import THINKING_TOKENS_KEY, flush_usage
    from rubik_core.database.models import LLMUsageEvent
    from rubik_core.models import _InstrumentedAsyncModels, _InstrumentedModels

    class FakeRealModels:
        def generate_content(self, *, model, contents, **kwargs):
            return _ThinkingResponse()

    class FakeRealAsyncModels:
        async def generate_content(self, *, model, contents, **kwargs):
            return _ThinkingResponse()

    with feature_context("ai_priorities"):
        _InstrumentedModels(FakeRealModels()).generate_content(
            model="gemini-2.5-pro", contents="hi"
        )
        asyncio.run(
            _InstrumentedAsyncModels(FakeRealAsyncModels()).generate_content(
                model="gemini-2.5-pro", contents="hi"
            )
        )
    flush_usage()

    expected = compute_cost_usd(
        "gemini-2.5-pro", completion_tokens=395, thinking_tokens=5_382
    )
    session = mem_db()
    try:
        rows = session.query(LLMUsageEvent).all()
        assert len(rows) == 2
        for row in rows:
            assert row.completion_tokens == 395
            assert row.metadata_json[THINKING_TOKENS_KEY] == 5_382
            assert row.cost_usd == pytest.approx(expected)
    finally:
        session.close()


def test_scheduled_task_cost_record_carries_thinking(mem_db):
    # The ADK coordinator runs outside the shared client, so its tokens are
    # recorded from the orchestration result: thinking must ride along.
    from types import SimpleNamespace

    from rubik_core.cost.usage import THINKING_TOKENS_KEY, flush_usage
    from rubik_core.database.models import LLMUsageEvent
    from rubik_core.orchestration.models import OrchestrationResult
    from rubik_core.scheduled_tasks.runner import _record_cost

    assert OrchestrationResult.model_fields["thinking_tokens"].default == 0
    result = SimpleNamespace(
        prompt_tokens=1_000,
        completion_tokens=200,
        thinking_tokens=800,
        total_tokens=2_000,
    )
    _record_cost(SimpleNamespace(model_id="gemini-2.5-pro"), result, "a@b.com", "req-1")
    flush_usage()

    session = mem_db()
    try:
        row = session.query(LLMUsageEvent).one()
        assert row.feature == "scheduled_tasks"
        assert row.metadata_json[THINKING_TOKENS_KEY] == 800
        assert row.cost_usd == pytest.approx(
            compute_cost_usd("gemini-2.5-pro", 1_000, 200, 0, 800)
        )
    finally:
        session.close()


def test_every_generate_cost_record_passes_thinking_tokens():
    """Every ``record_usage(operation="generate", ...)`` outside the tests must
    pass ``thinking_tokens``, or that path prices only the visible answer again.
    The ADK coordinator reads ``thoughts_token_count`` in both of its exits."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    missing = []
    for top in ("rubik_core", "backend"):
        for path in (root / top).rglob("*.py"):
            if "tests" in path.parts:
                continue
            src = path.read_text()
            if "record_usage(" not in src:
                continue
            for node in ast.walk(ast.parse(src)):
                if not (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", None))
                    == "record_usage"
                ):
                    continue
                kw = {k.arg: k.value for k in node.keywords}
                op = kw.get("operation")
                if (
                    isinstance(op, ast.Constant)
                    and op.value == "generate"
                    and "thinking_tokens" not in kw
                ):
                    missing.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not missing, f"generate cost records without thinking_tokens: {missing}"

    # The ADK coordinator reads usage through extract_usage (which reads
    # thoughts_token_count), summed over the turn by _TurnUsage in both exits.
    adk = (root / "rubik_core/orchestration/adk_orchestrator.py").read_text()
    assert adk.count("turn_usage.add(event)") == 2
    hubspot_only = (root / "rubik_core/agent_runtime/hubspot_only_chat.py").read_text()
    assert "thoughts_token_count" in hubspot_only


def test_adk_coordinator_cost_records_pass_cached_tokens():
    """The coordinator runs with a context cache, so the sites that book its
    turns must pass ``cached_tokens`` or every cache hit is priced at the full
    input rate."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sites = 0
    missing = []
    for rel in ("backend/routers/chat.py", "rubik_core/scheduled_tasks/runner.py"):
        for node in ast.walk(ast.parse((root / rel).read_text())):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None))
                == "record_usage"
            ):
                continue
            sites += 1
            if "cached_tokens" not in {k.arg for k in node.keywords}:
                missing.append(f"{rel}:{node.lineno}")
    assert sites == 3  # chat aggregate fallback + per-agent rows, and scheduled tasks
    assert not missing, f"coordinator cost records without cached_tokens: {missing}"


# ── ADK coordinator: usage summed over every model call in a turn ────────────


def _usage(prompt: int, answer: int, thinking: int, cached: int):
    from google.genai import types

    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt,
        candidates_token_count=answer,
        thoughts_token_count=thinking,
        cached_content_token_count=cached,
        total_token_count=prompt + answer + thinking,
    )


def _adk_turn_events(*, streaming: bool) -> list:
    """One coordinator turn as ADK 1.30 emits it, with real ADK events: model
    call 1 asks for a tool, the tool answers (no usage), model call 2 writes
    the answer. Each model call's usage rides on its own non-partial event.

    In SSE mode each call is also preceded by partial chunk events that carry
    that chunk's usage (``StreamingResponseAggregator.process_response``); they
    are deliberately large so counting them would be obvious.
    """
    from google.adk.events import Event
    from google.genai import types

    def model_event(parts, usage, *, partial=False):
        return Event(
            author="coordinator",
            content=types.ModelContent(parts=parts),
            usage_metadata=usage,
            partial=partial or None,
        )

    def ask_tool():
        return [types.Part.from_function_call(name="lookup", args={"q": "x"})]

    call_1 = model_event(ask_tool(), _usage(1_000, 20, 300, 800))
    tool = Event(
        author="coordinator",
        content=types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name="lookup", response={"status": "success"}
                )
            ],
        ),
    )
    call_2 = model_event(
        [types.Part.from_text(text="The answer.")], _usage(1_500, 60, 200, 800)
    )
    if not streaming:
        return [call_1, tool, call_2]

    def chunk(parts):
        return model_event(parts, _usage(9_000, 900, 900, 9_000), partial=True)

    return [
        chunk(ask_tool()),
        call_1,
        tool,
        chunk([types.Part.from_text(text="The ")]),
        chunk([types.Part.from_text(text="answer.")]),
        call_2,
    ]


def _run_adk_turn(monkeypatch, events, *, streaming: bool, model_route=None):
    from unittest.mock import MagicMock

    from rubik_core.auth.registry import UserProfile
    from rubik_core.orchestration import adk_orchestrator as ao

    class _Runner:
        def __init__(self, **kwargs):
            pass

        async def run_async(self, **kwargs):
            for event in events:
                if isinstance(event, Exception):
                    raise event
                yield event

    class _Session:
        id = "sess"
        events: list = []

    class _Sessions:
        async def get_session(self, **kwargs):
            return _Session()

        async def create_session(self, **kwargs):
            return _Session()

    monkeypatch.setattr(ao.settings, "CHAT_CONTEXT_CACHE_ENABLED", False)
    monkeypatch.setattr(ao, "Runner", _Runner)
    monkeypatch.setattr(ao, "get_session_service", lambda: _Sessions())
    monkeypatch.setattr(ao, "build_coordinator", lambda **kwargs: MagicMock())
    if model_route is not None:
        monkeypatch.setattr(
            "rubik_core.ai_providers.adk_model.resolve_adk_route",
            lambda feature, **kw: model_route,
        )
    user = UserProfile(
        id="t",
        email="test@acme.example",
        name="Test",
        department="sales",
        roles=["user"],
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    orch = ao.ADKOrchestrator(user=user, timezone="UTC")
    if not streaming:
        return asyncio.run(orch.run(request_id="t", query="hi", history=[]))

    async def collect():
        return [
            ev
            async for ev in orch.run_streaming(request_id="t", query="hi", history=[])
        ]

    return asyncio.run(collect())


# Both model calls of _adk_turn_events, summed.
_TURN_PROMPT = 1_000 + 1_500
_TURN_ANSWER = 20 + 60
_TURN_THINKING = 300 + 200
_TURN_CACHED = 800 + 800
_TURN_TOTAL = (1_000 + 20 + 300) + (1_500 + 60 + 200)


def test_adk_run_sums_usage_over_every_model_call(monkeypatch):
    result = _run_adk_turn(
        monkeypatch, _adk_turn_events(streaming=False), streaming=False
    )

    assert result.answer == "The answer."
    assert result.prompt_tokens == _TURN_PROMPT
    assert result.completion_tokens == _TURN_ANSWER
    assert result.thinking_tokens == _TURN_THINKING
    assert result.cached_tokens == _TURN_CACHED
    assert result.total_tokens == _TURN_TOTAL


def test_adk_run_streaming_sums_model_calls_and_skips_partial_chunks(monkeypatch):
    out = _run_adk_turn(monkeypatch, _adk_turn_events(streaming=True), streaming=True)

    assert [e["text"] for e in out if e["type"] == "token"] == ["The ", "answer."]
    done = out[-1]
    assert done["type"] == "done"
    assert done["answer"] == "The answer."
    assert done["prompt_tokens"] == _TURN_PROMPT
    assert done["completion_tokens"] == _TURN_ANSWER
    assert done["thinking_tokens"] == _TURN_THINKING
    assert done["cached_tokens"] == _TURN_CACHED
    assert done["total_tokens"] == _TURN_TOTAL


def test_adk_turn_with_no_usage_reports_zero(monkeypatch):
    from google.adk.events import Event
    from google.genai import types

    answer = Event(
        author="coordinator",
        content=types.ModelContent(parts=[types.Part.from_text(text="Hi.")]),
    )
    result = _run_adk_turn(monkeypatch, [answer], streaming=False)

    assert result.answer == "Hi."
    assert (result.prompt_tokens, result.total_tokens, result.cached_tokens) == (
        0,
        0,
        0,
    )


def test_selected_provider_stream_error_never_exposes_secret_or_prompt(monkeypatch):
    from rubik_core.ai_providers.registry import ModelRoute

    route = ModelRoute("test", "chat_adk", "openai", "gpt-6-luna", "client")
    events = [RuntimeError("key=private-secret prompt=customer-contract")]
    output = _run_adk_turn(monkeypatch, events, streaming=True, model_route=route)
    assert output[-1]["type"] == "error"
    assert "selected openai model" in output[-1]["message"]
    assert "private-secret" not in str(output)
    assert "customer-contract" not in str(output)


def test_stream_error_reports_usage_from_completed_model_call(monkeypatch, caplog):
    from google.adk.events import Event
    from google.genai import types

    from rubik_core.ai_providers.registry import ModelRoute

    route = ModelRoute("test", "chat_adk", "openai", "gpt-6-luna", "client")
    spent = Event(
        author="coordinator",
        content=types.ModelContent(
            parts=[types.Part.from_function_call(name="lookup", args={})]
        ),
        usage_metadata=_usage(25, 5, 0, 0),
    )
    output = _run_adk_turn(
        monkeypatch,
        [spent, RuntimeError("private-secret customer-contract")],
        streaming=True,
        model_route=route,
    )
    failure = output[-1]
    assert failure["type"] == "error"
    assert failure["total_tokens"] == 30
    assert failure["usage_by_agent"]["coordinator"]["prompt"] == 25
    assert "private-secret" not in caplog.text
    assert "customer-contract" not in caplog.text


def test_nightly_pipeline_records_spent_tokens_when_later_stage_fails(monkeypatch):
    from types import SimpleNamespace

    from backend.routers import jobs
    from rubik_core.ai_providers.registry import ModelRoute
    from rubik_core.ai_providers.runtime import ProviderRequestError
    from rubik_core.cost import usage as cost_usage

    route = ModelRoute("test", "nightly_pipeline", "openai", "gpt-6-luna", "client")
    recorded = []

    class FakeRunner:
        def __init__(self, **kwargs):
            pass

        async def run_async(self, **kwargs):
            yield SimpleNamespace(
                partial=False,
                usage_metadata=_usage(80, 12, 4, 10),
                is_final_response=lambda: False,
            )
            raise RuntimeError("private-secret customer-contract")

    class FakeSessions:
        async def create_session(self, **kwargs):
            return SimpleNamespace(id="nightly-test")

    monkeypatch.setattr(jobs, "resolve_adk_route", lambda *a, **kw: route)
    monkeypatch.setattr(
        "rubik_core.agent_runtime.nightly_pipeline.build_nightly_pipeline",
        lambda *a, **kw: object(),
    )
    monkeypatch.setattr(jobs, "get_session_service", lambda: FakeSessions())
    monkeypatch.setattr(jobs, "Runner", FakeRunner)
    monkeypatch.setattr(cost_usage, "record_usage", lambda **kw: recorded.append(kw))
    with pytest.raises(ProviderRequestError, match="nightly pipeline"):
        asyncio.run(jobs._run_nightly_pipeline("owner@example.com"))
    assert len(recorded) == 1
    assert recorded[0]["prompt_tokens"] == 80
    assert recorded[0]["completion_tokens"] == 12
    assert recorded[0]["metadata"] == {
        "provider": "openai",
        "billing_owner": "client",
    }


def test_scheduled_task_cost_record_prices_cache_hits_at_the_cached_rate(mem_db):
    from types import SimpleNamespace

    from rubik_core.cost.usage import flush_usage
    from rubik_core.database.models import LLMUsageEvent
    from rubik_core.scheduled_tasks.runner import _record_cost

    model = "gemini-2.5-pro"
    result = SimpleNamespace(
        prompt_tokens=_TURN_PROMPT,
        completion_tokens=_TURN_ANSWER,
        thinking_tokens=_TURN_THINKING,
        cached_tokens=_TURN_CACHED,
        total_tokens=_TURN_TOTAL,
    )
    _record_cost(SimpleNamespace(model_id=model), result, "a@b.com", "req-1")
    flush_usage()

    session = mem_db()
    try:
        row = session.query(LLMUsageEvent).one()
        assert row.cached_tokens == _TURN_CACHED
        expected = compute_cost_usd(
            model, _TURN_PROMPT, _TURN_ANSWER, _TURN_CACHED, _TURN_THINKING
        )
        assert row.cost_usd == pytest.approx(expected)
        # The cache discount is real: the same turn priced as if uncached costs more.
        assert expected < compute_cost_usd(
            model, _TURN_PROMPT, _TURN_ANSWER, 0, _TURN_THINKING
        )
    finally:
        session.close()


def test_admin_report_shows_thinking_and_how_many_calls_were_measured(mem_db):
    import uuid
    from datetime import datetime
    from types import SimpleNamespace

    from backend.routers.admin import ai_cost_summary
    from rubik_core.cost.usage import THINKING_TOKENS_KEY
    from rubik_core.database.models import LLMUsageEvent

    session = mem_db()
    try:

        def _row(feature, operation, meta, completion=100):
            session.add(
                LLMUsageEvent(
                    id=str(uuid.uuid4()),
                    timestamp=datetime.utcnow(),
                    feature=feature,
                    operation=operation,
                    model="gemini-2.5-pro",
                    prompt_tokens=10,
                    completion_tokens=completion,
                    total_tokens=10 + completion,
                    cost_usd=0.01,
                    metadata_json=meta,
                )
            )

        _row("ai_priorities", "generate", {THINKING_TOKENS_KEY: 5_000})
        _row(
            "ai_priorities",
            "generate",
            {
                THINKING_TOKENS_KEY: 0,
                "provider": "anthropic",
                "billing_owner": "client",
            },
        )
        # Recorded before thinking was captured: no key, origin-only, or none.
        _row("ai_priorities", "generate", None)
        _row("unattributed", "generate", {"origin": "mod.a:1"})
        # An embedding is not a generate call and never counts as unmeasured.
        _row("dam", "embed", None, completion=0)
        session.commit()

        out = asyncio.run(
            ai_cost_summary(days=30, user=SimpleNamespace(is_admin=True), db=session)
        )
    finally:
        session.close()

    totals = out["totals"]
    assert totals["thinking_tokens"] == 5_000
    assert totals["completion_tokens"] == 400  # thinking is not folded in here
    assert totals["generate_calls"] == 4
    assert totals["thinking_measured_calls"] == 2
    by_feature = {f["feature"]: f for f in out["features"]}
    assert by_feature["ai_priorities"]["thinking_tokens"] == 5_000
    assert by_feature["unattributed"]["thinking_tokens"] == 0
    by_provider = {row["provider"]: row for row in out["providers"]}
    assert by_provider["anthropic"]["billing_owner"] == "client"
    assert by_provider["anthropic"]["calls"] == 1
    assert by_provider["vertex_managed"]["billing_owner"] == "cubie"
    assert by_provider["vertex_managed"]["calls"] == 4


def test_client_owned_provider_rates_do_not_use_vertex_fallback():
    assert is_priced("claude-sonnet-4-6", provider="anthropic")
    assert (
        compute_cost_usd(
            "claude-sonnet-4-6", prompt_tokens=1_000_000, provider="anthropic"
        )
        == 3.0
    )
    assert (
        compute_cost_usd("muse-spark-1.3", completion_tokens=1_000_000, provider="meta")
        == 4.25
    )
    assert not is_priced("unknown", provider="meta")
