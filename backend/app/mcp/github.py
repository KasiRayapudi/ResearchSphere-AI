from typing import List, Dict, Any
from app.mcp.base import BaseMCPConnector

class GitHubConnector(BaseMCPConnector):
    def __init__(self, repo_name: str = "enterprise/ai-core"):
        super().__init__("mcp-github-01", "GitHub Enterprise Connector", "github")
        self.repo_name = repo_name

    def sync_data(self) -> List[Dict[str, Any]]:
        return [
            {"title": "README.md", "content": "GitHub Repo AI Core specs", "path": "/docs/README.md"},
            {"title": "rag_config.json", "content": "Vector similarity threshold settings", "path": "/config/rag.json"},
        ]

    def get_status(self) -> Dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "name": self.name,
            "provider": self.provider,
            "status": self.status,
            "synced_count": 342,
            "repo": self.repo_name,
        }

class GoogleDriveConnector(BaseMCPConnector):
    def __init__(self, folder_id: str = "1Fk9_Xm09A1z"):
        super().__init__("mcp-gdrive-02", "Google Drive Knowledge Sync", "gdrive")
        self.folder_id = folder_id

    def sync_data(self) -> List[Dict[str, Any]]:
        return [
            {"title": "Q3_Strategy.gdoc", "content": "Enterprise AI Roadmap", "folder": self.folder_id},
        ]

    def get_status(self) -> Dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "name": self.name,
            "provider": self.provider,
            "status": self.status,
            "synced_count": 128,
        }

class LocalFilesConnector(BaseMCPConnector):
    def __init__(self, path: str = "/data/research"):
        super().__init__("mcp-local-03", "Local Research Filesystem", "local")
        self.path = path

    def sync_data(self) -> List[Dict[str, Any]]:
        return [
            {"title": "local_notes.txt", "content": "Local directory watchdog items", "path": self.path},
        ]

    def get_status(self) -> Dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "name": self.name,
            "provider": self.provider,
            "status": self.status,
            "synced_count": 56,
        }
