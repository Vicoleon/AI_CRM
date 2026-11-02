"""Evidence gathering for the Ingestion Council.

One gatherer per :class:`RunKind`. Each turns the state an ingestion left behind
into a :class:`RunEvidence`: aggregate ``stats``, structural ``samples``
(presence and counts, never source prose), and the deterministic
``cross_checks`` the auditors reason about but can't overrule.

Two deliberate constraints:

* **No module-level session/engine.** Every query runs on the caller's
  ``Session``. A module-level ``SessionLocal`` bound at import is exactly what
  once leaked test writes into dev Cloud SQL — we don't repeat it.
* **Each gatherer is isolated.** A single failing query degrades to a noted
  ``RunEvidence`` (with a failed cross-check) instead of taking the whole run
  down — the same "one bad plugin doesn't fail the run" ethos as the overnight
  orchestrator.

Chunk-table counts are collected ONLY in LOCAL (the Chroma adapter writes them).
In GCP the vector store is Vertex RAG, which chunks server-side, so the local
``chunks`` table is empty by design for ingested docs — those counts are omitted
there so an empty table can't read as a false "unretrievable" failure. The
reliable, mode-independent transcript invariants are failed/stuck status, a
present ACL row, and a NULL department.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.config import settings
from rubik_core.database.models import (
    AgentRecommendation,
    Asset,
    Chunk,
    HomeCard,
    MeetingParticipant,
)
from rubik_core.ingestion_council.models import CrossCheck, RunEvidence, RunKind
from rubik_core.priorities.models import (
    GENERATION_FILTERED_EMPTY,
    GENERATION_NO_SIGNALS,
)

logger = logging.getLogger(__name__)

# The nightly council runs ~08:00 UTC, after the 02:00/06:00/07:00 ingestions;
# 26h covers "last night's run" with margin for a late job or a re-run.
NIGHTLY_WINDOW_HOURS = 26
# The EOS sweep is weekly (Mon 01:00 UTC); a week + a day of slack.
WEEKLY_WINDOW_HOURS = 24 * 8


def _local(email: str | None) -> str:
    """Local-part of an email — enough to identify a user in an ops report
    without splashing full addresses across a Slack channel."""
    return (email or "?").split("@")[0]


def _overnight_sales_evidence(db: Session, now: datetime) -> RunEvidence:
    window = NIGHTLY_WINDOW_HOURS
    cutoff = now - timedelta(hours=window)
    ev = RunEvidence(kind=RunKind.OVERNIGHT_SALES, window_hours=window)

    recs = (
        db.query(AgentRecommendation)
        .filter(AgentRecommendation.created_at >= cutoff)
        .order_by(AgentRecommendation.created_at.desc())
        .all()
    )
    # Historical CRM recommendations may have been written before a workspace
    # pipeline was narrowed. Recheck the same live deal fence as the user-facing
    # reader before any title/text reaches the council's samples or delivery.
    from backend.routers.agents import _visible_recommendations

    recs = _visible_recommendations(recs)
    by_type: dict[str, int] = {}
    by_plugin: dict[str, int] = {}
    run_ids: set[str] = set()
    empty_text = 0
    zero_conf = 0
    missing_run_id = 0
    users: set[str] = set()
    for r in recs:
        by_type[r.type] = by_type.get(r.type, 0) + 1
        meta = r.source_metadata if isinstance(r.source_metadata, dict) else {}
        plugin = str(meta.get("plugin") or "unknown")
        by_plugin[plugin] = by_plugin.get(plugin, 0) + 1
        if meta.get("run_id"):
            run_ids.add(str(meta["run_id"]))
        else:
            missing_run_id += 1
        if not (r.recommendation_text or "").strip():
            empty_text += 1
        if float(r.confidence or 0.0) <= 0.0:
            zero_conf += 1
        users.add(r.user_email)

    ev.stats = {
        "recommendations_written": len(recs),
        "distinct_users": len(users),
        "distinct_run_ids": len(run_ids),
        "by_type": by_type,
        "by_plugin": by_plugin,
        "empty_text": empty_text,
        "zero_confidence": zero_conf,
        "missing_run_id": missing_run_id,
    }
    ev.samples = [
        {
            "type": r.type,
            "confidence": round(float(r.confidence or 0.0), 2),
            "user": _local(r.user_email),
            # Recommendation prose was generated from CRM signals. Its source
            # scope was not stored, so it cannot enter a durable LLM report.
            "text_present": bool((r.recommendation_text or "").strip()),
        }
        for r in recs[:3]
    ]
    ev.cross_checks = [
        CrossCheck(
            "no empty recommendation text",
            empty_text == 0,
            f"{empty_text} rec(s) with blank text",
        ),
        CrossCheck(
            "every rec carries a run_id",
            missing_run_id == 0,
            f"{missing_run_id} rec(s) missing source_metadata.run_id",
        ),
    ]
    # Zero recs is AMBIGUOUS, not a deterministic failure — kept as a note, not a
    # failed cross-check, so the chairman doesn't escalate a quiet/de-duped night
    # to "catastrophic". The agent de-dups against a
    # 30-day horizon, so a quiet pipeline or a recent prior run legitimately
    # writes 0; only a corroborated no-execution is a real failure.
    if not recs:
        ev.notes.append(
            "0 recommendations written in the window — ambiguous, not proof of "
            "failure (30-day de-dup horizon; quiet pipeline or recent prior run "
            "writes 0). Corroborate against the 02:00 job's execution before "
            "calling it a failed run."
        )
    if zero_conf:
        ev.notes.append(f"{zero_conf} recommendation(s) surfaced with confidence 0.0")
    return ev


def _priorities_daily_evidence(db: Session, now: datetime) -> RunEvidence:
    window = NIGHTLY_WINDOW_HOURS
    cutoff = now - timedelta(hours=window)
    ev = RunEvidence(kind=RunKind.PRIORITIES_DAILY, window_hours=window)

    # The batch's own roster, read from the same function, so the council never
    # counts users the batch never generates for and falsely reports them
    # "missing".
    from rubik_core.priorities.batch import priorities_roster

    active = set(priorities_roster(db))
    cards = (
        db.query(HomeCard)
        .filter(HomeCard.created_at >= cutoff)
        .filter(HomeCard.category == "daily_briefing")
        .all()
    )
    covered: set[str] = set()
    empty_bundles = 0
    no_signals_bundles = 0
    filtered_empty_bundles = 0
    counts: list[int] = []
    for c in cards:
        covered.add(c.user_email)
        md = c.metadata_json if isinstance(c.metadata_json, dict) else {}
        n = len(md.get("priorities") or [])
        counts.append(n)
        if n == 0:
            # Two legitimate empties: "no_signals" (genuinely quiet day, no
            # channel errors) and "filtered_empty" (decision items all dropped
            # by the temporal/memory post-filters — the user handled
            # everything). Anything else empty is a defect: either a legacy
            # card from before the empty-briefing delivery gate, or a bug.
            status = md.get("generation_status")
            if status == GENERATION_NO_SIGNALS:
                no_signals_bundles += 1
            elif status == GENERATION_FILTERED_EMPTY:
                filtered_empty_bundles += 1
            else:
                empty_bundles += 1
    missing = sorted(active - covered)

    ev.stats = {
        "active_users": len(active),
        "users_with_cards": len(covered),
        "coverage_pct": round(100 * len(covered & active) / len(active))
        if active
        else 0,
        "missing_users": len(missing),
        "missing_sample": [_local(e) for e in missing[:10]],
        "empty_bundles": empty_bundles,
        "no_signals_bundles": no_signals_bundles,
        "filtered_empty_bundles": filtered_empty_bundles,
        "avg_priorities_per_user": round(sum(counts) / len(counts), 1) if counts else 0,
    }
    ev.samples = [
        {
            "user": _local(c.user_email),
            "date": c.date,
            "priorities": len((c.metadata_json or {}).get("priorities") or [])
            if isinstance(c.metadata_json, dict)
            else 0,
        }
        for c in cards[:3]
    ]
    ev.cross_checks = [
        CrossCheck(
            "briefing generation ran in window",
            len(cards) > 0,
            f"{len(cards)} cards created"
            if cards
            else "no daily_briefing cards in window — did the 06:00 run fire?",
        ),
        CrossCheck(
            "every active user has a daily briefing card",
            len(missing) == 0,
            (
                f"{len(missing)}/{len(active)} active users missing: "
                + ", ".join(_local(e) for e in missing[:8])
            )
            if missing
            else "full coverage",
        ),
        CrossCheck(
            "no empty priority bundles",
            empty_bundles == 0,
            f"{empty_bundles} card(s) generated with zero priorities"
            + (
                f" (+{no_signals_bundles} quiet-day, "
                f"+{filtered_empty_bundles} all-handled card(s) — "
                "legitimate empties, not counted)"
                if no_signals_bundles or filtered_empty_bundles
                else ""
            ),
        ),
    ]
    return ev


def _transcript_sync_evidence(db: Session, now: datetime) -> RunEvidence:
    window = NIGHTLY_WINDOW_HOURS
    cutoff = now - timedelta(hours=window)
    ev = RunEvidence(kind=RunKind.TRANSCRIPT_SYNC, window_hours=window)

    recent = db.query(Asset).filter(Asset.created_at >= cutoff).all()
    # The sync stamps transcripts topic "Meeting Transcripts"; match loosely so a
    # casing/wording drift doesn't hide them.
    transcripts = [a for a in recent if "transcript" in (a.topic or "").lower()]

    # Chunk rows only exist in LOCAL (the Chroma adapter writes them). In GCP the
    # vector store is Vertex RAG, which chunks server-side, so the local `chunks`
    # table is empty BY DESIGN for ingested docs. Collecting/reporting it there
    # reads as a false "100% unretrievable" failure, so we only gather it in
    # LOCAL.
    app_mode = getattr(settings.APP_MODE, "value", str(settings.APP_MODE))
    is_local = str(app_mode).lower() == "local"

    failed = stuck = missing_acl = dept_stamped = zero_chunks = 0
    samples: list[dict] = []
    stuck_cutoff = now - timedelta(hours=2)
    for a in transcripts:
        acl = (
            db.query(func.count(MeetingParticipant.participant_email))
            .filter(MeetingParticipant.doc_id == a.doc_id)
            .scalar()
            or 0
        )
        nchunks = None
        if is_local:
            nchunks = (
                db.query(func.count(Chunk.id)).filter(Chunk.doc_id == a.doc_id).scalar()
                or 0
            )
            if nchunks == 0:
                zero_chunks += 1
        if (a.status or "") == "failed":
            failed += 1
        if a.processing and a.created_at and a.created_at < stuck_cutoff:
            stuck += 1
        if acl == 0:
            missing_acl += 1
        if a.department:
            dept_stamped += 1
        if len(samples) < 4:
            sample = {
                "title_present": bool(a.original_filename or a.doc_id),
                "status": a.status,
                "acl_rows": int(acl),
                "department_present": bool(a.department),
                "allowed_emails": len(a.allowed_emails)
                if isinstance(a.allowed_emails, list)
                else 0,
            }
            if is_local:
                sample["chunk_rows"] = int(nchunks)
            samples.append(sample)

    ev.stats = {
        "transcripts_ingested": len(transcripts),
        "other_assets_ingested": len(recent) - len(transcripts),
        "failed": failed,
        "processing_stuck_gt_2h": stuck,
        "missing_acl_rows": missing_acl,
        "department_stamped": dept_stamped,
        "app_mode": str(app_mode),
    }
    if is_local:
        ev.stats["zero_chunk_rows"] = zero_chunks
    ev.samples = samples
    ev.cross_checks = [
        CrossCheck(
            "every transcript has ≥1 ACL participant row",
            missing_acl == 0,
            f"{missing_acl} transcript(s) with no meeting_participants row — "
            "unqueryable by anyone",
        ),
        CrossCheck(
            "no transcript stamped with a department",
            dept_stamped == 0,
            f"{dept_stamped} transcript(s) carry a department (over-grant risk; "
            "transcripts are participant-gated, not dept-gated)",
        ),
        CrossCheck(
            "no failed transcript ingestions",
            failed == 0,
            f"{failed} asset(s) in status=failed",
        ),
        CrossCheck(
            "no transcripts stuck 'processing' >2h",
            stuck == 0,
            f"{stuck} asset(s) still flagged processing",
        ),
    ]
    if not transcripts:
        ev.notes.append(
            f"no transcripts ingested in the last {window}h — may be legit on a "
            "quiet day; the Freshness auditor should weigh recent meeting volume."
        )
    if not is_local:
        ev.notes.append(
            "GCP/Vertex RAG chunks server-side, so the local chunks table is "
            "empty by design for ingested docs — chunk counts are intentionally "
            "NOT collected here and their absence is NOT a retrieval problem."
        )
    return ev


def _eos_weekly_evidence(db: Session, now: datetime) -> RunEvidence:
    window = WEEKLY_WINDOW_HOURS
    cutoff = now - timedelta(hours=window)
    ev = RunEvidence(kind=RunKind.EOS_WEEKLY, window_hours=window)

    disabled = bool(getattr(settings, "CUBIE_DISABLE_EOS_INGEST", False))
    from rubik_core.tenancy.corpora import corpus_id

    corpus_set = bool(corpus_id("default"))
    stats: dict = {
        "ingestion_enabled": not disabled,
        "rag_corpus_configured": corpus_set,
        "app_mode": str(settings.APP_MODE),
    }
    samples: list[dict] = []
    content_present = False
    try:
        from rubik_core.database.eos.issues import Issue
        from rubik_core.database.eos.l10 import L10Session
        from rubik_core.database.eos.processes import EosProcess
        from rubik_core.database.eos.rocks import Rock
        from rubik_core.database.eos.vto import VTOVersion

        rocks_total = db.query(func.count(Rock.id)).scalar() or 0
        issues_total = db.query(func.count(Issue.id)).scalar() or 0
        issues_solved = (
            db.query(func.count(Issue.id))
            .filter(func.lower(Issue.status) == "solved")
            .scalar()
            or 0
        )
        processes_total = db.query(func.count(EosProcess.id)).scalar() or 0
        vto_total = db.query(func.count(VTOVersion.id)).scalar() or 0
        sessions_total = db.query(func.count(L10Session.id)).scalar() or 0
        sessions_recent = (
            db.query(func.count(L10Session.id))
            .filter(L10Session.actual_start >= cutoff)
            .scalar()
            or 0
        )
        stats.update(
            {
                "rocks": rocks_total,
                "issues_total": issues_total,
                "issues_solved": issues_solved,
                "processes": processes_total,
                "vto_versions": vto_total,
                "l10_sessions_total": sessions_total,
                "l10_sessions_this_week": sessions_recent,
            }
        )
        r = db.query(Rock).first()
        if r:
            samples.append({"source": "rock", "text_present": bool(r.description)})
        i = db.query(Issue).first()
        if i:
            samples.append({"source": "issue", "text_present": bool(i.description)})
        p = db.query(EosProcess).first()
        if p:
            samples.append({"source": "process", "name_present": bool(p.name)})
        content_present = any(
            [rocks_total, issues_total, processes_total, vto_total, sessions_total]
        )
    except Exception as exc:  # noqa: BLE001 — degrade, don't crash the run
        logger.warning("[ingestion-council] EOS source query failed: %s", exc)
        ev.notes.append("EOS source query failed")

    ev.stats = stats
    ev.samples = samples
    ev.cross_checks = [
        CrossCheck(
            "EOS ingestion is enabled",
            not disabled,
            "CUBIE_DISABLE_EOS_INGEST is set — the weekly sweep is a no-op"
            if disabled
            else "",
        ),
        CrossCheck(
            "RAG corpus is configured",
            corpus_set,
            "This workspace has no bound default RAG corpus yet"
            if not corpus_set
            else "",
        ),
        CrossCheck(
            "EOS has content to ingest",
            content_present,
            "no rows in any EOS source table" if not content_present else "",
        ),
    ]
    ev.notes.append(
        "EOS docs land in Vertex (no per-doc DB record), so corpus writes are "
        "verified indirectly (source counts + config). The Retrieval auditor "
        "should treat the actual corpus import as unconfirmed by this evidence."
    )
    return ev


_GATHERERS: dict[RunKind, Callable[[Session, datetime], RunEvidence]] = {
    RunKind.OVERNIGHT_SALES: _overnight_sales_evidence,
    RunKind.PRIORITIES_DAILY: _priorities_daily_evidence,
    RunKind.TRANSCRIPT_SYNC: _transcript_sync_evidence,
    RunKind.EOS_WEEKLY: _eos_weekly_evidence,
}


def gather_evidence(
    db: Session, kinds: list[RunKind], now: datetime | None = None
) -> list[RunEvidence]:
    """Gather evidence for each requested run kind. ``now`` is injectable so
    tests pin the window deterministically."""
    now = now or datetime.utcnow()
    out: list[RunEvidence] = []
    for k in kinds:
        fn = _GATHERERS.get(k)
        if fn is None:
            continue
        try:
            out.append(fn(db, now))
        except Exception as exc:  # noqa: BLE001 — one gatherer must not kill the run
            logger.exception("[ingestion-council] evidence gather failed for %s", k)
            ev = RunEvidence(kind=k, window_hours=0)
            ev.notes.append("evidence gathering crashed")
            ev.cross_checks.append(
                CrossCheck("evidence gathering succeeded", False, "source unavailable")
            )
            out.append(ev)
    return out
