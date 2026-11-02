from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from rubik_core.adapters.bigquery import BigQueryAdapter
from rubik_core.config import settings
from rubik_core.models import get_genai_client
from rubik_core.retrieval import closed_won
from rubik_core.retrieval.date_utils import resolve_date_range
from rubik_core.retrieval.hubspot_properties import deal_product_property
from rubik_core.retrieval.schema_agent import HubSpotSchemaAgent
from rubik_core.retrieval.sql_validator import SQLValidator
from rubik_core.tenancy.context import current_workspace

logger = logging.getLogger(__name__)

# Simple in-memory cache to avoid repeated LLM calls for the same query
# Key: (workspace, query string), Value: Parsed intent dictionary (raw)
INTENT_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_INTENT_CACHE_MAX = 256


class HubSpotIntentSQLGenerator:
    """
    Generates HubSpot SQL queries based on structured intent objects.
    Ported from Javascript logic to Python.
    """

    def __init__(self, project: str | None = None):
        self.project = (
            project or settings.BIGQUERY_PROJECT_ID or settings.GCP_PROJECT_ID
        )
        self.marts_dataset = "hubspot_sales_marts"
        self.raw_dataset = "hubspot_raw"

    def build_where_clause(self, filters: dict[str, Any], alias: str = "d") -> str:
        conditions = ["1=1"]

        if filters.get("rep_name"):
            # Handling rep_name with placeholder as in original logic
            conditions.append(f"{alias}.hubspot_owner_id = '{{{{OWNER_ID}}}}'")

        if filters.get("team_name"):
            conditions.append(f"tm.team_name = '{filters['team_name']}'")

        if filters.get("company_name"):
            conditions.append(f"c.name LIKE '%{filters['company_name']}%'")

        if filters.get("pipeline"):
            conditions.append(f"{alias}.pipeline = '{filters['pipeline']}'")

        if filters.get("product_line"):
            # What a deal is for is the company's own field; with none named
            # there is nothing to filter on, so the qualifier is not applied.
            product = deal_product_property()
            if product:
                conditions.append(f"rd.{product} LIKE '%{filters['product_line']}%'")
            else:
                logger.info("product filter skipped: no deal product field named")

        if filters.get("email"):
            a = "rd" if alias == "rd" else "d"
            conditions.append(f"LOWER({a}.email) = LOWER('{filters['email']}')")

        if filters.get("contact_name"):
            # Logic uses d. even if rd alias is passed for some fields
            conditions.append(
                f"(LOWER(d.firstname) LIKE LOWER('%{filters['contact_name']}%') OR LOWER(d.lastname) LIKE LOWER('%{filters['contact_name']}%'))"
            )

        if filters.get("location"):
            conditions.append(
                f"(LOWER(d.city) LIKE LOWER('%{filters['location']}%') OR LOWER(d.state) LIKE LOWER('%{filters['location']}%') OR LOWER(d.address) LIKE LOWER('%{filters['location']}%'))"
            )

        return f"WHERE {' AND '.join(conditions)}"

    def generate_sql(self, intent_data: dict[str, Any]) -> str:
        # Normalize intent
        raw_intent = intent_data.get("intent", "").lower().replace(" ", "_")
        intent = raw_intent[4:] if raw_intent.startswith("kpi_") else raw_intent

        filters = intent_data.get("filters", {})
        timeframe = intent_data.get("timeframe", {})
        start_date = timeframe.get("start_date") or "2000-01-01"
        end_date = timeframe.get("end_date") or datetime.now().strftime("%Y-%m-%d")

        project = self.project
        marts = self.marts_dataset
        raw = self.raw_dataset

        # Common Conditions
        # Won = HubSpot's built-in stage plus the company's configured won stages,
        # bound as @closed_won_stage_ids when the query runs (closed_won.py).
        closed_won_condition = closed_won.closed_won_condition()
        closed_date_condition = f"SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.closedate AS STRING), 1, 10)) >= DATE('{start_date}') AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.closedate AS STRING), 1, 10)) <= DATE('{end_date}')"
        created_date_condition = f"SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.createdate AS STRING), 1, 10)) >= DATE('{start_date}') AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.createdate AS STRING), 1, 10)) <= DATE('{end_date}')"
        sales_cycle_calc = "AVG(DATE_DIFF(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.closedate AS STRING), 1, 10)), SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.createdate AS STRING), 1, 10)), DAY))"

        # Joins
        owner_joins = f"LEFT JOIN `{project}.{raw}.tbl_owners` o ON d.hubspot_owner_id = o.id LEFT JOIN `{project}.{marts}.dim_owner_team_map` tm ON d.hubspot_owner_id = tm.hubspot_owner_id"
        raw_deal_join = (
            f"LEFT JOIN `{project}.{raw}.tbl_deals` rd ON d.deal_id = rd.hs_object_id"
        )
        company_join = f"LEFT JOIN `{project}.{marts}.brg_deal_company` bdc ON d.deal_id = bdc.deal_id LEFT JOIN `{project}.{marts}.dim_company` c ON bdc.company_id = c.company_id"

        where_clause = self.build_where_clause(filters)

        sql = ""
        if intent == "deals_created":
            sql = f"SELECT COUNT(DISTINCT d.deal_id) as deals_created, CONCAT(o.first_name, ' ', o.last_name) as rep_name FROM `{project}.{marts}.fct_deals` d {owner_joins} {raw_deal_join} {company_join} {where_clause} AND {created_date_condition} GROUP BY rep_name ORDER BY deals_created DESC"
        elif intent == "deals_open":
            # Count of open deals (not closed won or lost)
            sql = f"SELECT COUNT(DISTINCT d.deal_id) as deals_open, CONCAT(o.first_name, ' ', o.last_name) as rep_name FROM `{project}.{marts}.fct_deals` d {owner_joins} {raw_deal_join} {company_join} {where_clause} AND NOT {closed_won_condition} AND LOWER(d.dealstage) NOT LIKE '%lost%' GROUP BY rep_name ORDER BY deals_open DESC"
        elif intent == "deals_won":
            sql = f"SELECT COUNT(DISTINCT d.deal_id) as deals_won, CONCAT(o.first_name, ' ', o.last_name) as rep_name FROM `{project}.{marts}.fct_deals` d {owner_joins} {raw_deal_join} {company_join} {where_clause} AND {closed_won_condition} AND {closed_date_condition} GROUP BY rep_name ORDER BY deals_won DESC"
        elif intent == "win_rate":
            sql = f"WITH created_deals AS (SELECT d.hubspot_owner_id, COUNT(DISTINCT d.deal_id) as created_count FROM `{project}.{marts}.fct_deals` d {owner_joins} {raw_deal_join} {company_join} {where_clause} AND {created_date_condition} GROUP BY d.hubspot_owner_id), won_deals AS (SELECT d.hubspot_owner_id, COUNT(DISTINCT d.deal_id) as won_count FROM `{project}.{marts}.fct_deals` d {owner_joins} {raw_deal_join} {company_join} {where_clause} AND {closed_won_condition} AND {closed_date_condition} GROUP BY d.hubspot_owner_id) SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, COALESCE(c.created_count, 0) as deals_created, COALESCE(w.won_count, 0) as deals_won, ROUND(SAFE_DIVIDE(COALESCE(w.won_count, 0), NULLIF(c.created_count, 0)) * 100, 2) as win_rate_pct FROM created_deals c FULL OUTER JOIN won_deals w ON c.hubspot_owner_id = w.hubspot_owner_id LEFT JOIN `{project}.{raw}.tbl_owners` o ON COALESCE(c.hubspot_owner_id, w.hubspot_owner_id) = o.id ORDER BY win_rate_pct DESC"
        elif intent == "avg_deal_value":
            sql = f"SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, AVG(d.amount) as avg_deal_value FROM `{project}.{marts}.fct_deals` d {owner_joins} {raw_deal_join} {company_join} {where_clause} AND {closed_won_condition} AND {closed_date_condition} GROUP BY rep_name ORDER BY avg_deal_value DESC"
        elif intent == "sales_cycle_length":
            sql = f"SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, {sales_cycle_calc} as avg_sales_cycle_days FROM `{project}.{marts}.fct_deals` d {owner_joins} {raw_deal_join} {company_join} {where_clause} AND {closed_won_condition} AND {closed_date_condition} GROUP BY rep_name ORDER BY avg_sales_cycle_days ASC"
        elif intent == "pipeline_value":
            sql = f"SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, SUM(d.amount) as pipeline_value FROM `{project}.{marts}.fct_deals` d {owner_joins} {raw_deal_join} {company_join} {where_clause} AND NOT {closed_won_condition} AND LOWER(d.dealstage) NOT LIKE '%lost%' GROUP BY rep_name ORDER BY pipeline_value DESC"
        elif intent == "meetings_booked_completed":
            w_clause = self.build_where_clause(filters, "e")
            sql = f"SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, COUNT(DISTINCT CASE WHEN e.activity_type = 'MEETING' AND e.outcome = 'COMPLETED' THEN e.activity_id END) as meetings_completed, COUNT(DISTINCT CASE WHEN e.activity_type = 'MEETING' THEN e.activity_id END) as meetings_booked FROM `{project}.{marts}.fct_activities` e LEFT JOIN `{project}.{raw}.tbl_owners` o ON e.hubspot_owner_id = o.id LEFT JOIN `{project}.{marts}.dim_owner_team_map` tm ON e.hubspot_owner_id = tm.hubspot_owner_id {w_clause} AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(e.event_ts AS STRING), 1, 10)) >= DATE('{start_date}') AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(e.event_ts AS STRING), 1, 10)) <= DATE('{end_date}') GROUP BY rep_name ORDER BY meetings_completed DESC"
        elif intent == "new_contacts_created":
            w_clause = self.build_where_clause(filters, "d")
            sql = f"SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, COUNT(DISTINCT d.contact_id) as new_contacts FROM `{project}.{marts}.dim_contact` d LEFT JOIN `{project}.{raw}.tbl_owners` o ON d.hubspot_owner_id = o.id LEFT JOIN `{project}.{marts}.dim_owner_team_map` tm ON d.hubspot_owner_id = tm.hubspot_owner_id {w_clause} AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.created_ts AS STRING), 1, 10)) >= DATE('{start_date}') AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.created_ts AS STRING), 1, 10)) <= DATE('{end_date}') GROUP BY rep_name ORDER BY new_contacts DESC"
        elif intent == "activities_logged":
            w_clause = self.build_where_clause(filters, "e")
            sql = f"SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, COUNT(DISTINCT e.activity_id) as total_activities, COUNT(DISTINCT CASE WHEN e.activity_type = 'CALL' THEN e.activity_id END) as calls, COUNT(DISTINCT CASE WHEN e.activity_type = 'EMAIL' THEN e.activity_id END) as emails, COUNT(DISTINCT CASE WHEN e.activity_type = 'MEETING' THEN e.activity_id END) as meetings FROM `{project}.{marts}.fct_activities` e LEFT JOIN `{project}.{raw}.tbl_owners` o ON e.hubspot_owner_id = o.id LEFT JOIN `{project}.{marts}.dim_owner_team_map` tm ON e.hubspot_owner_id = tm.hubspot_owner_id {w_clause} AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(e.event_ts AS STRING), 1, 10)) >= DATE('{start_date}') AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(e.event_ts AS STRING), 1, 10)) <= DATE('{end_date}') GROUP BY rep_name ORDER BY total_activities DESC"
        elif intent in ["quotes_sent", "proposal_sent"]:
            w_clause = self.build_where_clause(filters, "q")
            sql = f"SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, COUNT(DISTINCT q.quote_id) as quotes_sent FROM `{project}.{marts}.fct_quote` q LEFT JOIN `{project}.{raw}.tbl_owners` o ON q.hubspot_owner_id = o.id LEFT JOIN `{project}.{marts}.dim_owner_team_map` tm ON q.hubspot_owner_id = tm.hubspot_owner_id {w_clause} AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(q.event_ts AS STRING), 1, 10)) >= DATE('{start_date}') AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(q.event_ts AS STRING), 1, 10)) <= DATE('{end_date}') GROUP BY rep_name ORDER BY quotes_sent DESC"
        elif intent == "deal_conversion_rate":
            sql = f"WITH deals_created AS (SELECT d.deal_id, d.createdate, d.hubspot_owner_id FROM `{project}.{marts}.fct_deals` d {owner_joins} {company_join} {where_clause} AND {created_date_condition}), proposals AS (SELECT h.deal_id FROM `{project}.{marts}.fct_deal_stage_history` h JOIN `{project}.{raw}.tbl_deal_stages` s ON h.stage_id = s.id WHERE (LOWER(s.label) LIKE '%proposal%' OR LOWER(s.label) LIKE '%sow%' OR LOWER(s.label) LIKE '%agreement%' OR LOWER(s.label) LIKE '%negotiation%' OR LOWER(s.label) LIKE '%won%') AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(h.entered_ts AS STRING), 1, 10)) >= DATE('2024-01-01') GROUP BY 1) SELECT COUNT(DISTINCT dc.deal_id) as qualified_deals, COUNT(DISTINCT p.deal_id) as proposal_deals, ROUND(SAFE_DIVIDE(COUNT(DISTINCT p.deal_id), COUNT(DISTINCT dc.deal_id)) * 100, 2) as conversion_rate_pct FROM deals_created dc LEFT JOIN proposals p ON dc.deal_id = p.deal_id"
        elif intent in ["avg_revenue_per_client", "revenue_per_client"]:
            sql = f"SELECT c.name as client_name, SUM(d.amount) as total_revenue FROM `{project}.{marts}.fct_deals` d JOIN `{project}.{marts}.brg_deal_company` bdc ON d.deal_id = bdc.deal_id JOIN `{project}.{marts}.dim_company` c ON bdc.company_id = c.company_id WHERE {closed_won_condition} AND {closed_date_condition} GROUP BY client_name ORDER BY total_revenue DESC"
        elif intent == "churn_rate":
            w_clause_raw = self.build_where_clause(filters, "d")
            sql = f"WITH churned AS (SELECT COUNT(DISTINCT d.hs_object_id) as churned_count FROM `{project}.{raw}.tbl_deals` d {w_clause_raw} AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.hs_v2_date_entered_closedlost AS STRING), 1, 10)) BETWEEN DATE('{start_date}') AND DATE('{end_date}')), total_active AS (SELECT COUNT(DISTINCT d.deal_id) as active_count FROM `{project}.{marts}.fct_deals` d {where_clause} AND {closed_won_condition}) SELECT churned_count, active_count, ROUND(SAFE_DIVIDE(churned_count, active_count) * 100, 2) as churn_rate_pct FROM churned, total_active"
        elif intent == "arr":
            sql = f"SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, SUM(CAST(rd.hs_mrr AS NUMERIC)) * 12 as arr FROM `{project}.{marts}.fct_deals` d {raw_deal_join} {owner_joins} {company_join} {where_clause} AND {closed_won_condition} AND {closed_date_condition} GROUP BY rep_name ORDER BY arr DESC"
        elif intent == "lead_response_time":
            w_clause_d = self.build_where_clause(filters, "d")
            sql = f"WITH first_activity AS (SELECT hubspot_owner_id, contact_id, MIN(SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(event_ts AS STRING), 1, 10))) as first_act_date FROM `{project}.{marts}.fct_activities` a JOIN `{project}.{marts}.brg_activity_contact` bac ON a.activity_id = bac.activity_id GROUP BY 1, 2) SELECT CONCAT(o.first_name, ' ', o.last_name) as rep_name, AVG(DATE_DIFF(fa.first_act_date, SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.created_ts AS STRING), 1, 10)), DAY)) as avg_response_time_days FROM `{project}.{marts}.dim_contact` d JOIN first_activity fa ON d.contact_id = fa.contact_id LEFT JOIN `{project}.{raw}.tbl_owners` o ON fa.hubspot_owner_id = o.id LEFT JOIN `{project}.{marts}.dim_owner_team_map` tm ON fa.hubspot_owner_id = tm.hubspot_owner_id {w_clause_d} AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.created_ts AS STRING), 1, 10)) BETWEEN DATE('{start_date}') AND DATE('{end_date}') GROUP BY rep_name ORDER BY avg_response_time_days ASC"
        elif intent == "verify_contact":
            w_clause_d = self.build_where_clause(filters, "d")
            sql = f"SELECT hs_object_id as contact_id, firstname, lastname, email, createdate, lastmodifieddate, lifecyclestage, hs_lead_status FROM `{project}.{raw}.tbl_contacts` d {w_clause_d} LIMIT 10"
        elif intent == "verify_company":
            sql = f"SELECT c.company_id, c.name as company_name, c.domain, c.industry, tc.city, tc.state, tc.country FROM `{project}.{marts}.dim_company` c LEFT JOIN `{project}.{raw}.tbl_companies` tc ON c.company_id = tc.hs_object_id WHERE LOWER(c.name) LIKE LOWER('%{filters.get('company_name', '')}%') LIMIT 10"
        elif intent == "verify_deal_stage":
            sql = f"SELECT d.deal_id, ds.label as deal_stage, d.amount, d.dealstage, d.closedate FROM `{project}.{marts}.fct_deals` d JOIN `{project}.{marts}.brg_deal_company` bdc ON d.deal_id = bdc.deal_id JOIN `{project}.{marts}.dim_company` c ON bdc.company_id = c.company_id LEFT JOIN `{project}.{raw}.tbl_deal_stages` ds ON d.dealstage = ds.id WHERE LOWER(c.name) LIKE LOWER('%{filters.get('company_name', '')}%') ORDER BY SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(d.closedate AS STRING), 1, 10)) DESC LIMIT 10"
        elif intent == "verify_prospect_activity":
            w_clause_c = self.build_where_clause(filters, "c")
            sql = f"SELECT c.contact_id, CONCAT(c.first_name, ' ', c.last_name) as contact_name, a.activity_type, a.activity_date, a.details FROM `{project}.{marts}.fct_activities` a JOIN `{project}.{marts}.brg_activity_contact` bac ON a.activity_id = bac.activity_id JOIN `{project}.{raw}.tbl_contacts` c ON bac.contact_id = c.hs_object_id {w_clause_c} ORDER BY SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(a.activity_date AS STRING), 1, 10)) DESC LIMIT 15"
        elif intent == "verify_lead":
            w_clause_d = self.build_where_clause(filters, "d")
            sql = f"SELECT hs_object_id, firstname, lastname, email, lifecyclestage FROM `{project}.{raw}.tbl_contacts` d {w_clause_d} AND LOWER(lifecyclestage) = 'lead'"
        elif intent == "find_company_location":
            w_clause_d = self.build_where_clause(filters, "d")
            sql = f"SELECT hs_object_id as company_id, name, domain, city, state, country FROM `{project}.{raw}.tbl_companies` d {w_clause_d} LIMIT 20"
        else:
            # Avoid default generic query that causes false positives
            return ""

        return sql.strip()


