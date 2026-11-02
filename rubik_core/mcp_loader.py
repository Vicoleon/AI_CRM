import logging
import os

from google.adk.tools.mcp_tool import (
    McpToolset,
    SseConnectionParams,
    StdioConnectionParams,
)
from mcp.client.stdio import StdioServerParameters

from rubik_core.config import settings
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import MCPConnection

logger = logging.getLogger(__name__)


def load_mcp_toolsets(allowed_tools=None) -> list[McpToolset]:
    """Dynamically load MCP toolsets from the database registry."""
    if not settings.ENABLE_MCP_INTEGRATIONS:
        logger.info("MCP integrations are disabled via feature flag.")
        return []

    toolsets = []
    db = SessionLocal()
    try:
        query = db.query(MCPConnection).filter(MCPConnection.enabled.is_(True))
        # If allowed_tools is provided, we might want to filter, but for now we just load all enabled.
        # Ideally, we load based on what the specific agent requires.
        connections = query.all()

        for conn in connections:
            try:
                if conn.kind == "local_stdio" or (
                    conn.kind == "google_remote" and conn.command
                ):
                    # Local stdio execution
                    args = conn.args or []
                    toolset = McpToolset(
                        connection_params=StdioConnectionParams(
                            server_params=StdioServerParameters(
                                command=conn.command, args=args, env=os.environ.copy()
                            ),
                            timeout=60.0,
                        )
                    )
                    toolsets.append(toolset)
                elif conn.kind == "remote_sse" or conn.server_url:
                    # Remote SSE execution
                    # Provide url directly or according to google adk signature
                    toolset = McpToolset(
                        connection_params=SseConnectionParams(url=conn.server_url)
                    )
                    toolsets.append(toolset)
                logger.info(f"Loaded MCP Toolset: {conn.name}")
            except Exception as e:
                logger.error(f"Failed to load MCP Toolset {conn.name}: {e}")
    finally:
        db.close()

    return toolsets
