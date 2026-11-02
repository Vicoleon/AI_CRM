from unittest.mock import MagicMock, patch

from backend.tests._legacy_brand import LEGACY_PATTERN
from rubik_core.retrieval.hubspot import HubSpotRetriever
from rubik_core.retrieval.schema_agent import HubSpotSchemaAgent


def test_hubspot_schema_agent_dynamic_project():
    """
    Test that the HubSpotSchemaAgent includes fully qualified table names
    with the dynamic project ID.
    """
    test_project = "agent-test-proj"

    with (
        patch("rubik_core.retrieval.schema_agent.BigQueryAdapter") as mock_bq,
        patch("rubik_core.retrieval.schema_agent.get_genai_client"),
    ):
        mock_bq_instance = mock_bq.return_value
        mock_bq_instance.project_id = test_project
        mock_bq_instance.list_tables.side_effect = lambda ds: (
            ["tbl1", "tbl2"] if ds == "hubspot_raw" else ["fct1"]
        )

        agent = HubSpotSchemaAgent()
        agent.client.models.generate_content.return_value = MagicMock(
            text="hubspot_raw: tbl1"
        )

        context = agent.get_relevant_schema("query")

        assert f"`{test_project}.hubspot_raw.tbl1`" in context
        assert f"`{test_project}.hubspot_raw.tbl2`" in context
        assert f"`{test_project}.hubspot_sales_marts.fct1`" in context


def test_hubspot_retriever_prompt_no_hardcoded_examples():
    """
    Test that the HubSpotRetriever prompt does not contain hardcoded examples
    and uses the project ID from settings.
    """
    test_project = "retriever-test-proj"

    with (
        patch("rubik_core.retrieval.hubspot.BigQueryAdapter"),
        patch("rubik_core.retrieval.hubspot.get_genai_client"),
        patch("rubik_core.retrieval.hubspot.HubSpotSchemaAgent"),
        patch("rubik_core.config.settings.BIGQUERY_PROJECT_ID", test_project),
    ):
        retriever = HubSpotRetriever()
        retriever.project = test_project

        prompt = retriever._get_sql_generation_prompt("How many deals?", "some context")

        # Verify dynamic project ID
        assert f"project ID: `{test_project}`" in prompt

        # The old hard-coded project and its examples are NOT there.
        assert f"{test_project}.hubspot_sales_marts.fct_activities" not in prompt
        assert not LEGACY_PATTERN.search(prompt)


def test_hubspot_retriever_error_messages_dynamic_project():
    """
    Test that HubSpotRetriever error messages use the dynamic project ID.
    """
    test_project = "another-test-proj"

    with (
        patch("rubik_core.retrieval.hubspot.BigQueryAdapter") as mock_bq,
        patch("rubik_core.retrieval.hubspot.get_genai_client"),
        patch("rubik_core.retrieval.hubspot.HubSpotSchemaAgent") as mock_agent,
        patch("rubik_core.config.settings.BIGQUERY_PROJECT_ID", test_project),
    ):
        retriever = HubSpotRetriever()
        retriever.project = test_project

        # Mock schema agent
        mock_agent_instance = mock_agent.return_value
        mock_agent_instance.get_relevant_schema.return_value = "some schema"

        # Mock BQ adapter to raise Forbidden error
        from google.api_core.exceptions import Forbidden

        mock_bq_instance = mock_bq.return_value
        mock_bq_instance.query.side_effect = Forbidden("403 bigquery.jobs.create")

        retriever.client.models.generate_content.return_value = MagicMock(
            text="```sql\nSELECT * FROM table\n```"
        )

        with patch(
            "rubik_core.retrieval.hubspot.SQLValidator.validate",
            return_value=(True, None),
        ):
            context, sql = retriever.retrieve("Show deals")

            assert f"project `{test_project}`" in context
            assert (
                f"workspace-sync-sa@{test_project}.iam.gserviceaccount.com" in context
            )
            assert not LEGACY_PATTERN.search(context)