class HubSpotRetriever:
    """
    Retriever for HubSpot data stored in BigQuery.
    Dynamically generates SQL using an LLM based on user intent.
    """

    def __init__(self, project: str | None = None):
        self.bq = BigQueryAdapter()
        self.project = (
            project or settings.BIGQUERY_PROJECT_ID or settings.GCP_PROJECT_ID
        )
        self.dataset = settings.BIGQUERY_DATASET
        self.client = get_genai_client()
        self.model_id = settings.VERTEX_LLM_MODEL_FLASH

        self.schema_agent = HubSpotSchemaAgent()

    def _get_sql_generation_prompt(self, user_query: str, schema_context: str) -> str:
        current_date = datetime.now().strftime("%Y-%m-%d")
        return f"""You are a BigQuery SQL expert. Write a SQL query to answer the user's question about HubSpot data.

CURRENT DATE: {current_date}

CRM DATA CONTEXT:
{schema_context}

RULES:
- Use Standard SQL.
- ALWAYS use fully qualified table names with this project ID: `{self.project}`.
- ALWAYS include a `LIMIT` clause (max 50), even for `COUNT` or aggregate queries.
- Return ONLY the SQL query inside a markdown code block (```sql ... ```).
- If the question cannot be answered with these tables, return: "NOT_CRM_RELATED"
- Time-based queries (e.g., "last month", "yesterday"):
    - Common date columns: `closedate`, `event_ts`, `created_at`.
    - If unsure about the date column name, try to use common ones like `closedate` for deals.
    - RECOMMENDED DATE PARSING: `SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(column AS STRING), 1, 10))`
    - Example for "last month" with `closedate`: `SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(closedate AS STRING), 1, 10)) >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH) AND SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(CAST(closedate AS STRING), 1, 10)) < DATE_TRUNC(CURRENT_DATE(), MONTH)`
- "Leads" often refers to deals in `tbl_deals`. 
- "Closed Won" typically means `dealstage` = 'closedwon'.
- If the query requires a join, use logical connections (e.g., `hubspot_owner_id` or `id`).
- For example, a correct `COUNT` query: `SELECT COUNT(*) FROM \`{self.project}.hubspot_raw.tbl_deals\` LIMIT 1`

USER QUESTION:
{user_query}

SQL QUERY:"""

    def _extract_sql(self, llm_output: str) -> str | None:
        if "NOT_CRM_RELATED" in llm_output:
            return None
        sql_match = re.search(r"```sql\n(.*?)\n```", llm_output, re.DOTALL)
        if sql_match:
            return sql_match.group(1).strip()
        # Fallback if no block but contains SELECT
        if "SELECT" in llm_output.upper():
            return llm_output.strip()
        return None

    def _parse_intent(self, user_query: str) -> dict[str, Any] | None:
        """
        Uses LLM to parse user query into a structured intent JSON.
        """
        current_date = datetime.now().strftime("%Y-%m-%d")

        # Check cache first
        cache_key = (current_workspace(), user_query.strip().lower())
        if cache_key in INTENT_CACHE:
            logger.info(f"Intent cache hit for query: '{user_query}'")
            # Return a deep copy to avoid mutation issues with date resolution
            import copy

            intent_data = copy.deepcopy(INTENT_CACHE[cache_key])
        else:
            prompt = f"""Today is {current_date}. Your task is to parse user questions and categorize them into one of these supported intents: 
            
            - 'kpi_deals_created': Questions about new deals created.
            - 'kpi_deals_won': Questions about closed won deals, revenue (won).
            - 'kpi_deals_open': QUESTIONS about open deals count (not value).
            - 'kpi_win_rate': Questions about win rate percentage.
            - 'kpi_avg_deal_value': Questions about average deal size/value.
            - 'kpi_sales_cycle_length': Questions about time to close deals.
            - 'kpi_pipeline_value': Questions about total value of open deals.
            - 'kpi_meetings_booked_completed': Questions about meetings counts.
            - 'kpi_new_contacts_created': Questions about new contacts/leads.
            - 'kpi_activities_logged': Questions about calls, emails, meetings logged.
            - 'kpi_quotes_sent': Questions about proposals or quotes sent.
            - 'kpi_deal_conversion_rate': Questions about conversion from proposal to won.
            - 'kpi_revenue_per_client': Questions about revenue by client/company.
            - 'kpi_churn_rate': Questions about lost deals or churn.
            - 'kpi_arr': Questions about Annual Recurring Revenue.
            - 'kpi_lead_response_time': Questions about time to respond to leads.
            - 'verify_contact': Check if contact exists.
            - 'verify_company': Check if company exists.
            - 'verify_deal_stage': Check stage of a specific deal.
            - 'verify_prospect_activity': Check activity history for a prospect.
            - 'verify_lead': Check if someone is a lead.
            - 'find_company_location': Find location of a company.
            - 'general': Anything else.

            Return a JSON object with:
            - 'intent': The category enum.
            - 'filters': A dictionary of filters extracted (e.g. {{'owner_name': 'John'}}).
            - 'timeframe': A dictionary with 'start_date' and 'end_date' (YYYY-MM-DD) if explicitly mentioned.
            - 'relative_timeframe': relative time period ENUM if mentioned: [today, yesterday, this_week, last_week, this_month, last_month, this_quarter, last_quarter, this_year, last_year, last_30_days, last_90_days, last_7_days]
            - 'needs_clarification': Boolean, true if the request is ambiguous.
            
            Return ONLY the JSON object.
            If the user mentions a relative time period (e.g. "last month", "this quarter"), set 'relative_timeframe' to the corresponding enum value.

            USER QUESTION: {user_query}
            """

            try:
                response = self.client.models.generate_content(
                    model=self.model_id,
                    contents=prompt,
                )
                output_text = response.text

                # Extract JSON from code blocks if present
                json_match = re.search(r"```json\s*(.*?)\s*```", output_text, re.DOTALL)
                if json_match:
                    json_str = json_match.group(1)
                else:
                    # Try to find the first '{' and last '}'
                    start = output_text.find("{")
                    end = output_text.rfind("}")
                    if start != -1 and end != -1:
                        json_str = output_text[start : end + 1]
                    else:
                        json_str = output_text

                intent_data = json.loads(json_str)
                # Cache the successful parse
                if len(INTENT_CACHE) >= _INTENT_CACHE_MAX:
                    INTENT_CACHE.pop(next(iter(INTENT_CACHE)))
                INTENT_CACHE[cache_key] = intent_data

            except Exception as e:
                logger.error(f"Error parsing intent: {e}")
                return None

        # Resolve relative dates if present
        if intent_data.get("relative_timeframe"):
            dates = resolve_date_range(intent_data["relative_timeframe"])
            intent_data.setdefault("timeframe", {})
            intent_data["timeframe"]["start_date"] = dates["start_date"]
            intent_data["timeframe"]["end_date"] = dates["end_date"]
            # Store original relative term for context
            intent_data["timeframe"]["relative_term"] = intent_data[
                "relative_timeframe"
            ]
            logger.info(
                f"Resolved relative timeframe '{intent_data['relative_timeframe']}' to {dates}"
            )

        return intent_data

    def retrieve(self, query: str) -> tuple[str, str | None]:
        """
        Dynamically generates and executes SQL to retrieve HubSpot data.
        Returns (context_block, generated_sql).
        """
        query_lower = query.lower()
        crm_keywords = [
            "deal",
            "activity",
            "meeting",
            "pipeline",
            "crm",
            "hubspot",
            "won",
            "win",
            "win rate",
            "loss",
            "closed",
            "revenue",
            "arr",
            "sales cycle",
            "response time",
            "contact",
            "lead",
            "mql",
            "sql",
        ]

        # 1. Check if it's a JSON intent first
        try:
            data = json.loads(query)
            if isinstance(data, dict) and ("intent" in data or "output" in data):
                # Handle n8n style input where output key might contain the intent
                intent_obj = data.get("output", data)
                if isinstance(intent_obj, dict) and "intent" in intent_obj:
                    logger.info(f"Detected HubSpot JSON intent: {intent_obj['intent']}")
                    gen = HubSpotIntentSQLGenerator(self.project)
                    sql = gen.generate_sql(intent_obj)
                    return self._execute_and_format_sql(sql)
        except json.JSONDecodeError:
            pass

        # 2. Check for keywords for natural language processing
        if not any(kw in query_lower for kw in crm_keywords):
            return "", None

        try:
            # Try to parse intent first to use templates
            intent_obj = self._parse_intent(query)
            if intent_obj and not intent_obj.get("needs_clarification"):
                gen = HubSpotIntentSQLGenerator(self.project)
                sql = gen.generate_sql(intent_obj)
                # Only proceed if we have a valid SQL query from a template
                if sql:
                    # Context message for the final LLM
                    context_msg = ""
                    if "timeframe" in intent_obj:
                        tf = intent_obj["timeframe"]
                        if "relative_term" in tf:
                            context_msg = f"Data for period '{tf['relative_term']}' ({tf.get('start_date')} to {tf.get('end_date')})"
                        elif "start_date" in tf and "end_date" in tf:
                            context_msg = f"Data for period {tf['start_date']} to {tf['end_date']}"

                    return self._execute_and_format_sql(sql, metadata=context_msg)
                else:
                    logger.info(
                        f"Intent {intent_obj['intent']} did not generate any SQL."
                    )

            # Fallback to dynamic SQL generation
            # 1. Ask Schema Agent for context
            schema_context = self.schema_agent.get_relevant_schema(query)
            if not schema_context:
                logger.info("No relevant HubSpot schema found for query.")
                return "", None

            # 2. Ask LLM to generate SQL
            prompt = self._get_sql_generation_prompt(query, schema_context)

            import time

            llm_response = ""
            for attempt in range(3):
                try:
                    response = self.client.models.generate_content(
                        model=self.model_id,
                        contents=prompt,
                    )
                    llm_response = response.text
                    break
                except Exception as e:
                    if "429" in str(e) or "Resource exhausted" in str(e):
                        if attempt < 2:
                            delay = 2 * (2**attempt)
                            logger.warning(
                                f"HubSpot SQL Gen Rate limit hit. Retrying in {delay}s..."
                            )
                            time.sleep(delay)
                            continue
                    raise e

            sql = self._extract_sql(llm_response)
            if not sql:
                logger.info(f"LLM Response: {llm_response}")
                logger.info("Query deemed not CRM related or SQL extraction failed.")
                return "", None

            # Standardize SQL (remove trailing semicolon)
            sql = sql.strip()
            if sql.endswith(";"):
                sql = sql[:-1].strip()

            # 2. Validate SQL
            logger.info(f"Dynamically generated SQL: {sql}")
            is_valid, error = SQLValidator.validate(sql)

            if not is_valid:
                logger.warning(f"SQL Validation Failed: {error}")
                return f"Could not run CRM query: {error}", sql

            return self._execute_and_format_sql(sql)
        except Exception as e:
            logger.error(f"Error in dynamic HubSpot retrieval: {e}")
            from google.api_core.exceptions import Forbidden

            if isinstance(e, Forbidden) or "403" in str(e):
                sa_email = f"workspace-sync-sa@{self.project}.iam.gserviceaccount.com"
                if "bigquery.jobs.create" in str(e):
                    error_msg = f"--- HubSpot Permission Error ---\nThe service account `{sa_email}` lacks the `BigQuery Job User` (roles/bigquery.jobUser) permission in project `{self.project}`. This is required to run queries."
                    return error_msg, None

                error_msg = f"--- HubSpot Permission Error ---\nI identified the correct tables, but the service account `{sa_email}` does not have permission to query them in BigQuery. Please grant `roles/bigquery.dataViewer` to this service account on the HubSpot datasets."
                return error_msg, None
            # Bubble up the error so the LLM knows it's a technical issue, not "no information"
            return (
                f"--- HubSpot Technical Error ---\nError retrieving CRM data: {str(e)}",
                None,
            )

    def _execute_and_format_sql(
        self, sql: str, metadata: str | None = None
    ) -> tuple[str, str | None]:
        """
        Executes the provided SQL and formats the results.
        """
        try:
            job_config = (
                closed_won.closed_won_job_config()
                if closed_won.uses_closed_won(sql)
                else None
            )
            results = self.bq.query(sql, job_config=job_config)

            if not results:
                return "--- HubSpot Records ---\nNo matching CRM records found.", sql

            # Format result block
            block = "--- HubSpot CRM Context ---\n"
            if metadata:
                block += f"{metadata}\n"
            for i, row in enumerate(results):
                row_str = " | ".join([f"{k}: {v}" for k, v in row.items()])
                block += f"{i + 1}. {row_str}\n"

            return block, sql
        except Exception as e:
            logger.error(f"Error executing HubSpot SQL: {e}")
            raise e
