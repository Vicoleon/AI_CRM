import logging
import re

logger = logging.getLogger(__name__)


class SQLValidator:
    """
    Validates BigQuery SQL queries for HubSpot retrieval to ensure they are safe.
    """

    ALLOWED_DATASETS = {"hubspot_raw", "hubspot_sales_marts"}

    @staticmethod
    def validate(sql: str) -> tuple[bool, str]:
        """
        Main validation entry point.
        Returns (is_valid, error_message).
        """
        clean_sql = sql.strip()
        # Strip trailing semicolon if present
        if clean_sql.endswith(";"):
            clean_sql = clean_sql[:-1].strip()

        # 1. Must start with SELECT
        if not re.match(r"^SELECT\b", clean_sql, re.IGNORECASE):
            return False, "Query must start with SELECT"

        # 2. No inner semicolons or multiple statements
        if ";" in clean_sql:
            return False, "Inner semicolons and multiple statements are not allowed"

        # 3. Check for unauthorized tables
        # Look for table names in FROM and JOIN clauses
        # Regex to capture words after FROM/JOIN, ignoring subqueries and aliases for now
        # BigQuery table format: `project.dataset.table` or `dataset.table` or table
        # We look for the last part of the table reference.
        tables_found = SQLValidator._extract_tables(clean_sql)
        for table in tables_found:
            # table might be `project.dataset.name` or `dataset.name`
            parts = table.replace("`", "").split(".")
            if len(parts) >= 2:
                dataset_name = parts[-2]
                if dataset_name not in SQLValidator.ALLOWED_DATASETS:
                    return False, f"Unauthorized dataset access: {dataset_name}"
            else:
                # If no dataset prefix, we could reject or check if it's in a default dataset
                # For safety, let's require dataset prefix or check against a default
                return False, f"Table reference must include dataset: {table}"

        # 4. Enforce LIMIT <= 50
        limit_match = re.search(r"LIMIT\s+(\d+)", clean_sql, re.IGNORECASE)
        if not limit_match:
            return False, "Query must include a LIMIT clause (max 50)"

        limit_val = int(limit_match.group(1))
        if limit_val > 50:
            return False, f"LIMIT {limit_val} exceeds maximum allowed value of 50"

        return True, ""

    @staticmethod
    def _extract_tables(sql: str) -> list[str]:
        """
        Extracts table references from FROM and JOIN clauses.
        """
        # This is a simplified extraction.
        # We look for word matches after FROM or JOIN, handling backticks.
        # Format: FROM `proj.ds.table` AS alias
        pattern = r"(?:FROM|JOIN)\s+([\w\.\-`]+)"
        matches = re.findall(pattern, sql, re.IGNORECASE)
        return matches
