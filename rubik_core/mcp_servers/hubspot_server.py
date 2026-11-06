"""
HubSpot CRM MCP Server.
Exposes CRM query tools via the Model Context Protocol.
"""

import os
import sys

# Add the project root to sys.path so we can import rubik_core
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if project_root not in sys.path:
    sys.path.append(project_root)

from mcp.server.fastmcp import FastMCP

from rubik_core.brand import PRODUCT_NAME  # noqa: E402

mcp = FastMCP(f"{PRODUCT_NAME} HubSpot CRM")


@mcp.tool()
def search_hubspot_data(query: str) -> str:
    """Search HubSpot CRM data for deals, revenue, pipeline, or sales activity.

    Args:
        query: Natural-language query about CRM data.

    Returns:
        Formatted report of matching CRM records.
    """
    from rubik_core.retrieval.hubspot_api import HubSpotAPIRetriever
    from rubik_core.tenancy.secrets import workspace_secret

    try:
        if not (workspace_secret("hubspot", "read_token") or "").strip():
            return "HubSpot read token is not configured for this workspace."
        retriever = HubSpotAPIRetriever()
        context_block, _debug = retriever.retrieve(query)

        if not context_block:
            return "No matching HubSpot CRM records found."

        return context_block
    except Exception as e:
        return f"Error querying HubSpot: {str(e)}"


@mcp.tool()
def get_deal_stages() -> str:
    """List all available deal stages in HubSpot."""
    from rubik_core.retrieval.hubspot_api import HubSpotAPIClient
    from rubik_core.tenancy.secrets import workspace_secret

    try:
        if not (workspace_secret("hubspot", "read_token") or "").strip():
            return "HubSpot read token is not configured for this workspace."
        stages = HubSpotAPIClient().deal_stage_labels()
        if not stages:
            return "No deal stages found."

        lines = [f"- {label} (ID: {stage_id})" for stage_id, label in stages.items()]
        return "Deal Stages:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error fetching deal stages: {str(e)}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
