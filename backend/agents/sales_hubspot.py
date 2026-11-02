"""HubSpot BigQuery probes — the data layer behind the HubSpot SignalSource.

Born as UC-10's `SalesHubspotPlugin` (mechanical stale-lead / stage-stuck /
new-product rules). UC-17's `MultiSourcePrioritiesPlugin` replaced it as the
Sales producer and the plugin class + rule builders have been deleted — see
git history. What remains is the part that stayed live: the BigQuery fetch
probes `backend/signals/hubspot.py` imports to build `Signal`s.

When the HubSpot pipeline is mid-rebuild (e.g. tables missing, schema in
flux), every probe degrades gracefully via `_safe_query` — callers see zero
rows of that flavor rather than an exception.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.adapters.bigquery import (
    BigQueryAdapter,
    WorkspaceBigQueryUnavailable,
    require_workspace_crm_bigquery,
)
from rubik_core.config import settings
from rubik_core.retrieval.hubspot_properties import deal_product_property

logger = logging.getLogger(__name__)


STALE_LEAD_DAYS = 21
STAGE_STUCK_DAYS = 14
LOOKBACK_DAYS = 30


def _project() -> str:
    try:
        require_workspace_crm_bigquery()
    except WorkspaceBigQueryUnavailable:
        return ""
    return settings.BIGQUERY_PROJECT_ID or settings.GCP_PROJECT_ID or ""


def _products_column() -> str:
    """The raw deal column that says what a deal is for, as ``products``.

    The raw mirror's columns are the deal's HubSpot properties, and the one
    that holds products is the company's own field
    (``HUBSPOT_DEAL_PRODUCT_PROPERTY``). Its name is checked to be a plain
    internal name before it gets here; with none named the column is NULL.
    """
    prop = deal_product_property()
    return f"rd.{prop} AS products" if prop else "CAST(NULL AS STRING) AS products"


def _safe_query(bq: BigQueryAdapter, sql: str, label: str) -> list[dict[str, Any]]:
    """Run a BigQuery query, swallowing errors so one missing table doesn't
    kill the run. Returns [] when the query fails — the caller sees no
    rows of that flavor that night."""
    try:
        require_workspace_crm_bigquery()
        return bq.query(sql)
    except Exception as e:
        logger.warning(f"[sales_hubspot] {label} query failed (treating as empty): {e}")
        return []


# ─── BigQuery probes ──────────────────────────────────────────────────────────


def fetch_stale_open_deals(
    bq: BigQueryAdapter, lookback_days: int = LOOKBACK_DAYS
) -> list[dict]:
    """Open deals whose most recent activity is > STALE_LEAD_DAYS ago.

    Schema notes from dev BigQuery:
      • `fct_deals` doesn't carry the deal name — that lives in raw `tbl_deals`
        (joined as `rd`). The marts table exposes the boolean `is_closed`
        instead of the string `dealstage` matching we used in early drafts.
      • `fct_activities` is event-partitioned but does NOT require a partition
        filter, so a global MAX() per deal still works.
    """
    project = _project()
    if not project:
        return []
    sql = f"""
WITH last_activity AS (
  SELECT
    bdc.deal_id,
    MAX(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(a.event_ts AS STRING), 1, 10))) AS last_activity_date
  FROM `{project}.hubspot_sales_marts.fct_activities` a
  JOIN `{project}.hubspot_sales_marts.brg_activity_deal` bdc ON a.activity_id = bdc.activity_id
  -- brg_activity_deal REQUIRES a partition filter on `ingested_ts` (BigQuery
  -- rejects the query otherwise). 730 days is wide enough to cover historical
  -- bridge rows for any active deal. fct_activities is partitioned but doesn't
  -- require a filter; we cap to 365 days for performance.
  WHERE bdc.ingested_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 730 DAY)
    AND a.event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 365 DAY)
  GROUP BY bdc.deal_id
)
SELECT
  d.deal_id,
  rd.dealname AS deal_name,
  LOWER(o.email) AS owner_email,
  o.id AS owner_id,
  c.name AS company_name,
  la.last_activity_date,
  DATE_DIFF(CURRENT_DATE(), la.last_activity_date, DAY) AS days_since_last_activity,
  d.dealstage,
  d.amount,
  {_products_column()}
