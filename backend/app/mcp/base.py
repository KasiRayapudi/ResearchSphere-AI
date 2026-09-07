from abc import ABC, abstractmethod
from typing import Any


class BaseMCPConnector(ABC):
    """
    Abstract Base Class for Model Context Protocol (MCP) Connectors.
    """

    def __init__(self, connector_id: str, name: str, provider: str):
        self.connector_id = connector_id
        self.name = name
        self.provider = provider
        # No connector has a real transport yet, so none of them is connected.
        # Reporting "connected" made the workspace UI show live integrations
        # that had never contacted anything.
        self.status = "disconnected"

    @abstractmethod
    def sync_data(self) -> list[dict[str, Any]]:
        """Syncs items from external provider into vector chunks format."""
        pass

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """Returns connector health and sync counts."""
        pass
