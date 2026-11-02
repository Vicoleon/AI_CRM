"""Which deals count as WON in the BigQuery HubSpot mirror: one helper.

HubSpot's built-in won stage is ``closedwon``. A pipeline can also have a won
stage of its own (a custom stage, whose id is a number or a UUID), and which
ones those are is the company's configuration, never code:
the workspace HubSpot connector's closed-won stage ids (empty by default).

Every BigQuery report that splits won from open builds its condition here, and
the configured ids reach BigQuery as a query parameter
(``@closed_won_stage_ids``), never interpolated into the SQL. A query that uses
the condition runs with :func:`closed_won_job_config`.

The live HubSpot API path does not need this: it reads ``hs_is_closed_won``,
which HubSpot computes for every pipeline (``rubik_core/retrieval/hubspot_api.py``).
"""

from __future__ import annotations

from typing import Any

from rubik_core.tenancy.hubspot_config import get_hubspot_config

#: The query parameter the condition binds the configured ids to.
PARAM = "closed_won_stage_ids"


def closed_won_stage_ids() -> tuple[str, ...]:
    """The configured won-stage ids besides ``closedwon`` (may be empty)."""
    return get_hubspot_config().closed_won_stage_ids


def closed_won_condition(alias: str = "d") -> str:
    """SQL that is true for a won deal row aliased ``alias``.

    The built-in ``closedwon`` (in any spelling the mirror stores) or one of
    the configured ids, bound as ``@closed_won_stage_ids``.
    """
    return (
        f"(LOWER(REPLACE({alias}.dealstage, '_', '')) = 'closedwon' "
        f"OR {alias}.dealstage IN UNNEST(@{PARAM}))"
    )


def uses_closed_won(sql: str) -> bool:
    """Does ``sql`` reference the won-stage parameter (and so need its config)?"""
    return f"@{PARAM}" in (sql or "")


def closed_won_job_config() -> Any:
    """A BigQuery ``QueryJobConfig`` binding the configured won-stage ids."""
    from google.cloud import bigquery  # local: keep this module import-light

    return bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter(PARAM, "STRING", list(closed_won_stage_ids()))
        ]
    )