FROM `{project}.hubspot_sales_marts.fct_deals` d
LEFT JOIN `{project}.hubspot_raw.tbl_owners` o ON d.hubspot_owner_id = o.id
LEFT JOIN `{project}.hubspot_sales_marts.brg_deal_company` bdc ON d.deal_id = bdc.deal_id
LEFT JOIN `{project}.hubspot_sales_marts.dim_company` c ON bdc.company_id = c.company_id
LEFT JOIN `{project}.hubspot_raw.tbl_deals` rd ON d.deal_id = rd.hs_object_id
LEFT JOIN last_activity la ON d.deal_id = la.deal_id
WHERE
  d.is_closed = FALSE
  AND la.last_activity_date IS NOT NULL
  AND DATE_DIFF(CURRENT_DATE(), la.last_activity_date, DAY) > {STALE_LEAD_DAYS}
  AND DATE_DIFF(CURRENT_DATE(), la.last_activity_date, DAY) <= {lookback_days + STALE_LEAD_DAYS}
  AND o.email IS NOT NULL
ORDER BY days_since_last_activity DESC
LIMIT 500
"""
    return _safe_query(bq, sql, "stale_open_deals")


def fetch_stage_stuck_deals(bq: BigQueryAdapter) -> list[dict]:
    """Open deals stuck in their current pipeline stage > STAGE_STUCK_DAYS.

    Schema notes:
      • `fct_deal_stage_history` REQUIRES a partition filter on `entered_ts`,
        otherwise BigQuery rejects the query. We restrict to the past 365
        days — anything older isn't actionable as "stuck" today anyway.
      • Deal name pulled from raw `tbl_deals` (`rd`); fct_deals doesn't carry it.
      • `is_closed = FALSE` replaces the brittle dealstage string match.
    """
    project = _project()
    if not project:
        return []
    sql = f"""
WITH current_stage AS (
  SELECT
    h.deal_id,
    h.stage_id,
    h.entered_ts,
    ROW_NUMBER() OVER (PARTITION BY h.deal_id ORDER BY h.entered_ts DESC) AS rn
  FROM `{project}.hubspot_sales_marts.fct_deal_stage_history` h
  WHERE h.entered_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 365 DAY)
)
SELECT
  d.deal_id,
  rd.dealname AS deal_name,
  LOWER(o.email) AS owner_email,
  o.id AS owner_id,
  c.name AS company_name,
  s.label AS stage_label,
  cs.entered_ts AS stage_entered_ts,
  DATE_DIFF(
    CURRENT_DATE(),
    SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(cs.entered_ts AS STRING), 1, 10)),
    DAY
  ) AS days_in_stage,
  d.amount
FROM `{project}.hubspot_sales_marts.fct_deals` d
JOIN current_stage cs ON d.deal_id = cs.deal_id AND cs.rn = 1
LEFT JOIN `{project}.hubspot_raw.tbl_deal_stages` s ON cs.stage_id = s.id
LEFT JOIN `{project}.hubspot_raw.tbl_owners` o ON d.hubspot_owner_id = o.id
LEFT JOIN `{project}.hubspot_sales_marts.brg_deal_company` bdc ON d.deal_id = bdc.deal_id
LEFT JOIN `{project}.hubspot_sales_marts.dim_company` c ON bdc.company_id = c.company_id
LEFT JOIN `{project}.hubspot_raw.tbl_deals` rd ON d.deal_id = rd.hs_object_id
WHERE
  d.is_closed = FALSE
  AND DATE_DIFF(
        CURRENT_DATE(),
        SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(cs.entered_ts AS STRING), 1, 10)),
        DAY
      ) > {STAGE_STUCK_DAYS}
  AND o.email IS NOT NULL
ORDER BY days_in_stage DESC
LIMIT 500
"""
    return _safe_query(bq, sql, "stage_stuck_deals")


def fetch_market_triggers(bq: BigQueryAdapter) -> list[dict]:
    """Optional UC-14 cross-reference. Empty list when the table doesn't exist yet.

    The ``hubspot_sales_marts.market_triggers`` table has never been provisioned
    (UC-14 stayed on the backlog), so every call was a guaranteed 404 that
    ``_safe_query`` caught and turned into ``[]`` — but it still cost a BigQuery
    round-trip and a WARNING log line *per user*, once for every user in the
    nightly priorities batch. We short-circuit to the same ``[]`` result without
    the round-trip. To re-enable when the table lands, restore the SELECT against
    ``{project}.hubspot_sales_marts.market_triggers`` (see git history) and route
    it back through ``_safe_query(bq, sql, "market_triggers")``.
    """
    return []
