from __future__ import annotations

# rubik_core/adapters/bigquery.py
import logging
from typing import Any

from google.cloud import bigquery

from rubik_core.config import settings

logger = logging.getLogger(__name__)


class WorkspaceBigQueryUnavailable(RuntimeError):
    """The legacy CRM mirror has no workspace-bound BigQuery configuration."""


def require_workspace_crm_bigquery() -> None:
    """Reject global CRM BigQuery settings until exact workspace bindings exist.

    The old project, dataset, and data-agent settings are process-wide. A
    request scoped to one workspace must never query another workspace's CRM
    mirror through those settings, even when the caller injected an adapter.
    """
    raise WorkspaceBigQueryUnavailable(
        "HubSpot BigQuery mirror is unavailable: workspace-bound project and "
        "dataset bindings are not configured"
    )


class BigQueryAdapter:
    def __init__(self):
        require_workspace_crm_bigquery()
        self.project_id = settings.BIGQUERY_PROJECT_ID or settings.GCP_PROJECT_ID
        self.dataset_id = settings.BIGQUERY_DATASET
        self.client = bigquery.Client(project=self.project_id)

    def query(
        self, sql: str, job_config: bigquery.QueryJobConfig | None = None
    ) -> list[dict[str, Any]]:
        """Executes a SQL query and returns results as a list of dictionaries."""
        require_workspace_crm_bigquery()
        logger.info(f"Executing BigQuery SQL: {sql}")
        query_job = self.client.query(sql, job_config=job_config)
        results = query_job.result()
        return [dict(row) for row in results]

    def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """Returns the schema of a table."""
        require_workspace_crm_bigquery()
        try:
            table_ref = f"{self.project_id}.{self.dataset_id}.{table_name}"
            table = self.client.get_table(table_ref)
            return [
                {"name": field.name, "type": field.field_type} for field in table.schema
            ]
        except Exception as e:
            logger.error(f"BigQuery Schema Error for {table_name}: {e}")
            return []

    def list_tables(self, dataset_id: str | None = None) -> list[str]:
        """Lists all tables in the specified dataset."""
        require_workspace_crm_bigquery()
        try:
            ds_id = dataset_id or self.dataset_id
            dataset_ref = f"{self.project_id}.{ds_id}"
            tables = self.client.list_tables(dataset_ref)
            return [table.table_id for table in tables]
        except Exception as e:
            logger.error(f"BigQuery List Tables Error for {dataset_id}: {e}")
            return []
