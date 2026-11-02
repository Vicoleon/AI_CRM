# rubik_core/retrieval/schema_agent.py
import logging

from rubik_core.adapters.bigquery import BigQueryAdapter
from rubik_core.config import settings
from rubik_core.models import get_genai_client

logger = logging.getLogger(__name__)


class HubSpotSchemaAgent:
    """
    Agent that dynamically discovers relevant HubSpot schemas in BigQuery.
    """

    def __init__(self):
        self.bq = BigQueryAdapter()
        self.client = get_genai_client()
        self.model_id = settings.VERTEX_LLM_MODEL_FLASH

        self.datasets = ["hubspot_raw", "hubspot_sales_marts"]

    def get_relevant_schema(self, query: str) -> str:
        """
        Identifies relevant tables and fetches their schemas.
        """
        try:
            # 1. Discover all tables
            all_tables = {}
            for ds in self.datasets:
                tables = self.bq.list_tables(ds)
                all_tables[ds] = tables

            # 2. Use LLM to pick relevant tables
            relevant_tables = self._pick_relevant_tables(query, all_tables)
            if not relevant_tables:
                logger.info("No relevant tables identified by Schema Agent.")
                return ""

            # 3. Format the discovered tables for the generator
            project = self.bq.project_id
            schema_context = "AVAILABLE HUBSPOT TABLES (Use fully qualified names):\n"
            for ds, tables in all_tables.items():
                schema_context += f"- Dataset: `{ds}`\n"
                full_table_names = [f"`{project}.{ds}.{t}`" for t in tables]
                schema_context += f"  Tables: {', '.join(full_table_names)}\n"

            schema_context += "\nRELEVANT TABLES TO FOCUS ON (Use these first):\n"
            for ds, tables in relevant_tables.items():
                full_relevant_names = [f"`{project}.{ds}.{t}`" for t in tables]
                schema_context += f"- {ds}: {', '.join(full_relevant_names)}\n"

            # 4. Add Deal Stage Mapping
            schema_context += self._get_deal_stages()

            return schema_context
        except Exception as e:
            logger.error(f"Error in HubSpotSchemaAgent: {e}")
            return ""

    def _get_deal_stages(self) -> str:
        """
        Fetches deal stages from BigQuery to provide ID-to-Label mapping.
        """
        try:
            # Check both datasets just in case, but prefer raw for metadata
            table_name = "hubspot_raw.tbl_deal_stages"
            sql = f"SELECT id, label, pipeline_id FROM `{table_name}` LIMIT 100"

            results = self.bq.query(sql)
            if not results:
                return ""

            mapping = "\nDEAL STAGES (id: label):\n"
            for row in results:
                mapping += (
                    f"- {row['id']}: {row['label']} (Pipeline: {row['pipeline_id']})\n"
                )

            return mapping
        except Exception as e:
            logger.warning(f"Could not fetch deal stages: {e}")
            return ""

    def _pick_relevant_tables(
        self, query: str, all_tables: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        """
        Asks the LLM to select relevant tables based on the query.
        """
        table_list_str = ""
        for ds, tables in all_tables.items():
            table_list_str += f"{ds}: {', '.join(tables)}\n"

        prompt = f"""You are a data architect. Given a user's question, identify which HubSpot tables in BigQuery are likely to contain the answer.

USER QUESTION: {query}

AVAILABLE DATASETS AND TABLES:
{table_list_str}

Pick only the most relevant tables (max 5). 
Return your response in this format:
hubspot_raw: table1, table2
hubspot_sales_marts: table3

ONLY return the formatted list, nothing else."""

        response = self.client.models.generate_content(
            model=self.model_id,
            contents=prompt,
        )
        content = response.text

        relevant = {}
        for line in content.strip().split("\n"):
            if ":" in line:
                ds, tables = line.split(":", 1)
                ds = ds.strip()
                table_names = [t.strip() for t in tables.split(",") if t.strip()]
                if table_names:
                    relevant[ds] = table_names

        return relevant
