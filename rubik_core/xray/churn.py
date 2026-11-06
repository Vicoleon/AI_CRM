"""Deterministic churn scorer.

Score = 0–100 **risk** (higher = more at-risk), a weighted average of factors
that HAVE data. Every factor returns ``(name, value, points, weight,
evidence)`` so the UI can show the table and the analyst can cite it. The LLM
writes ``rationale_md`` from the factor table but can NEVER change the number
(spec §4).

Two design rules that make this trustworthy:

* **Renormalize, don't zero.** A factor with no evidence (no renewal date, no
  open deals, or Fast runs skipping meeting sentiment) is DROPPED and the
  remaining weights renormalize — so "we don't know" never masquerades as
  "healthy on that axis".
* **Cadence-relative recency.** Engagement recency is judged against the
  account's OWN historical cadence, not a fixed threshold — a monthly-cadence
  account 60 days dark is riskier than a quarterly one 60 days dark.

Every snapshot logs score + factors, so once we have real churn outcomes we can
fit weights (or a model) on our own labels.
"""

from __future__ import annotations

import logging
import re
import statistics
from datetime import datetime
from typing import Any

from rubik_core.xray import config
from rubik_core.xray import properties as xray_properties
from rubik_core.xray.fetchers import _ACTIVITY_TS_PROP, _parse_ts
from rubik_core.xray.schemas import ChurnFactor, ChurnResult, CompanyBundle

logger = logging.getLogger(__name__)

_DEFAULT_CADENCE_DAYS = 30.0
_TERMINATED_RE = re.compile(
    r"\b(terminat|cancel|closed?\s*lost|churn|offboard|inactive|expired)\b", re.I
)
_AT_RISK_RE = re.compile(
    r"\b(at[\s_-]?risk|past\s*due|delinquent|overdue|dispute)\b", re.I
)
_HEALTHY_RE = re.compile(
    r"\b(active|current|good\s*standing|renew|onboard|live)\b", re.I
)


def score(
    bundle: CompanyBundle,
    *,
    meeting_sentiment: float | None = None,
    now: datetime | None = None,
) -> ChurnResult:
    """Compute the churn result for a fetched bundle.

    ``meeting_sentiment`` (-1..1) is supplied only by Deep runs (the meetings
    agent); Fast passes ``None`` and that factor is skipped + renormalized.
    """
    now = now or datetime.utcnow()
    factors: list[ChurnFactor] = []
    for fn in (
        _engagement_recency,
        _engagement_trend,
        _renewal_exposure,
        _relationship_breadth,
        _deal_momentum,
        _client_status,
    ):
        try:
            f = fn(bundle, now)
        except Exception:  # noqa: BLE001 - one bad factor never sinks the score
            logger.exception(
                "churn factor %s failed for %s", fn.__name__, bundle.company_id
            )
            f = None
        if f is not None:
            factors.append(f)

    if meeting_sentiment is not None:
        factors.append(_meeting_sentiment_factor(meeting_sentiment))

    if not factors:
        logger.info("churn: no scorable factors for %s", bundle.company_id)
        return ChurnResult(score=0, band=config.band_for_score(0), factors=[])

    total_weight = sum(f.weight for f in factors) or 1.0
    raw = sum(f.points * f.weight for f in factors) / total_weight
    s = max(0, min(100, round(raw)))
    return ChurnResult(score=s, band=config.band_for_score(s), factors=factors)


# ── Factors ───────────────────────────────────────────────────────────────────
def _w(name: str) -> float:
    return config.CHURN_WEIGHTS.get(name, 0.1)


def _activity_dates(bundle: CompanyBundle) -> list[datetime]:
    out: list[datetime] = []
    for obj, items in bundle.engagements.items():
        ts_prop = _ACTIVITY_TS_PROP.get(obj, "hs_timestamp")
        for it in items:
            dt = _parse_ts(it.get(ts_prop))
            if dt:
                out.append(dt)
    return sorted(out)


def _company_last_activity(bundle: CompanyBundle) -> datetime | None:
    c = bundle.company or {}
    for key in (
        "hs_last_sales_activity_timestamp",
        "notes_last_updated",
        "notes_last_contacted",
    ):
        dt = _parse_ts(c.get(key))
        if dt:
            return dt
    return None


def _engagement_recency(bundle: CompanyBundle, now: datetime) -> ChurnFactor | None:
    dates = _activity_dates(bundle)
    last = dates[-1] if dates else _company_last_activity(bundle)
    if last is None:
        return None
    days_since = max(0, (now - last).days)

    # Account's own cadence = median gap between consecutive activities.
    cadence = _DEFAULT_CADENCE_DAYS
    if len(dates) >= 3:
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            cadence = max(3.0, statistics.median(gaps))

    ratio = days_since / max(cadence, 1.0)
    if ratio <= 1:
        pts = 10.0
    elif ratio <= 2:
        pts = 32.0
    elif ratio <= 3:
        pts = 55.0
    elif ratio <= 5:
        pts = 78.0
    else:
        pts = 95.0
    return ChurnFactor(
        name="engagement_recency",
        value=f"{days_since}d since last touch",
        points=pts,
        weight=_w("engagement_recency"),
        evidence=f"last activity {last.date()}, account cadence ~{round(cadence)}d",
    )


