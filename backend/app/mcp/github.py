"""
MCP connector definitions.

None of these connectors is implemented: there is no OAuth flow, no external
API call and no sync job behind them. They exist so the connector catalogue,
the registry and the workspace UI have something concrete to enumerate.

They therefore report ``disconnected`` with a synced count of zero, and
``sync_data`` raises. They previously returned ``connected`` with counts of
342, 128 and 56 documents, and hardcoded document lists -- numbers that were
written into the connectors table and shown to users as real sync activity.

Implementing one means giving it real credentials, a real fetch and a real
count; until then the honest answer is that nothing is connected.
"""

from typing import Any

from app.mcp.base import BaseMCPConnector


def _unimplemented(provider: str) -> "NotImplementedError":
    return NotImplementedError(
        f"The {provider} connector is not implemented: no credentials, transport "
        f"or sync job exists for it yet."
    )


class GitHubConnector(BaseMCPConnector):
    def __init__(self, repo_name: str = "enterprise/ai-core"):
        super().__init__("mcp-github-01", "GitHub Enterprise Connector", "github")
        self.repo_name = repo_name

    def sync_data(self) -> list[dict[str, Any]]:
        raise _unimplemented(self.provider)

    def get_status(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "name": self.name,
            "provider": self.provider,
            "status": self.status,
            "synced_count": 0,
            "is_future_connector": False,
            "repo": self.repo_name,
        }


class GoogleDriveConnector(BaseMCPConnector):
    def __init__(self, folder_id: str = "1Fk9_Xm09A1z"):
        super().__init__("mcp-gdrive-02", "Google Drive Knowledge Sync", "gdrive")
        self.folder_id = folder_id

    def sync_data(self) -> list[dict[str, Any]]:
        raise _unimplemented(self.provider)

    def get_status(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "name": self.name,
            "provider": self.provider,
            "status": self.status,
            "synced_count": 0,
            "is_future_connector": False,
        }


class LocalFilesConnector(BaseMCPConnector):
    def __init__(self, path: str = "/data/research"):
        super().__init__("mcp-local-03", "Local Research Filesystem", "local")
        self.path = path

    def sync_data(self) -> list[dict[str, Any]]:
        raise _unimplemented(self.provider)

    def get_status(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "name": self.name,
            "provider": self.provider,
            "status": self.status,
            "synced_count": 0,
            "is_future_connector": False,
        }
