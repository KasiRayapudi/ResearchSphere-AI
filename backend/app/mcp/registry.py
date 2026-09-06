from typing import Dict, List, Any
from app.mcp.github import GitHubConnector, GoogleDriveConnector, LocalFilesConnector

class MCPConnectorRegistry:
    """
    Registry for managing modular Model Context Protocol (MCP) connectors.
    Extensible for future integrations: Slack, Notion, Confluence, Jira, Teams, Gmail.
    """
    def __init__(self):
        self.connectors = {
            "github": GitHubConnector(),
            "gdrive": GoogleDriveConnector(),
            "local": LocalFilesConnector(),
        }

    def list_all_connectors(self) -> List[Dict[str, Any]]:
        active = [c.get_status() for c in self.connectors.values()]
        # Future connectors catalog
        future = [
            {"connector_id": "mcp-slack-04", "name": "Slack Workspace Connector", "provider": "slack", "status": "disconnected", "synced_count": 0, "is_future_connector": True},
            {"connector_id": "mcp-notion-05", "name": "Notion AI Workspace Sync", "provider": "notion", "status": "disconnected", "synced_count": 0, "is_future_connector": True},
            {"connector_id": "mcp-jira-06", "name": "Atlassian Jira & Confluence", "provider": "confluence", "status": "disconnected", "synced_count": 0, "is_future_connector": True},
            {"connector_id": "mcp-teams-07", "name": "Microsoft Teams & SharePoint", "provider": "sharepoint", "status": "disconnected", "synced_count": 0, "is_future_connector": True},
            {"connector_id": "mcp-gmail-08", "name": "Gmail Enterprise Search", "provider": "gmail", "status": "disconnected", "synced_count": 0, "is_future_connector": True},
        ]
        return active + future