def _engagement_trend(bundle: CompanyBundle, now: datetime) -> ChurnFactor | None:
    dates = _activity_dates(bundle)
    if not dates:
        return None
    win = config.ACTIVITY_TREND_WINDOW_DAYS
    recent = sum(1 for d in dates if (now - d).days <= win)
    prior = sum(1 for d in dates if win < (now - d).days <= 2 * win)
    if recent == 0 and prior == 0:
        return None
    if prior == 0:
        pts = 15.0  # brand-new or resurgent activity — not a decline signal
    else:
        ratio = recent / prior
        if ratio >= 1.0:
            pts = 12.0
        elif ratio >= 0.5:
            pts = 45.0
        elif ratio >= 0.25:
            pts = 68.0
        else:
            pts = 88.0
    return ChurnFactor(
        name="engagement_trend",
        value=f"{recent} vs {prior} activities",
        points=pts,
        weight=_w("engagement_trend"),
        evidence=f"last {win}d ({recent}) vs prior {win}d ({prior})",
    )


def _open_deals(bundle: CompanyBundle) -> list[dict[str, Any]]:
    return [d for d in bundle.deals if str(d.get("hs_is_closed")) != "true"]


def _renewal_exposure(bundle: CompanyBundle, now: datetime) -> ChurnFactor | None:
    # Nearest renewal date across open deals.
    # The renewal date is the company's own field (XRAY_RENEWAL_DATE_PROPERTY);
    # with none named, no deal has one and the factor drops out.
    renewals: list[tuple[datetime, dict[str, Any]]] = []
    for d in _open_deals(bundle):
        dt = _parse_ts(xray_properties.renewal_date(d))
        if dt:
            renewals.append((dt, d))
    if not renewals:
        return None
    renewals.sort(key=lambda x: x[0])
    when, deal = renewals[0]
    days_to = (when - now).days

    auto = xray_properties.auto_renews(deal)
    if days_to < 0:
        pts = 90.0
    elif days_to <= 30:
        pts = 80.0
    elif days_to <= 90:
        pts = 55.0
    elif days_to <= 180:
        pts = 30.0
    else:
        pts = 10.0
    if not auto:
        pts = min(100.0, pts + 15.0)  # no auto-renew = the classic churn cliff
    return ChurnFactor(
        name="renewal_exposure",
        value=f"{days_to}d to renewal",
        points=pts,
        weight=_w("renewal_exposure"),
        evidence=f"renewal {when.date()}, auto-renew={'yes' if auto else 'no'}",
    )


def _relationship_breadth(bundle: CompanyBundle, now: datetime) -> ChurnFactor | None:
    contacts = bundle.contacts or []
    if not contacts:
        return None
    engaged = 0
    for c in contacts:
        dt = _parse_ts(
            c.get("hs_last_sales_activity_timestamp")
            or c.get("notes_last_contacted")
            or c.get("lastmodifieddate")
        )
        if dt and (now - dt).days <= 180:
            engaged += 1
    n = engaged if engaged else len(contacts)
    if n >= 4:
        pts = 10.0
    elif n == 3:
        pts = 30.0
    elif n == 2:
        pts = 55.0
    else:
        pts = 80.0  # single-threaded
    return ChurnFactor(
        name="relationship_breadth",
        value=f"{n} engaged contact(s)",
        points=pts,
        weight=_w("relationship_breadth"),
        evidence=f"{engaged} contact(s) active in 6mo of {len(contacts)} total",
    )


def _deal_momentum(bundle: CompanyBundle, now: datetime) -> ChurnFactor | None:
    open_deals = _open_deals(bundle)
    if not open_deals:
        return None
    stalled_days = 0
    for d in open_deals:
        entered = _parse_ts(d.get("hs_v2_date_entered_current_stage"))
        if entered:
            stalled_days = max(stalled_days, (now - entered).days)
    if stalled_days <= 30:
        pts = 15.0
    elif stalled_days <= 60:
        pts = 40.0
    elif stalled_days <= 120:
        pts = 65.0
    else:
        pts = 85.0
    return ChurnFactor(
        name="deal_momentum",
        value=f"{stalled_days}d in stage",
        points=pts,
        weight=_w("deal_momentum"),
        evidence=f"{len(open_deals)} open deal(s); oldest {stalled_days}d in current stage",
    )


def _client_status(bundle: CompanyBundle, now: datetime) -> ChurnFactor | None:
    # The status fields an admin named (XRAY_ACCOUNT_STATUS_PROPERTIES), in
    # order; with none named the factor drops out.
    status = xray_properties.account_status(bundle.company or {})
    if not status:
        return None
    if _TERMINATED_RE.search(status):
        pts = 90.0
    elif _AT_RISK_RE.search(status):
        pts = 70.0
    elif _HEALTHY_RE.search(status):
        pts = 12.0
    else:
        pts = 40.0  # unknown wording — mild caution
    return ChurnFactor(
        name="client_status",
        value=status[:80],
        points=pts,
        weight=_w("client_status"),
        evidence="account status in HubSpot",
    )


def _meeting_sentiment_factor(sentiment: float) -> ChurnFactor:
    s = max(-1.0, min(1.0, float(sentiment)))
    pts = (1.0 - s) / 2.0 * 100.0  # +1 → 0 risk, -1 → 100 risk
    return ChurnFactor(
        name="meeting_sentiment",
        value=f"{s:+.2f} trend",
        points=pts,
        weight=_w("meeting_sentiment"),
        evidence="meetings-agent sentiment trajectory (-1..1)",
    )
